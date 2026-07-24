from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from pathlib import Path
from typing import BinaryIO, TextIO

from .agent_context import AgentContext
from .agent_cache import (
    CacheClientError,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_MAX_MEMORY_BYTES,
    DEFAULT_STARTUP_TIMEOUT,
    parse_memory_size,
    navigation_cache_path,
    query_cache,
    run_cache_daemon,
    start_cache,
    stop_cache,
    watch_workspace_changes,
)
from .agent_layers import build_codebase_index, layer_payload, render_layer
from .agent_protocol import AgentProtocolError, SUPPORTED_ACTIONS, SUPPORTED_DETAILS, SUPPORTED_RELATIONS
from .agent_templates import install_opencode_support, install_skill
from .parallel_outline import ParallelOutlineError, parse_worker_setting


_MAX_WORKER_RECORD_BYTES = 1024 * 1024
_INVALID_JSON_MESSAGE = "Invalid JSON request."
_INVALID_ENCODING_MESSAGE = "Invalid UTF-8 request."
_REQUEST_TOO_LARGE_MESSAGE = "Request exceeds the 1 MiB limit."
_INTERNAL_ERROR_MESSAGE = "Internal request error."
_SOURCE_UNAVAILABLE_MESSAGE = "Selected source is unavailable."
_WORKER_REVISION_CHECK_INTERVAL_SECONDS = 30.0


class _CliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
            "metrics",
        ],
    )
    view.add_argument("--query", default="")
    view.add_argument("--format", default="markdown", choices=["markdown", "json"])
    view.add_argument("--deep-projects", action="store_true", help="Deep-parse project dependencies for the projects layer.")
    view.add_argument("--workers", type=parse_worker_setting, default=0)
    view.set_defaults(func=_view)

    index = subcommands.add_parser("index", help="Materialize a JSON codebase index.")
    index.add_argument("--root", type=Path, default=Path("."))
    index.add_argument("--project-file", type=Path)
    index.add_argument("--out", type=Path, default=Path(".delphi-lsp") / "agent-index" / "index.json")
    index.add_argument("--workers", type=parse_worker_setting, default=0)
    index.set_defaults(func=_index)

    skill = subcommands.add_parser("skill", help="Install agent skill templates.")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_install = skill_commands.add_parser("install", help="Install .agents skill.")
    skill_install.add_argument("--target", type=Path, default=Path("."))
    skill_install.add_argument("--force", action="store_true")
    skill_install.set_defaults(func=_skill_install)

    opencode = subcommands.add_parser("opencode", help="Install opencode integration.")
    opencode_commands = opencode.add_subparsers(dest="opencode_command", required=True)
    opencode_install = opencode_commands.add_parser(
        "install", help="Install the package-named skill, Markdown agent, and OpenCode plugin."
    )
    opencode_install.add_argument("--target", type=Path, default=Path("."))
    opencode_install.add_argument("--python", default=sys.executable)
    opencode_install.add_argument("--force", action="store_true")
    opencode_install.add_argument(
        "--write-agent",
        "--write-config",
        dest="write_config",
        action="store_true",
        help="Deprecated compatibility option; the Markdown agent is always installed and opencode.json is never touched.",
    )
    opencode_install.set_defaults(func=_opencode_install)

    worker = subcommands.add_parser("worker", help="Serve Protocol v2 NDJSON requests.")
    worker.add_argument("--root", type=Path, required=True)
    worker.add_argument("--project-file", type=Path)
    worker.add_argument("--workers", type=parse_worker_setting, default=0)
    worker.set_defaults(func=_worker)

    cache = subcommands.add_parser("cache", help="Manage the shared Protocol v2 cache daemon.")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_start = cache_commands.add_parser("start", help="Start the cache daemon if needed.")
    _add_cache_start_arguments(cache_start)
    cache_start.set_defaults(func=_cache_start)
    cache_status_parser = cache_commands.add_parser("status", help="Show cache daemon status.")
    cache_status_parser.add_argument("--root", type=Path, default=Path("."))
    cache_status_parser.add_argument("--format", choices=["text", "json"], default="text")
    cache_status_parser.set_defaults(func=_cache_status)
    cache_stop = cache_commands.add_parser("stop", help="Stop the cache daemon if it is running.")
    cache_stop.add_argument("--root", type=Path, default=Path("."))
    cache_stop.set_defaults(func=_cache_stop)
    cache_serve = cache_commands.add_parser("serve", help=argparse.SUPPRESS)
    cache_serve.add_argument("--root", type=Path, required=True)
    cache_serve.add_argument("--project-file", type=Path)
    cache_serve.add_argument("--max-memory", type=parse_memory_size, required=True)
    cache_serve.add_argument("--workers", type=parse_worker_setting, required=True)
    cache_serve.add_argument("--idle-timeout", type=_positive_integer, required=True)
    cache_serve.set_defaults(func=_cache_serve)

    query = subcommands.add_parser("query", help="Send an ergonomic request to a running cache daemon.")
    query.add_argument("--root", type=Path, default=Path("."))
    query.add_argument("action", choices=SUPPORTED_ACTIONS)
    query.add_argument("value", nargs="?", default="")
    query.add_argument("--project-id", default="")
    query.add_argument("--detail", choices=SUPPORTED_DETAILS, default="summary")
    query.add_argument("--relation", choices=SUPPORTED_RELATIONS)
    query.add_argument("--cursor", default="")
    query.add_argument("--max-items", type=_max_items, default=12)
    query.add_argument("--max-chars", type=_max_chars, default=12000)
    query.set_defaults(func=_query)

    return parser


