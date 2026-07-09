from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent_layers import build_codebase_index, layer_payload, render_layer
from .agent_templates import install_opencode_support, install_skill


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
    opencode_install = opencode_commands.add_parser("install", help="Install .agents skill and opencode custom tool.")
    opencode_install.add_argument("--target", type=Path, default=Path("."))
    opencode_install.add_argument("--python", default=sys.executable)
    opencode_install.add_argument("--force", action="store_true")
    opencode_install.add_argument("--write-config", action="store_true")
    opencode_install.set_defaults(func=_opencode_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BrokenPipeError:
        return 1
    return 0


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
    skill_path, tool_path, config_path = install_opencode_support(
        args.target,
        python_executable=args.python,
        force=args.force,
        write_config=args.write_config,
    )
    print(skill_path)
    print(tool_path)
    if config_path is not None:
        print(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
