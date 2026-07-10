from __future__ import annotations

from pathlib import Path
import json


SKILL_NAME = "delphi-codebase-navigator"


def install_skill(target: str | Path, *, force: bool = False) -> Path:
    target_path = Path(target).expanduser().resolve()
    skill_path = target_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    _write_text(skill_path, _skill_markdown(), force=force)
    return skill_path


def install_opencode_support(
    target: str | Path,
    *,
    python_executable: str,
    force: bool = False,
    write_config: bool = False,
) -> tuple[Path, Path, Path | None]:
    target_path = Path(target).expanduser().resolve()
    skill_path = install_skill(target_path, force=force)
    tool_path = target_path / ".opencode" / "tools" / "delphi_codebase.ts"
    _write_text(tool_path, _opencode_tool(python_executable), force=force)
    config_path: Path | None = None
    if write_config:
        config_path = target_path / "opencode.json"
        _merge_opencode_config(config_path)
    return skill_path, tool_path, config_path


def _write_text(path: Path, text: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != text and not force:
        raise FileExistsError(f"{path} already exists with different content; pass --force to overwrite")
    path.write_text(text, encoding="utf-8")


def _skill_markdown() -> str:
    return """---
name: delphi-codebase-navigator
description: Inspect Delphi and Object Pascal codebases through python-delphi-lsp layered semantic views instead of raw source-file reading.
compatibility: opencode
metadata:
  package: python-delphi-lsp
---

## When to use this skill

Use this skill when a task asks you to understand a Delphi/Object Pascal codebase, package, project file, unit, symbol, dependency, include file, or reference chain.

## Hard rules

- Do not inspect Delphi source by loading whole `.pas`, `.dpr`, `.dpk`, or `.inc` files into context.
- Do not use shell text-search commands for Delphi navigation.
- Use the `delphi_codebase` opencode tool when it is available.
- If the tool is not available, run `delphi-lsp-agent view` commands only after the user permits command execution.
- When a task asks for codebase inspection, call `delphi_codebase` first; do not write "let me inspect" text before the tool call.
- Cite files and line numbers from the layer output.

## Workflow

1. Start with `overview` to learn projects, units, search paths, include paths, defines, and unresolved problems.
2. Use `projects` to understand `.dpr`, `.dpk`, `.dproj`, `.cfg`, and include resolution.
3. Use `units` and `unit` to inspect outlines without routine bodies.
4. Use `symbols` to find a type, routine, property, field, constant, or variable.
5. Use `symbol` for a focused symbol card and children.
6. Use `implementation` with a concrete class, routine, or member query when you need the complete source for that one symbol.
7. Use `references` for definition/reference-oriented evidence.
8. Use `problems` before concluding that code is missing.

## Tool examples

- `delphi_codebase({ "layer": "overview" })`
- `delphi_codebase({ "layer": "unit", "query": "Worker" })`
- `delphi_codebase({ "layer": "symbols", "query": "TWorker", "format": "json" })`
- `delphi_codebase({ "layer": "implementation", "query": "TWorker.Run" })`

Prefer narrow follow-up queries over broad output.
"""


def _opencode_tool(python_executable: str) -> str:
    python_json = json.dumps(python_executable)
    return f"""import {{ tool }} from "@opencode-ai/plugin"

const PYTHON = {python_json}

async function streamToText(stream: ReadableStream | null): Promise<string> {{
  if (!stream) return ""
  return await new Response(stream).text()
}}

export default tool({{
  description: "Inspect a Delphi/Object Pascal codebase through python-delphi-lsp layered semantic views.",
  args: {{
    layer: tool.schema.enum(["overview", "projects", "units", "unit", "symbols", "symbol", "implementation", "references", "problems"]).describe("Layer to inspect"),
    query: tool.schema.string().optional().describe("Unit, file, or symbol filter"),
    root: tool.schema.string().optional().describe("Workspace root; defaults to current worktree or directory"),
    format: tool.schema.enum(["markdown", "json"]).optional().describe("Output format; defaults to markdown"),
  }},
  async execute(args, context) {{
    const contextDirectory = context.directory && context.directory !== "/" ? context.directory : undefined
    const root = args.root ?? context.worktree ?? contextDirectory ?? process.cwd()
    const command = [
      PYTHON,
      "-m",
      "delphi_lsp.agent_cli",
      "view",
      "--root",
      root,
      "--layer",
      args.layer,
      "--format",
      args.format ?? "markdown",
    ]
    if (args.query) {{
      command.push("--query", args.query)
    }}

    const proc = Bun.spawn(command, {{
      stdout: "pipe",
      stderr: "pipe",
    }})
    const [stdout, stderr, exitCode] = await Promise.all([
      streamToText(proc.stdout),
      streamToText(proc.stderr),
      proc.exited,
    ])
    if (exitCode !== 0) {{
      throw new Error(stderr || `delphi_codebase failed with exit code ${{exitCode}}`)
    }}
    return stdout.trim()
  }},
}})
"""


def _merge_opencode_config(config_path: Path) -> None:
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        config = {"$schema": "https://opencode.ai/config.json"}
    agents = config.setdefault("agent", {})
    agents["vllm-delphi-codebase"] = {
        "description": "Use the Delphi codebase navigator skill and tool without direct filesystem source inspection.",
        "temperature": 0,
        "prompt": (
            "You are a tool-calling assistant. When the user asks for a tool call, call the matching tool. "
            "The matching tool for Delphi/Object Pascal codebase inspection is delphi_codebase. "
            "Use delphi_codebase before answering Delphi codebase questions. "
            "Use implementation for complete source of one concrete class, routine, or member."
        ),
        "tools": {
            "delphi_codebase": True,
            "skill": False,
            "lsp": False,
            "bash": False,
            "read": False,
            "glob": False,
            "grep": False,
            "edit": False,
            "write": False,
            "task": False,
            "webfetch": False,
            "todowrite": False,
        },
        "permission": {
            "delphi_codebase": "allow",
            "lsp": "deny",
            "skill": "deny",
        },
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["SKILL_NAME", "install_opencode_support", "install_skill"]