def _add_cache_start_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--project-file", type=Path)
    parser.add_argument("--max-memory", type=parse_memory_size, default=DEFAULT_MAX_MEMORY_BYTES)
    parser.add_argument("--workers", type=parse_worker_setting, default=0)
    parser.add_argument("--idle-timeout", type=_positive_integer, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--startup-timeout", type=_positive_float, default=DEFAULT_STARTUP_TIMEOUT)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _max_items(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 50:
        raise argparse.ArgumentTypeError("value must be between 1 and 50")
    return parsed


def _max_chars(value: str) -> int:
    parsed = int(value)
    if not 256 <= parsed <= 40000:
        raise argparse.ArgumentTypeError("value must be between 256 and 40000")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if not getattr(sys.stdout, "closed", False):
            sys.stdout.flush()
    except _CliError as error:
        sys.stderr.write(f"cli_error:{error.code}: {error.message}\n")
        sys.stderr.flush()
        return 1
    except CacheClientError as error:
        return _cache_error(error)
    except ParallelOutlineError as error:
        sys.stderr.write(f"cli_error:parallel_failed: {error}\n")
        sys.stderr.flush()
        return 1
    except BrokenPipeError:
        _discard_broken_stdout()
        os._exit(1)
    except OSError as error:
        sys.stderr.write(f"cli_error:io_error: {error}\n")
        sys.stderr.flush()
        return 1
    return result if isinstance(result, int) else 0


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
    replacement = open(os.devnull, "w", encoding=getattr(stdout, "encoding", None) or "utf-8")
    sys.stdout = replacement
    sys.__stdout__ = replacement
    try:
        stdout.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _view(args: argparse.Namespace) -> None:
    args.root = _workspace_root(args.root)
    index = build_codebase_index(
        args.root,
        project_file=args.project_file,
        index_projects=args.deep_projects,
        workers=args.workers,
    )
    sys.stdout.write(render_layer(index, args.layer, query=args.query, output_format=args.format))


def _index(args: argparse.Namespace) -> None:
    args.root = _workspace_root(args.root)
    index = build_codebase_index(
        args.root,
        project_file=args.project_file,
        index_projects=True,
        workers=args.workers,
    )
    payload = {
        "overview": layer_payload(index, "overview"),
        "projects": layer_payload(index, "projects"),
        "problems": layer_payload(index, "problems"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)


def _skill_install(args: argparse.Namespace) -> None:
    try:
        skill_path = install_skill(args.target, force=args.force)
    except FileExistsError as error:
        raise _CliError("install_conflict", str(error)) from None
    print(skill_path)


def _opencode_install(args: argparse.Namespace) -> None:
    try:
        skill_path, plugin_path, agent_path = install_opencode_support(
            args.target,
            python_executable=args.python,
            force=args.force,
            write_config=args.write_config,
        )
    except FileExistsError as error:
        raise _CliError("install_conflict", str(error)) from None
    print(skill_path)
    print(plugin_path)
    print(agent_path)


def _worker(args: argparse.Namespace) -> None:
    args.root = _workspace_root(args.root)
    context = _open_worker_context(args.root, args.project_file, args.workers)
    watcher_stop = threading.Event()
    watcher = threading.Thread(
        target=watch_workspace_changes,
        kwargs={
            "root": args.root,
            "stop_event": watcher_stop,
            "on_change": context.invalidate_revision_cache,
        },
        name="delphi-worker-watcher",
        daemon=True,
    )
    watcher.start()
    try:
        _serve_worker(context, sys.stdin.buffer, sys.stdout.buffer, sys.stderr)
    finally:
        watcher_stop.set()
        watcher.join(timeout=2.0)
        try:
            sys.stdout.close()
        except (BrokenPipeError, OSError) as error:
            raise BrokenPipeError from error


def _open_worker_context(
    root: Path,
    project_file: Path | None,
    workers: int,
) -> AgentContext:
    try:
        cache_dir: Path | None = navigation_cache_path(root, create=True)
    except (CacheClientError, OSError):
        cache_dir = None
    return AgentContext.open(
        root,
        project_file,
        workers=workers,
        revision_check_interval_seconds=_WORKER_REVISION_CHECK_INTERVAL_SECONDS,
        navigation_cache_dir=cache_dir,
    )


def _write_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _write_warning(warning: str) -> None:
    if warning:
        sys.stderr.write(warning + "\n")
        sys.stderr.flush()


def _cache_error(error: CacheClientError) -> int:
    sys.stderr.write(f"cache_error:{error.code}: {error.message}\n")
    sys.stderr.flush()
    return 1


def _cache_start(args: argparse.Namespace) -> int:
    args.root = _workspace_root(args.root)
    try:
        start_cache(
            args.root,
            project_file=args.project_file,
            max_memory_bytes=args.max_memory,
            workers=args.workers,
            idle_timeout=args.idle_timeout,
            startup_timeout=args.startup_timeout,
        )
        response = query_cache(args.root, {"action": "status"})
    except CacheClientError as error:
        return _cache_error(error)
    _write_json(response.payload)
    _write_warning(response.warning)
    return 0


def _cache_status(args: argparse.Namespace) -> int:
    args.root = _workspace_root(args.root)
    try:
        response = query_cache(args.root, {"action": "status"})
    except CacheClientError as error:
        return _cache_error(error)
    if args.format == "json":
        _write_json(response.payload)
    else:
        status = response.payload
        sys.stdout.write(
            f"running pid={status['pid']} state={status['cache_state']} "
            f"memory={status['current_utilization_percent']:.1f}%\n"
        )
    _write_warning(response.warning)
    return 0


def _cache_stop(args: argparse.Namespace) -> int:
    args.root = _workspace_root(args.root)
    try:
        response = query_cache(args.root, {"action": "status"})
    except CacheClientError as error:
        if error.code in {"unavailable", "cache_not_running"}:
            try:
                stop_cache(args.root)
            except CacheClientError as cleanup_error:
                return _cache_error(cleanup_error)
            _write_json({"stopped": False})
            return 0
        return _cache_error(error)
    _write_warning(response.warning)
    try:
        stop_cache(args.root)
    except CacheClientError as error:
        return _cache_error(error)
    _write_json({"stopped": True})
    return 0


def _cache_serve(args: argparse.Namespace) -> int:
    args.root = _workspace_root(args.root)
    run_cache_daemon(
        args.root,
        project_file=str(args.project_file or ""),
        max_memory_bytes=args.max_memory,
        workers=args.workers,
        idle_timeout=args.idle_timeout,
    )
    return 0


def _query(args: argparse.Namespace) -> int:
    args.root = _workspace_root(args.root)
    request: dict[str, object] = {"action": args.action}
    if args.value:
        if args.action in {"find", "metrics"}:
            request["query"] = args.value
        elif args.action in {"focus", "inspect", "trace"}:
            request["target_id"] = args.value
        else:
            sys.stderr.write(f"cache_error:invalid_request: {args.action} does not accept a value.\n")
            sys.stderr.flush()
            return 1
    for argument, field in (("project_id", "project_id"), ("detail", "detail"), ("relation", "relation"), ("cursor", "cursor"), ("max_items", "max_items"), ("max_chars", "max_chars")):
        value = getattr(args, argument)
        if value is not None:
            request[field] = value
    try:
        response = query_cache(args.root, request)
    except CacheClientError as error:
        return _cache_error(error)
    _write_json(response.payload)
    _write_warning(response.warning)
    return 0


def _workspace_root(value: str | Path) -> Path:
    root = Path(value).expanduser()
    if not root.exists():
        raise _CliError(
            "workspace_not_found",
            f"Workspace root does not exist: {root}",
        )
    if not root.is_dir():
        raise _CliError(
            "workspace_not_directory",
            f"Workspace root is not a directory: {root}",
        )
    return root.resolve()


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
            message = response.to_mapping()
        except BrokenPipeError:
            raise
        except AgentProtocolError as error:
            message = _SOURCE_UNAVAILABLE_MESSAGE if error.code == "source_unavailable" else error.message
            message = _worker_error(error.code, message)
        except Exception as error:
            error_stream.write(f"{type(error).__name__}\n")
            error_stream.flush()
            message = _worker_error("internal_error", _INTERNAL_ERROR_MESSAGE)
        _write_worker_message(output_stream, message)
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
    try:
        output_stream.write(serialized + b"\n")
        output_stream.flush()
    except BrokenPipeError:
        raise
    except OSError as error:
        raise BrokenPipeError from error


if __name__ == "__main__":
    raise SystemExit(main())
