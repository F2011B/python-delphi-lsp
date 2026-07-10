from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from .agent_context import AgentContext
from .agent_layers import build_codebase_index, layer_payload, render_layer
from .agent_protocol import AgentProtocolError
from .agent_templates import install_opencode_support, install_skill


_MAX_WORKER_RECORD_BYTES = 1024 * 1024
_INVALID_JSON_MESSAGE = "Invalid JSON request."
_INVALID_ENCODING_MESSAGE = "Invalid UTF-8 request."
_REQUEST_TOO_LARGE_MESSAGE = "Request exceeds the 1 MiB limit."
_INTERNAL_ERROR_MESSAGE = "Internal request error."
_SOURCE_UNAVAILABLE_MESSAGE = "Selected source is unavailable."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delphi-lsp-agent",
        description="Agent-facing Delphi/Object Pascal codebase navigation helpers.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    view = subcommands.add_parser("view", help="Render a layered codebase view.")
    view.add_argument("--root", type=Path, default=Path("."))
    view.add_argument("--project-file", type=Path)
    view.add_argument(
        "--layer",
        required=True,
        choices=[
            "overview",
            "projects",
            "units",
            "unit",
            "symbols",
            "symbol",
            "implementation",
            "references",
            "problems",
        ],
    )
    view.add_argument("--query", default="")
    view.add_argument("--format", default="markdown", choices=["markdown", "json"])
    view.add_argument("--deep-projects", action="store_true", help="Deep-parse project dependencies for the projects layer.")
    view.set_defaults(func=_view)

    index = subcommands.add_parser("index", help="Materialize a JSON codebase index.")
    index.add_argument("--root", type=Path, default=Path("."))
    index.add_argument("--project-file", type=Path)
    index.add_argument("--out", type=Path, default=Path(".delphi-lsp") / "agent-index" / "index.json")
    index.set_defaults(func=_index)

    skill = subcommands.add_parser("skill", help="Install agent skill templates.")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_install = skill_commands.add_parser("install", help="Install .agents skill.")
    skill_install.add_argument("--target", type=Path, default=Path("."))
    skill_install.add_argument("--force", action="store_true")
    skill_install.set_defaults(func=_skill_install)

    opencode = subcommands.add_parser("opencode", help="Install opencode integration.")
    opencode_commands = opencode.add_subparsers(dest="opencode_command", required=True)
    opencode_install = opencode_commands.add_parser("install", help="Install .agents skill and opencode plugin.")
    opencode_install.add_argument("--target", type=Path, default=Path("."))
    opencode_install.add_argument("--python", default=sys.executable)
    opencode_install.add_argument("--force", action="store_true")
    opencode_install.add_argument("--write-config", action="store_true")
    opencode_install.set_defaults(func=_opencode_install)

    worker = subcommands.add_parser("worker", help="Serve Protocol v2 NDJSON requests.")
    worker.add_argument("--root", type=Path, required=True)
    worker.add_argument("--project-file", type=Path)
    worker.set_defaults(func=_worker)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        sys.stdout.flush()
    except BrokenPipeError:
        _discard_broken_stdout()
        return 1
    return 0


def _discard_broken_stdout() -> None:
    stdout = sys.stdout
    try:
        stdout_fd = stdout.fileno()
    except (AttributeError, OSError, ValueError):
        stdout_fd = None
    if stdout_fd is not None:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, stdout_fd)
        finally:
            os.close(devnull_fd)
    try:
        stdout.close()
    except (BrokenPipeError, OSError, ValueError):
        pass
    sys.stdout = open(os.devnull, "w", encoding=getattr(stdout, "encoding", None) or "utf-8")


def _view(args: argparse.Namespace) -> None:
    index = build_codebase_index(args.root, project_file=args.project_file, index_projects=args.deep_projects)
    sys.stdout.write(render_layer(index, args.layer, query=args.query, output_format=args.format))


def _index(args: argparse.Namespace) -> None:
    index = build_codebase_index(args.root, project_file=args.project_file, index_projects=True)
    payload = {
        "overview": layer_payload(index, "overview"),
        "projects": layer_payload(index, "projects"),
        "problems": layer_payload(index, "problems"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)


def _skill_install(args: argparse.Namespace) -> None:
    skill_path = install_skill(args.target, force=args.force)
    print(skill_path)


def _opencode_install(args: argparse.Namespace) -> None:
    skill_path, plugin_path, config_path = install_opencode_support(
        args.target,
        python_executable=args.python,
        force=args.force,
        write_config=args.write_config,
    )
    print(skill_path)
    print(plugin_path)
    if config_path is not None:
        print(config_path)


def _worker(args: argparse.Namespace) -> None:
    context = AgentContext.open(args.root, args.project_file)
    _serve_worker(context, sys.stdin.buffer, sys.stdout.buffer, sys.stderr)


def _serve_worker(context: AgentContext, input_stream: BinaryIO, output_stream: BinaryIO, error_stream: TextIO) -> None:
    discarding_oversize_record = False
    while True:
        record = input_stream.readline(_MAX_WORKER_RECORD_BYTES + 1)
        if not record:
            return
        if discarding_oversize_record:
            if record.endswith(b"\n"):
                discarding_oversize_record = False
            continue
        if not record.endswith(b"\n") and len(record) > _MAX_WORKER_RECORD_BYTES:
            if record.endswith(b"\r") and input_stream.read(1) == b"\n":
                record = record[:-1]
            else:
                discarding_oversize_record = True
                _write_worker_message(
                    output_stream,
                    _worker_error("request_too_large", _REQUEST_TOO_LARGE_MESSAGE),
                )
                continue

        record = record.rstrip(b"\r\n")
        if len(record) > _MAX_WORKER_RECORD_BYTES:
            _write_worker_message(
                output_stream,
                _worker_error("request_too_large", _REQUEST_TOO_LARGE_MESSAGE),
            )
            continue
        try:
            text = record.decode("utf-8")
        except UnicodeDecodeError:
            _write_worker_message(
                output_stream,
                _worker_error("invalid_encoding", _INVALID_ENCODING_MESSAGE),
            )
            continue
        if not text.strip():
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            _write_worker_message(output_stream, _worker_error("invalid_json", _INVALID_JSON_MESSAGE))
            continue
        try:
            response = context.handle(request)
            _write_worker_message(output_stream, response.to_mapping())
        except BrokenPipeError:
            raise
        except AgentProtocolError as error:
            message = _SOURCE_UNAVAILABLE_MESSAGE if error.code == "source_unavailable" else error.message
            _write_worker_message(output_stream, _worker_error(error.code, message))
        except Exception as error:
            error_stream.write(f"{type(error).__name__}\n")
            error_stream.flush()
            _write_worker_message(output_stream, _worker_error("internal_error", _INTERNAL_ERROR_MESSAGE))
def _worker_error(code: str, message: str) -> dict[str, object]:
    return {"schema": 2, "error": {"code": code, "message": message}}


def _write_worker_message(output_stream: BinaryIO, message: object) -> None:
    serialized = json.dumps(
        message,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    output_stream.write(serialized + b"\n")
    output_stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
