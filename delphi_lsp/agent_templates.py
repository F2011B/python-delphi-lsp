from __future__ import annotations

import hashlib
from pathlib import Path
import json
import os
import secrets
import stat


SKILL_NAME = "python-delphi-lsp"
AGENT_NAME = "python-delphi-lsp"
_LEGACY_SKILL_NAME = "delphi-codebase-navigator"
_LEGACY_SKILL_RELATIVE_PATH = Path(".agents") / "skills" / _LEGACY_SKILL_NAME / "SKILL.md"
_LEGACY_SKILL_SHA256 = "0843d37bd48431c2b992f191b985175a3c60fe5b56d28e0a46b5e18db6383b28"
_LEGACY_TOOL_RELATIVE_PATH = Path(".opencode") / "tools" / "delphi_codebase.ts"


def install_skill(target: str | Path, *, force: bool = False) -> Path:
    target_path = Path(target).expanduser().resolve()
    legacy_path = _preflight_legacy_skill(target_path, force=force)
    skill_path = target_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    _write_text(skill_path, _skill_markdown(), force=force)
    _remove_legacy_skill(legacy_path)
    return skill_path


def install_opencode_support(
    target: str | Path,
    *,
    python_executable: str,
    force: bool = False,
    write_config: bool = False,
) -> tuple[Path, Path, Path]:
    target_path = Path(target).expanduser().resolve()
    legacy_path = target_path / _LEGACY_TOOL_RELATIVE_PATH
    legacy_skill_path = _preflight_legacy_skill(target_path, force=force)
    skill_path = target_path / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    plugin_path = target_path / ".opencode" / "plugins" / "delphi_codebase.ts"
    agent_path = target_path / ".opencode" / "agents" / f"{AGENT_NAME}.md"
    skill_text = _skill_markdown()
    plugin_text = _opencode_plugin(python_executable)
    agent_text = _agent_markdown()

    legacy_before = _preflight_legacy(legacy_path, force=force)
    snapshots: dict[Path, bytes | None] = {
        legacy_path: legacy_before,
        **(
            {legacy_skill_path: legacy_skill_path.read_bytes()}
            if legacy_skill_path is not None
            else {}
        ),
        skill_path: _preflight_destination(skill_path, skill_text, force=force),
        plugin_path: _preflight_destination(plugin_path, plugin_text, force=force),
        agent_path: _preflight_destination(agent_path, agent_text, force=force),
    }
    writes = [(skill_path, skill_text), (plugin_path, plugin_text), (agent_path, agent_text)]

    try:
        for path, text in writes:
            if snapshots[path] != text.encode("utf-8"):
                _write_text(path, text, force=True)
        if legacy_before is not None:
            legacy_path.unlink()
        _remove_legacy_skill(legacy_skill_path)
    except BaseException:
        for path, content in reversed(tuple(snapshots.items())):
            _restore_file(path, content)
        raise
    _ = write_config  # Deprecated compatibility input; user configuration is never touched.
    return skill_path, plugin_path, agent_path


def _preflight_legacy_skill(target: Path, *, force: bool) -> Path | None:
    legacy_path = target / _LEGACY_SKILL_RELATIVE_PATH
    legacy_path.relative_to(target)
    _reject_symbolic_link(legacy_path)
    if not legacy_path.exists():
        return None
    if not legacy_path.is_file():
        raise FileExistsError(f"Legacy skill path is not a file: {legacy_path}")
    digest = hashlib.sha256(legacy_path.read_bytes()).hexdigest()
    if digest != _LEGACY_SKILL_SHA256 and not force:
        raise FileExistsError(
            f"Refusing to remove modified legacy skill without force: {legacy_path}"
        )
    return legacy_path


def _remove_legacy_skill(path: Path | None) -> None:
    if path is None:
        return
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _preflight_legacy(legacy_path: Path, *, force: bool) -> bytes | None:
    _reject_symbolic_link(legacy_path)
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
    _reject_symbolic_link(path)
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
    _write_bytes_atomic(path, content)


def _is_generated_legacy_tool(text: str) -> bool:
    return (
        "@opencode-ai/plugin" in text
        and "layered semantic views" in text
        and "python-delphi-lsp" in text
    )


def _write_text(path: Path, text: str, *, force: bool) -> None:
    content = text.encode("utf-8")
    _reject_symbolic_link(path)
    if path.exists() and not path.is_file():
        raise FileExistsError(f"Generated destination is not a file: {path}")
    if path.exists() and path.read_bytes() != content and not force:
        raise FileExistsError(f"{path} already exists with different content; pass --force to overwrite")
    _write_bytes_atomic(path, content)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symbolic_link(path)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    temporary_path: Path | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for _attempt in range(10):
            candidate = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
            try:
                descriptor = os.open(candidate, flags, 0o666)
            except FileExistsError:
                continue
            temporary_path = candidate
            break
        else:
            raise FileExistsError(f"Could not allocate a staging file for {path}")
        try:
            staged_file = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with staged_file as staged:
            written = staged.write(content)
            if written != len(content):
                raise OSError(f"Could not stage complete content for {path}")
            staged.flush()
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            os.fsync(staged.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _reject_symbolic_link(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise FileExistsError(
                f"Generated destination must not contain a symbolic link: {current}"
            )
        current = current.parent


def _skill_markdown() -> str:
    return """---
name: python-delphi-lsp
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
6. Call `metrics` for project LOC and architecture metrics. Use `query` for unit summaries or a unit `target_id` with `detail=members` for full Halstead, complexity, coupling, abstractness, instability, and distance details.
7. Follow `page.next_cursor` with `cursor` until the needed evidence is available.

Prefer `summary` and `declaration`, narrow `max_items` and `max_chars`, and request `body` only when it is necessary. Keep the focused target stable while collecting evidence.

## Tool calls

`delphi_codebase` accepts `action` (`open`, `find`, `inspect`, `trace`, `focus`, `problems`, `metrics`), optional `query`, `target_id`, `project_id`, `detail`, `relation`, `cursor`, `max_items` (1-50), and `max_chars` (256-40000). It has no root or path argument; the active OpenCode worktree is used.
"""


def _agent_markdown() -> str:
    return """---
description: Inspect Delphi and Object Pascal codebases through python-delphi-lsp.
mode: all
temperature: 0
permission:
  delphi_codebase: allow
  skill:
    "*": deny
    python-delphi-lsp: allow
  lsp: deny
  bash: deny
  read: deny
  glob: deny
  grep: deny
  list: deny
  edit: deny
  write: deny
  patch: deny
  task: deny
  webfetch: deny
  websearch: deny
  question: deny
  todowrite: deny
  todoread: deny
  codebase_map: deny
  code_guidelines: deny
---

Load `python-delphi-lsp` first, then use only `delphi_codebase` for Delphi and
Object Pascal codebase inspection. Do not use `lsp`, `bash`, `read`, `glob`,
`grep`, `edit`, `write`, `task`, `webfetch`, or `todowrite`. Preserve returned
citations exactly and report partial or ambiguous semantic evidence explicitly.
"""


def _opencode_plugin(python_executable: str) -> str:
    python_json = json.dumps(python_executable)
    template = '''import { tool, type Plugin } from "@opencode-ai/plugin"

const PYTHON = __PYTHON_EXECUTABLE__
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000

type AgentRequest = {
  action: "open" | "find" | "inspect" | "trace" | "focus" | "problems" | "metrics"
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
          action: tool.schema.enum(["open", "find", "inspect", "trace", "focus", "problems", "metrics"]),
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


__all__ = ["SKILL_NAME", "install_opencode_support", "install_skill"]
