from __future__ import annotations

from pathlib import Path
import json


SKILL_NAME = "delphi-codebase-navigator"
_LEGACY_TOOL_RELATIVE_PATH = Path(".opencode") / "tools" / "delphi_codebase.ts"


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
    legacy_path = target_path / _LEGACY_TOOL_RELATIVE_PATH
    skill_path = target_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    plugin_path = target_path / ".opencode" / "plugins" / "delphi_codebase.ts"
    config_path = target_path / "opencode.json" if write_config else None
    skill_text = _skill_markdown()
    plugin_text = _opencode_plugin(python_executable)

    legacy_before = _preflight_legacy(legacy_path, force=force)
    snapshots: dict[Path, bytes | None] = {
        legacy_path: legacy_before,
        skill_path: _preflight_destination(skill_path, skill_text, force=force),
        plugin_path: _preflight_destination(plugin_path, plugin_text, force=force),
    }
    writes = [(skill_path, skill_text), (plugin_path, plugin_text)]
    if config_path is not None:
        config_before, config_text = _render_opencode_config(config_path)
        snapshots[config_path] = config_before
        writes.append((config_path, config_text))

    try:
        for path, text in writes:
            if snapshots[path] != text.encode("utf-8"):
                _write_text(path, text, force=True)
        if legacy_before is not None:
            legacy_path.unlink()
    except BaseException:
        for path, content in reversed(tuple(snapshots.items())):
            _restore_file(path, content)
        raise
    return skill_path, plugin_path, config_path


def _preflight_legacy(legacy_path: Path, *, force: bool) -> bytes | None:
    if not legacy_path.exists():
        return None
    if not legacy_path.is_file():
        raise FileExistsError(f"Legacy opencode tool path is not a file: {legacy_path}")
    legacy_bytes = legacy_path.read_bytes()
    try:
        legacy_text = legacy_bytes.decode("utf-8")
    except UnicodeDecodeError:
        legacy_text = ""
    if not _is_generated_legacy_tool(legacy_text) and not force:
        raise FileExistsError(
            f"Unrecognized legacy opencode tool at {legacy_path}; pass --force to remove it."
        )
    return legacy_bytes


def _preflight_destination(path: Path, text: str, *, force: bool) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise FileExistsError(f"Generated destination is not a file: {path}")
    content = path.read_bytes()
    if content != text.encode("utf-8") and not force:
        raise FileExistsError(f"{path} already exists with different content; pass --force to overwrite")
    return content


def _restore_file(path: Path, content: bytes | None) -> None:
    if content is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _is_generated_legacy_tool(text: str) -> bool:
    return (
        "@opencode-ai/plugin" in text
        and "layered semantic views" in text
        and "python-delphi-lsp" in text
    )


def _write_text(path: Path, text: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != text and not force:
        raise FileExistsError(f"{path} already exists with different content; pass --force to overwrite")
    path.write_text(text, encoding="utf-8")


def _skill_markdown() -> str:
    return """---
name: delphi-codebase-navigator
description: Inspect Delphi and Object Pascal codebases through the Protocol v2 semantic navigator.
compatibility: opencode
metadata:
  package: python-delphi-lsp
---

## Hard rule

Inspect Delphi/Object Pascal only through `delphi_codebase`; never raw bash/read/glob/grep/cat/shell source inspection. Preserve one focused target and cite `path:line` evidence from tool output.

## Protocol v2 workflow

1. Call `open`. If the requested project is not active, select it with `focus(project_id)`.
2. Call `find` with a narrow query, then `focus(target_id)` for the returned target.
3. Inspect focused details in this order as needed: `summary`, `declaration`, `members`, `context`, `body`, `implementations`.
4. Trace relations with `references`, `callers`, `callees`, `uses`, `used_by`, `inherits`, or `implements`.
5. Call `problems` before declaring evidence missing. Explain any `sound_partial` relation metadata rather than treating partial results as complete.
6. Follow `page.next_cursor` with `cursor` until the needed evidence is available.

Prefer `summary` and `declaration`, narrow `max_items` and `max_chars`, and request `body` only when it is necessary. Keep the focused target stable while collecting evidence.

## Tool calls

`delphi_codebase` accepts `action` (`open`, `find`, `inspect`, `trace`, `focus`, `problems`), optional `query`, `target_id`, `project_id`, `detail`, `relation`, `cursor`, `max_items` (1–50), and `max_chars` (256–40000). It has no root or path argument; the active OpenCode worktree is used.
"""


def _opencode_plugin(python_executable: str) -> str:
    python_json = json.dumps(python_executable)
    template = '''import { tool, type Plugin } from "@opencode-ai/plugin"

const PYTHON = __PYTHON_EXECUTABLE__
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000

type AgentRequest = {
  action: "open" | "find" | "inspect" | "trace" | "focus" | "problems"
  query?: string
  target_id?: string
  project_id?: string
  detail?: "summary" | "declaration" | "members" | "context" | "body" | "implementations"
  relation?: "references" | "callers" | "callees" | "uses" | "used_by" | "inherits" | "implements"
  cursor?: string
  max_items?: number
  max_chars?: number
}

type AgentFocus = { project_id: string; unit_id: string; target_id: string }

type AgentSuccessResponse = {
  schema: 2
  workspace_revision: string
  focus: AgentFocus
  result: unknown
  page: Record<string, unknown>
  context: Record<string, unknown>
}

type AgentErrorResponse = {
  schema: 2
  error: { code: string; message: string }
}

type AgentResponse = AgentSuccessResponse | AgentErrorResponse

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function workspaceRoot(context: { worktree?: string; directory?: string }): string {
  if (context.directory && context.directory !== "/") return context.directory
  if (context.worktree && context.worktree !== "/") return context.worktree
  return process.cwd()
}

async function drainStderr(stream: ReadableStream<Uint8Array> | null): Promise<void> {
  if (!stream) return
  const reader = stream.getReader()
  let retained = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      retained = Math.min(65_536, retained + value.byteLength)
    }
  } catch {
    // Stderr is deliberately not surfaced to the model.
  } finally {
    reader.releaseLock()
  }
}

class WorkerProtocolError extends Error {
  readonly reusable: boolean

  constructor(message: string, reusable = true) {
    super(message)
    this.name = "WorkerProtocolError"
    this.reusable = reusable
  }
}

class WorkerTransportError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "WorkerTransportError"
  }
}

function parseAgentResponse(line: string): AgentResponse {
  let payload: unknown
  try {
    payload = JSON.parse(line)
  } catch {
    throw new WorkerProtocolError("delphi_codebase returned malformed JSON.")
  }

  if (!isRecord(payload) || payload.schema !== 2) {
    throw new WorkerProtocolError("delphi_codebase returned a malformed protocol response.")
  }
  if ("error" in payload) {
    if (
      !isRecord(payload.error)
      || typeof payload.error.code !== "string"
      || typeof payload.error.message !== "string"
    ) {
      throw new WorkerProtocolError("delphi_codebase returned a malformed protocol response.")
    }
    return payload as AgentErrorResponse
  }
  if (
    typeof payload.workspace_revision !== "string"
    || !isRecord(payload.focus)
    || typeof payload.focus.project_id !== "string"
    || typeof payload.focus.unit_id !== "string"
    || typeof payload.focus.target_id !== "string"
    || !("result" in payload)
    || !isRecord(payload.page)
    || !isRecord(payload.context)
  ) {
    throw new WorkerProtocolError("delphi_codebase returned a malformed protocol response.")
  }
  return payload as AgentSuccessResponse
}

class WorkerClient {
  private readonly proc: ReturnType<typeof Bun.spawn>
  private readonly reader: ReadableStreamDefaultReader<Uint8Array>
  private readonly decoder = new TextDecoder("utf-8", { fatal: true })
  private readonly requestTimeoutMs: number
  private buffered = ""
  private tail: Promise<unknown> = Promise.resolve()
  private closed = false

  constructor(root: string, requestTimeoutMs: number) {
    this.requestTimeoutMs = requestTimeoutMs
    const command = [PYTHON, "-m", "delphi_lsp.agent_cli", "worker", "--root", root]
    this.proc = Bun.spawn(command, {
      cwd: root,
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
      windowsHide: true,
    })
    this.reader = this.proc.stdout.getReader()
    void drainStderr(this.proc.stderr)
  }

  request(request: AgentRequest, signal?: AbortSignal): Promise<AgentResponse> {
    const run = async (): Promise<AgentResponse> => this.requestOne(request, signal)
    const queued = this.tail.then(run, run)
    this.tail = queued.catch(() => undefined)
    return queued
  }

  private async requestOne(request: AgentRequest, signal?: AbortSignal): Promise<AgentResponse> {
    if (this.closed) throw new WorkerTransportError("delphi_codebase worker is closed.")
    if (signal?.aborted) {
      this.close()
      throw new WorkerTransportError("delphi_codebase request was cancelled.")
    }

    let rejectInterruption: (error: WorkerTransportError) => void = () => undefined
    const interruption = new Promise<never>((_resolve, reject) => {
      rejectInterruption = reject
    })
    const interrupt = (message: string) => {
      rejectInterruption(new WorkerTransportError(message))
      this.close()
    }
    const timeout = setTimeout(
      () => interrupt("delphi_codebase request timed out."),
      this.requestTimeoutMs,
    )
    const abort = () => interrupt("delphi_codebase request was cancelled.")
    signal?.addEventListener("abort", abort, { once: true })
    try {
      const response = await Promise.race([this.exchange(request), interruption])
      if ("error" in response) {
        throw new WorkerProtocolError(`delphi_codebase: ${response.error.message}`)
      }
      return response
    } finally {
      clearTimeout(timeout)
      signal?.removeEventListener("abort", abort)
    }
  }

  private async exchange(request: AgentRequest): Promise<AgentResponse> {
    try {
      this.proc.stdin.write(JSON.stringify(request) + "\\n")
      await this.proc.stdin.flush()
    } catch {
      this.close()
      throw new WorkerTransportError("delphi_codebase worker write failed.")
    }
    return this.readLine()
  }

  private async readLine(): Promise<AgentResponse> {
    while (true) {
      const newline = this.buffered.indexOf("\\n")
      if (newline >= 0) {
        const rawLine = this.buffered.slice(0, newline)
        this.buffered = this.buffered.slice(newline + 1)
        const line = rawLine.endsWith("\\r") ? rawLine.slice(0, -1) : rawLine
        return parseAgentResponse(line)
      }

      let chunk: ReadableStreamReadResult<Uint8Array>
      try {
        chunk = await this.reader.read()
      } catch {
        throw new WorkerTransportError("delphi_codebase worker read failed.")
      }
      if (chunk.done) {
        try {
          this.buffered += this.decoder.decode()
        } catch {
          throw new WorkerProtocolError("delphi_codebase returned an incomplete response.", false)
        }
        if (this.buffered) {
          throw new WorkerProtocolError("delphi_codebase returned an incomplete response.", false)
        }
        throw new WorkerTransportError("delphi_codebase worker closed without a response.")
      }
      try {
        this.buffered += this.decoder.decode(chunk.value, { stream: true })
      } catch {
        throw new WorkerProtocolError("delphi_codebase returned malformed UTF-8.", false)
      }
    }
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    try { this.proc.stdin.end() } catch {}
    try { this.proc.kill() } catch {}
    void this.reader.cancel().catch(() => undefined)
  }
}

export const DelphiCodebasePlugin: Plugin = async (_input) => {
  const workers = new Map<string, Map<string, WorkerClient>>()

  function clientFor(sessionID: string, root: string): WorkerClient {
    let clients = workers.get(sessionID)
    if (!clients) {
      clients = new Map<string, WorkerClient>()
      workers.set(sessionID, clients)
    }
    let client = clients.get(root)
    if (!client) {
      client = new WorkerClient(root, DEFAULT_REQUEST_TIMEOUT_MS)
      clients.set(root, client)
    }
    return client
  }

  function removeClient(sessionID: string, root: string): void {
    const clients = workers.get(sessionID)
    if (!clients) return
    const client = clients.get(root)
    if (client) client.close()
    clients.delete(root)
    if (clients.size === 0) workers.delete(sessionID)
  }

  function closeSession(sessionID: string): void {
    const clients = workers.get(sessionID)
    if (!clients) return
    for (const client of clients.values()) client.close()
    workers.delete(sessionID)
  }

  return {
    tool: {
      delphi_codebase: tool({
        description: "Protocol v2 semantic Delphi/Object Pascal codebase navigation.",
        args: {
          action: tool.schema.enum(["open", "find", "inspect", "trace", "focus", "problems"]),
          query: tool.schema.string().optional(),
          target_id: tool.schema.string().optional(),
          project_id: tool.schema.string().optional(),
          detail: tool.schema.enum(["summary", "declaration", "members", "context", "body", "implementations"]).optional(),
          relation: tool.schema.enum(["references", "callers", "callees", "uses", "used_by", "inherits", "implements"]).optional(),
          cursor: tool.schema.string().optional(),
          max_items: tool.schema.number().int().min(1).max(50).optional(),
          max_chars: tool.schema.number().int().min(256).max(40_000).optional(),
        },
        async execute(args, context) {
          const root = workspaceRoot(context)
          const client = clientFor(context.sessionID, root)
          try {
            return JSON.stringify(await client.request(args as AgentRequest, context.abort))
          } catch (error) {
            if (!(error instanceof WorkerProtocolError && error.reusable)) {
              removeClient(context.sessionID, root)
            }
            throw error instanceof Error ? error : new Error("delphi_codebase worker unavailable.")
          }
        },
      }),
    },
    event: async ({ event }) => {
      if (event.type !== "session.deleted") return
      closeSession(event.properties.info.id)
    },
    "experimental.session.compacting": async ({ sessionID }, output) => {
      const clients = workers.get(sessionID)
      if (!clients) return
      for (const [root, client] of [...clients.entries()]) {
        try {
          const current = await client.request({ action: "focus", max_items: 1, max_chars: 1_000 })
          if ("error" in current || !current.focus.target_id) continue
          const summary = await client.request({ action: "inspect", detail: "summary", max_items: 1, max_chars: 4_000 })
          if ("error" in summary) continue
          const target = Array.isArray(summary.result) ? summary.result[0] : summary.result
          output.context.push(`Delphi navigator context: workspace_revision=${summary.workspace_revision}; focus=${JSON.stringify(summary.focus)}; target=${JSON.stringify(target)}. Continue via delphi_codebase.`)
          return
        } catch (error) {
          if (!(error instanceof WorkerProtocolError && error.reusable)) {
            removeClient(sessionID, root)
          }
          // Compaction lookup failures must not disrupt a session.
        }
      }
    },
    dispose: async () => {
      for (const clients of workers.values()) {
        for (const client of clients.values()) client.close()
      }
      workers.clear()
    },
  }
}
'''
    return template.replace("__PYTHON_EXECUTABLE__", python_json)


def _render_opencode_config(config_path: Path) -> tuple[bytes | None, str]:
    if config_path.exists():
        if not config_path.is_file():
            raise ValueError(f"opencode config path is not a file: {config_path}")
        config_before = config_path.read_bytes()
        config = json.loads(config_before.decode("utf-8"))
    else:
        config_before = None
        config = {"$schema": "https://opencode.ai/config.json"}
    if not isinstance(config, dict):
        raise ValueError("opencode config must be a JSON object")
    if "agent" not in config:
        agents = {}
        config["agent"] = agents
    else:
        agents = config["agent"]
    if not isinstance(agents, dict):
        raise ValueError("opencode config field 'agent' must be a JSON object")
    agents["vllm-delphi-codebase"] = {
        "description": "Use the Delphi codebase navigator skill and plugin without direct filesystem source inspection.",
        "temperature": 0,
        "prompt": (
            "load delphi-codebase-navigator first, then use only delphi_codebase for Delphi/Object Pascal "
            "codebase inspection. Do not use lsp, bash, read, glob, grep, edit, write, task, webfetch, or todowrite."
        ),
        "tools": {
            "delphi_codebase": True,
            "skill": True,
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
            "skill": {
                "*": "deny",
                SKILL_NAME: "allow",
            },
            "lsp": "deny",
        },
    }
    return config_before, json.dumps(config, indent=2, sort_keys=True) + "\n"


__all__ = ["SKILL_NAME", "install_opencode_support", "install_skill"]
