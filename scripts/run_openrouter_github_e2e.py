#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROBE = REPO_ROOT / "scripts" / "run_opencode_lsp_probe.py"
DEFAULT_MODEL = "openrouter/google/gemma-4-31b-it"
DEFAULT_AGENT = "python-delphi-lsp"
DEFAULT_OPENCODE = "opencode"
DEFAULT_OPENCODE_VERSION = "1.17.18"
FORBIDDEN_TOOLS = ("bash", "read", "grep", "glob", "list", "invalid")
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|secret|token)",
    re.IGNORECASE,
)


class E2EValidationError(RuntimeError):
    """A release-blocking E2E evidence validation failure."""


@dataclass(frozen=True)
class TargetEvidence:
    name: str
    relative_path: str
    line: int
    source_line: str
    sha256: str

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.line}"


@dataclass(frozen=True)
class TranscriptEvidence:
    target_id: str
    tools: list[str]
    actions: list[str]
    elapsed_ms: dict[str, int | None]
    final_response: str


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the GitHub corpus OpenRouter E2E release gate.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--target-name", required=True)
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--target-line", required=True, type=int)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--agent", default=DEFAULT_AGENT)
    parser.add_argument("--opencode", default=DEFAULT_OPENCODE)
    parser.add_argument("--expected-opencode-version", default=DEFAULT_OPENCODE_VERSION)
    parser.add_argument("--timeout", default=300.0, type=float)
    parser.add_argument("--probe-script", default=DEFAULT_PROBE, type=Path)
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--xdg-data-home", type=Path)
    parser.add_argument("--xdg-cache-home", type=Path)
    parser.add_argument("--xdg-state-home", type=Path)
    parser.add_argument("--npm-cache", type=Path)
    parser.add_argument("--raw-jsonl", default="opencode.jsonl")
    parser.add_argument("--session-export-json", default="session-export.json")
    parser.add_argument("--summary-json", default="summary.json")
    return parser.parse_args(argv)


def _prompt(args: argparse.Namespace) -> str:
    citation = f"{args.target_path}:{args.target_line}"
    return (
        "Use one tool per turn and exactly this sequence: skill, open, find, focus, inspect. "
        "Load skill python-delphi-lsp. Then call delphi_codebase open. "
        f"Find {args.target_name}, focus its returned target_id, and inspect its declaration. "
        "Do not use any other tools. "
        f"Finish with exactly the symbol name and citation {citation}."
    )


def build_probe_command(args: argparse.Namespace, raw_jsonl: Path) -> list[str]:
    command = [
        str(args.python_executable),
        str(args.probe_script),
        _prompt(args),
        "--cwd",
        str(args.workspace),
        "--model",
        args.model,
        "--agent",
        args.agent,
        "--opencode",
        args.opencode,
        "--inherit-process-group",
        "--title",
        "python-delphi-lsp-github-2m-openrouter",
        "--timeout",
        str(args.timeout),
        "--output",
        str(raw_jsonl),
        "--exact-tools",
        "--require-tool",
        "skill:python-delphi-lsp",
        "--require-tool",
        'delphi_codebase.open:"schema":2',
        "--require-tool",
        f"delphi_codebase.find:{args.target_name}",
        "--require-tool",
        "delphi_codebase.focus:target_id",
        "--require-tool",
        f"delphi_codebase.inspect:{args.target_name}",
        "--require-final",
        args.target_name,
        "--require-final",
        args.target_path,
        "--require-final",
        str(args.target_line),
    ]
    for tool in FORBIDDEN_TOOLS:
        command.extend(["--forbid-tool", tool])
    return command


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_target(
    *,
    workspace: Path,
    manifest_path: Path,
    name: str,
    relative_path: str,
    line: int,
) -> TargetEvidence:
    workspace = workspace.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if not workspace.is_dir():
        raise E2EValidationError(f"workspace is not a directory: {workspace}")
    if not manifest_path.is_file():
        raise E2EValidationError(f"manifest is not a file: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise E2EValidationError(f"could not read corpus manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise E2EValidationError("corpus manifest schema_version must be 1")
    if not isinstance(name, str) or not name.strip():
        raise E2EValidationError("target name must be non-empty")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise E2EValidationError("target line must be a positive integer")

    normalized = Path(relative_path).as_posix()
    if not relative_path or Path(relative_path).is_absolute() or normalized != relative_path:
        raise E2EValidationError("target path must be a normalized relative POSIX path")
    source = (workspace / relative_path).resolve()
    if not source.is_relative_to(workspace):
        raise E2EValidationError("target path escapes the workspace")

    records: list[dict[str, object]] = []
    corpora = manifest.get("corpora")
    if not isinstance(corpora, list):
        raise E2EValidationError("corpus manifest has no corpora list")
    for corpus in corpora:
        if not isinstance(corpus, dict) or not isinstance(corpus.get("files"), list):
            raise E2EValidationError("corpus manifest contains invalid file records")
        records.extend(
            record
            for record in corpus["files"]
            if isinstance(record, dict) and record.get("path") == relative_path
        )
    if len(records) != 1:
        raise E2EValidationError(
            f"target path must occur exactly once in corpus manifest: {relative_path}"
        )
    record = records[0]
    expected_hash = record.get("sha256")
    expected_lines = record.get("lines")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise E2EValidationError("target manifest record has an invalid SHA-256 hash")
    if not source.is_file():
        raise E2EValidationError(f"manifest target file is missing: {relative_path}")
    actual_hash = _sha256(source)
    if actual_hash != expected_hash:
        raise E2EValidationError(f"target file hash does not match manifest: {relative_path}")
    try:
        source_lines = source.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeError) as error:
        raise E2EValidationError(f"could not read target source: {error}") from error
    if expected_lines != len(source_lines):
        raise E2EValidationError(f"target file line count does not match manifest: {relative_path}")
    if line > len(source_lines):
        raise E2EValidationError(f"target line is outside source file: {line}")
    source_line = source_lines[line - 1]
    if name not in source_line:
        raise E2EValidationError(f"target name is absent from source line {relative_path}:{line}")
    return TargetEvidence(
        name=name,
        relative_path=relative_path,
        line=line,
        source_line=source_line,
        sha256=actual_hash,
    )


def _tool_parts(events: Iterable[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    parts: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            raise E2EValidationError("tool event has no part object")
        parts.append((index, part))
    return parts


def _state(part: dict[str, Any], action: str) -> tuple[dict[str, Any], str, int | None]:
    state = part.get("state")
    if not isinstance(state, dict):
        raise E2EValidationError(f"{action} tool call has no state")
    if state.get("status") != "completed":
        raise E2EValidationError(f"{action} tool call did not complete with status completed")
    tool_input = state.get("input")
    if not isinstance(tool_input, dict) or not tool_input:
        raise E2EValidationError(f"{action} tool call has empty input")
    output = state.get("output")
    if not isinstance(output, str) or not output:
        raise E2EValidationError(f"{action} tool call has empty output")
    timing = state.get("time")
    elapsed: int | None = None
    if isinstance(timing, dict):
        start = timing.get("start")
        end = timing.get("end")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            elapsed = end - start
    return tool_input, output, elapsed


def _json_output(output: str, action: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise E2EValidationError(f"{action} output is not JSON") from error
    if not isinstance(payload, dict):
        raise E2EValidationError(f"{action} output is not a JSON object")
    return payload


def _result_records(payload: dict[str, Any], action: str) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, list):
        raise E2EValidationError(f"{action} output has no result list")
    return [record for record in result if isinstance(record, dict)]


def validate_transcript(
    events: Iterable[dict[str, Any]],
    target: TargetEvidence,
    *,
    expected_agent: str = DEFAULT_AGENT,
) -> TranscriptEvidence:
    materialized = list(events)
    parts = _tool_parts(materialized)
    for _index, part in parts:
        tool = part.get("tool")
        if tool in FORBIDDEN_TOOLS:
            raise E2EValidationError(f"forbidden tool call: {tool}")
    if len(parts) != 5:
        raise E2EValidationError(f"transcript must contain exactly five tool calls; found {len(parts)}")

    expected_tools = ["skill", *("delphi_codebase" for _ in range(4))]
    actual_tools = [str(part.get("tool") or "") for _index, part in parts]
    if actual_tools != expected_tools:
        raise E2EValidationError(f"unexpected tool sequence: {actual_tools}")
    expected_actions = ["skill", "open", "find", "focus", "inspect"]
    actual_actions: list[str] = []
    elapsed_by_action: dict[str, int | None] = {}

    skill_input, skill_output, elapsed = _state(parts[0][1], "skill")
    if skill_input.get("name") != expected_agent:
        raise E2EValidationError("skill call must load python-delphi-lsp")
    if expected_agent not in skill_output:
        raise E2EValidationError("skill output does not prove python-delphi-lsp was loaded")
    actual_actions.append("skill")
    elapsed_by_action["skill"] = elapsed

    decoded: dict[str, dict[str, Any]] = {}
    inputs: dict[str, dict[str, Any]] = {}
    for expected_action, (_event_index, part) in zip(expected_actions[1:], parts[1:], strict=True):
        tool_input, output, elapsed = _state(part, expected_action)
        if tool_input.get("action") != expected_action:
            raise E2EValidationError(
                f"expected delphi_codebase {expected_action}, got {tool_input.get('action')!r}"
            )
        inputs[expected_action] = tool_input
        decoded[expected_action] = _json_output(output, expected_action)
        actual_actions.append(expected_action)
        elapsed_by_action[expected_action] = elapsed

    if inputs["find"].get("query") != target.name:
        raise E2EValidationError(f"find query must be exactly {target.name}")
    find_matches = [
        record
        for record in _result_records(decoded["find"], "find")
        if record.get("name") == target.name
        and record.get("path") == target.relative_path
        and record.get("line") == target.line
        and isinstance(record.get("target_id"), str)
        and record.get("target_id")
    ]
    if len(find_matches) != 1:
        raise E2EValidationError("find output does not contain exactly one manifest-backed target")
    target_id = str(find_matches[0]["target_id"])

    if inputs["focus"].get("target_id") != target_id:
        raise E2EValidationError("focus target_id does not match the find result")
    focus = decoded["focus"].get("focus")
    if not isinstance(focus, dict) or focus.get("target_id") != target_id:
        raise E2EValidationError("focus output does not confirm the find target_id")

    if inputs["inspect"].get("target_id") != target_id:
        raise E2EValidationError("inspect target_id does not match the find result")
    if inputs["inspect"].get("detail") != "declaration":
        raise E2EValidationError("inspect detail must be declaration")
    inspect_matches: list[dict[str, Any]] = []
    for record in _result_records(decoded["inspect"], "inspect"):
        start_line = record.get("start_line")
        end_line = record.get("end_line")
        text = record.get("text")
        if (
            record.get("path") == target.relative_path
            and record.get("target_id") == target_id
            and isinstance(start_line, int)
            and isinstance(end_line, int)
            and start_line <= target.line <= end_line
            and isinstance(text, str)
            and target.name in text
            and target.source_line.strip() in text
        ):
            inspect_matches.append(record)
    if len(inspect_matches) != 1:
        raise E2EValidationError("inspect output is not backed by the target source line")

    final_tool_index = parts[-1][0]
    final_texts = [
        str((event.get("part") or {}).get("text") or "").strip()
        for index, event in enumerate(materialized)
        if index > final_tool_index and isinstance(event, dict) and event.get("type") == "text"
    ]
    final_texts = [text for text in final_texts if text]
    if not final_texts:
        raise E2EValidationError("transcript has no final response after inspect")
    final_response = final_texts[-1]
    if target.name not in final_response or target.citation not in final_response:
        raise E2EValidationError("final response is missing the manifest-backed name or citation")

    return TranscriptEvidence(
        target_id=target_id,
        tools=actual_tools,
        actions=actual_actions,
        elapsed_ms=elapsed_by_action,
        final_response=final_response,
    )


def _is_error_event(value: Any) -> bool:
    if isinstance(value, list):
        return any(_is_error_event(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("type") == "error":
        return True
    if value.get("finish") == "error" or value.get("reason") == "error":
        return True
    if value.get("error") not in (None, False, "", {}):
        return True
    return any(_is_error_event(item) for item in value.values())


def _session_ids(value: Any) -> set[str]:
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_session_ids(item))
        return result
    if not isinstance(value, dict):
        return set()
    result = {
        item
        for key, item in value.items()
        if key == "sessionID" and isinstance(item, str) and item
    }
    for item in value.values():
        result.update(_session_ids(item))
    return result


def validate_stream(events: Iterable[dict[str, Any]]) -> str:
    materialized = list(events)
    session_ids = _session_ids(materialized)
    if len(session_ids) != 1 or any(
        not isinstance(event, dict) or event.get("sessionID") not in session_ids
        for event in materialized
    ):
        raise E2EValidationError("transcript must contain exactly one sessionID on every event")
    if _is_error_event(materialized):
        raise E2EValidationError("transcript contains an error event")
    return next(iter(session_ids))


def _model_identity(model: str) -> tuple[str, str]:
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise E2EValidationError("model must use provider/model format")
    return provider, model_id


def validate_session_export(
    payload: Any,
    *,
    session_id: str,
    target: TargetEvidence,
    expected_version: str,
    expected_agent: str,
    expected_model: str,
) -> TranscriptEvidence:
    if not isinstance(payload, dict):
        raise E2EValidationError("OpenCode session export is not an object")
    if _is_error_event(payload):
        raise E2EValidationError("OpenCode session export contains an error event")
    info = payload.get("info")
    if not isinstance(info, dict):
        raise E2EValidationError("OpenCode session export has no info object")
    if info.get("id") != session_id:
        raise E2EValidationError("OpenCode session export ID does not match the transcript sessionID")
    if _session_ids(payload) != {session_id}:
        raise E2EValidationError("OpenCode session export contains another sessionID")
    if info.get("version") != expected_version:
        raise E2EValidationError(
            f"OpenCode session version must be exactly {expected_version}"
        )
    if info.get("agent") != expected_agent:
        raise E2EValidationError(f"OpenCode session agent must be exactly {expected_agent}")
    expected_provider, expected_model_id = _model_identity(expected_model)
    model = info.get("model")
    if not isinstance(model, dict) or model.get("providerID") != expected_provider:
        raise E2EValidationError(
            f"OpenCode session provider must be exactly {expected_provider}"
        )
    if model.get("id") != expected_model_id:
        raise E2EValidationError(
            f"OpenCode session model must be exactly {expected_model_id}"
        )

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise E2EValidationError("OpenCode session export has no messages")
    last_message = messages[-1]
    last_info = last_message.get("info") if isinstance(last_message, dict) else None
    if (
        not isinstance(last_info, dict)
        or last_info.get("role") != "assistant"
        or last_info.get("finish") != "stop"
    ):
        raise E2EValidationError(
            "OpenCode messages[-1] must be the sole assistant terminal finish=stop response"
        )
    assistant_infos: list[dict[str, Any]] = []
    transcript_events: list[dict[str, Any]] = []
    terminal_texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            raise E2EValidationError("OpenCode session export contains an invalid message")
        message_info = message.get("info")
        parts = message.get("parts")
        if not isinstance(message_info, dict) or not isinstance(parts, list):
            raise E2EValidationError("OpenCode session export message is incomplete")
        if message_info.get("sessionID") != session_id:
            raise E2EValidationError("OpenCode session export contains another sessionID")
        role = message_info.get("role")
        if role == "user":
            user_model = message_info.get("model")
            if message_info.get("agent") != expected_agent:
                raise E2EValidationError("OpenCode user message has an unexpected agent")
            if (
                not isinstance(user_model, dict)
                or user_model.get("providerID") != expected_provider
                or user_model.get("modelID") != expected_model_id
            ):
                raise E2EValidationError("OpenCode user message has an unexpected provider or model")
        elif role == "assistant":
            assistant_infos.append(message_info)
            if message_info.get("agent") != expected_agent or message_info.get("mode") != expected_agent:
                raise E2EValidationError("OpenCode assistant message has an unexpected agent")
            if (
                message_info.get("providerID") != expected_provider
                or message_info.get("modelID") != expected_model_id
            ):
                raise E2EValidationError(
                    "OpenCode assistant message has an unexpected provider or model"
                )
            for part in parts:
                if not isinstance(part, dict):
                    raise E2EValidationError("OpenCode session export contains an invalid part")
                if part.get("sessionID") != session_id:
                    raise E2EValidationError("OpenCode session export part has another sessionID")
                if part.get("type") == "tool":
                    transcript_events.append({"type": "tool_use", "part": part})
                elif part.get("type") == "text" and message_info.get("finish") == "stop":
                    text = str(part.get("text") or "").strip()
                    if text:
                        terminal_texts.append(text)
        else:
            raise E2EValidationError(f"OpenCode session export has unexpected role: {role!r}")

    if not assistant_infos:
        raise E2EValidationError("OpenCode session export has no assistant messages")
    finishes = [message_info.get("finish") for message_info in assistant_infos]
    if finishes[-1] != "stop" or finishes.count("stop") != 1:
        raise E2EValidationError("OpenCode session must end with exactly one normal finish=stop")
    if any(finish not in {"tool-calls", "stop"} for finish in finishes):
        raise E2EValidationError("OpenCode session contains a non-normal assistant finish")
    if len(terminal_texts) != 1:
        raise E2EValidationError(
            "OpenCode terminal assistant message with finish=stop must contain exactly one response"
        )
    transcript_events.append(
        {"type": "text", "part": {"text": terminal_texts[0]}}
    )
    return validate_transcript(
        transcript_events,
        target,
        expected_agent=expected_agent,
    )


def _artifact_path(artifact_dir: Path, configured: str) -> Path:
    path = Path(configured).expanduser()
    return path.resolve() if path.is_absolute() else (artifact_dir / path).resolve()


def _state_path(configured: Path | None, default: Path) -> Path:
    return (configured or default).expanduser().resolve()


def _validate_retained_state_paths(
    configured: dict[str, Path | None],
    resolved: dict[str, Path],
    artifact_dir: Path,
) -> None:
    for key, configured_path in configured.items():
        if configured_path is None:
            continue
        state_path = resolved[key]
        if state_path == artifact_dir or state_path.is_relative_to(artifact_dir):
            raise E2EValidationError(
                f"{key} must not be equal to or inside the retained artifact directory"
            )


def _secret_values(env: dict[str, str]) -> list[str]:
    marker = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|AUTH)", re.IGNORECASE)
    return sorted(
        {value for key, value in env.items() if marker.search(key) and value},
        key=len,
        reverse=True,
    )


def _redact(text: str, env: dict[str, str]) -> str:
    redacted = text
    for value in _secret_values(env):
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", redacted)
    redacted = re.sub(r"\bsk-or-v1-[A-Za-z0-9_-]+", "[REDACTED]", redacted)
    return redacted


def _redact_payload(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact(value, env)
    if isinstance(value, list):
        return [_redact_payload(item, env) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted_key = _redact(key_text, env)
            redacted[redacted_key] = (
                "[REDACTED]"
                if _SECRET_KEY.search(key_text)
                else _redact_payload(item, env)
            )
        return redacted
    return value


def _discard_sensitive_file(path: Path) -> None:
    if path.is_symlink():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        size = path.stat().st_size
        with path.open("r+b", buffering=0) as stream:
            remaining = size
            zeroes = b"\0" * min(1024 * 1024, max(1, size))
            while remaining:
                chunk = zeroes[: min(len(zeroes), remaining)]
                stream.write(chunk)
                remaining -= len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_sensitive_tree(root: Path) -> None:
    if not root.exists():
        return
    for directory, child_directories, files in os.walk(
        root,
        topdown=False,
        followlinks=False,
    ):
        parent = Path(directory)
        for name in files:
            _discard_sensitive_file(parent / name)
        for name in child_directories:
            child = parent / name
            try:
                if child.is_symlink():
                    child.unlink(missing_ok=True)
                else:
                    child.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass
    if root.exists():
        try:
            shutil.rmtree(root)
        except OSError as error:
            raise RuntimeError(f"could not remove ephemeral state directory: {root}") from error


def materialize_redacted_jsonl(
    raw_path: Path,
    artifact_path: Path,
    env: dict[str, str],
) -> None:
    redacted_events: list[Any] = []
    try:
        if raw_path.is_file():
            try:
                with raw_path.open("r", encoding="utf-8") as source:
                    for line_number, line in enumerate(source, start=1):
                        if not line.strip():
                            continue
                        try:
                            event: Any = json.loads(line)
                        except json.JSONDecodeError:
                            event = {
                                "type": "invalid_jsonl",
                                "line": line_number,
                                "raw": line.rstrip("\r\n"),
                            }
                        redacted_events.append(_redact_payload(event, env))
            except (OSError, UnicodeError) as error:
                redacted_events.append(
                    _redact_payload(
                        {"type": "artifact_error", "error": str(error)},
                        env,
                    )
                )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(
            json.dumps(event, sort_keys=True) + "\n" for event in redacted_events
        )
        artifact_path.write_text(text, encoding="utf-8")
    finally:
        _discard_sensitive_file(raw_path)


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise E2EValidationError(f"probe did not produce raw JSONL: {path}")
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise E2EValidationError(f"JSONL event {line_number} is not an object")
                events.append(event)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise E2EValidationError(f"could not parse raw JSONL: {error}") from error
    if not events:
        raise E2EValidationError("raw JSONL contains no events")
    return events


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[str],
    process_group_id: int,
    timeout: float,
) -> bool:
    deadline = monotonic() + timeout
    while True:
        process.poll()
        if not _process_group_exists(process_group_id):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.01, remaining))


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float = 0.2,
) -> None:
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=grace_seconds)
        return
    if not _wait_for_process_group_exit(process, process_group_id, grace_seconds):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_for_process_group_exit(process, process_group_id, grace_seconds):
            raise RuntimeError(
                f"process group {process_group_id} survived SIGKILL"
            )
    process.wait(timeout=grace_seconds)


def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        raise
    _terminate_process_group(process)
    return subprocess.CompletedProcess(
        list(command),
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def run_e2e(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.expanduser().resolve()
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest is not None
        else workspace / "corpus-manifest.json"
    )
    artifact_dir = args.artifact_dir.expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    raw_jsonl = _artifact_path(artifact_dir, args.raw_jsonl)
    session_export_json = _artifact_path(artifact_dir, args.session_export_json)
    summary_json = _artifact_path(artifact_dir, args.summary_json)
    raw_jsonl.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp_name = tempfile.mkstemp(
        prefix=".opencode-",
        suffix=".unredacted.jsonl",
        dir=raw_jsonl.parent,
    )
    os.close(descriptor)
    raw_temp = Path(raw_temp_name)
    raw_temp.chmod(0o600)
    ephemeral_state_root = Path(
        tempfile.mkdtemp(prefix="python-delphi-lsp-e2e-state-")
    ).resolve()

    env = os.environ.copy()
    configured_state_paths = {
        "XDG_DATA_HOME": args.xdg_data_home,
        "XDG_CACHE_HOME": args.xdg_cache_home,
        "XDG_STATE_HOME": args.xdg_state_home,
        "NPM_CONFIG_CACHE": args.npm_cache,
    }
    state_paths = {
        "XDG_DATA_HOME": _state_path(args.xdg_data_home, ephemeral_state_root / "data"),
        "XDG_CACHE_HOME": _state_path(args.xdg_cache_home, ephemeral_state_root / "cache"),
        "XDG_STATE_HOME": _state_path(args.xdg_state_home, ephemeral_state_root / "state"),
        "NPM_CONFIG_CACHE": _state_path(args.npm_cache, ephemeral_state_root / "npm-cache"),
    }

    base_summary: dict[str, Any] = {
        "agent": args.agent,
        "forbidden_tools": list(FORBIDDEN_TOOLS),
        "manifest": str(manifest_path),
        "model": args.model,
        "opencode": args.opencode,
        "opencode_version": args.expected_opencode_version,
        "raw_jsonl": str(raw_jsonl),
        "session_export": str(session_export_json),
        "workspace": str(workspace),
    }
    try:
        _write_summary(session_export_json, {"status": "not-run"})
        _validate_retained_state_paths(
            configured_state_paths,
            state_paths,
            artifact_dir,
        )
        for key, path in state_paths.items():
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        if not args.probe_script.expanduser().resolve().is_file():
            raise E2EValidationError(f"probe script is missing: {args.probe_script}")
        target = validate_target(
            workspace=workspace,
            manifest_path=manifest_path,
            name=args.target_name,
            relative_path=args.target_path,
            line=args.target_line,
        )
        try:
            version_result = run_command(
                [args.opencode, "--version"],
                cwd=workspace,
                env=env,
                timeout=min(15.0, args.timeout + 5.0),
            )
        except subprocess.TimeoutExpired as error:
            raise E2EValidationError("OpenCode version check timed out") from error
        except OSError as error:
            raise E2EValidationError(f"could not run OpenCode version check: {error}") from error
        if version_result.returncode != 0:
            detail = _redact((version_result.stderr or version_result.stdout or "").strip(), env)
            suffix = f": {detail}" if detail else ""
            raise E2EValidationError(
                f"OpenCode version check exited with status {version_result.returncode}{suffix}"
            )
        actual_version = version_result.stdout.strip()
        if actual_version != args.expected_opencode_version:
            raise E2EValidationError(
                "OpenCode version must be exactly "
                f"{args.expected_opencode_version}; found {actual_version or '<empty>'}"
            )

        command = build_probe_command(args, raw_temp)
        command.extend(["--npm-cache", str(state_paths["NPM_CONFIG_CACHE"])])
        try:
            result = run_command(
                command,
                cwd=workspace,
                env=env,
                timeout=args.timeout + 5.0,
            )
        except subprocess.TimeoutExpired as error:
            raise E2EValidationError(f"probe exceeded subprocess timeout {args.timeout + 5.0:g}s") from error
        except OSError as error:
            raise E2EValidationError(f"could not run probe: {error}") from error
        if result.returncode != 0:
            detail = _redact((result.stderr or result.stdout or "").strip(), env)
            suffix = f": {detail}" if detail else ""
            raise E2EValidationError(f"probe exited with status {result.returncode}{suffix}")
        raw_events = _read_jsonl(raw_temp)
        session_id = validate_stream(raw_events)
        probe_transcript = validate_transcript(
            raw_events,
            target,
            expected_agent=args.agent,
        )

        export_command = [args.opencode, "export", session_id]
        try:
            export_result = run_command(
                export_command,
                cwd=workspace,
                env=env,
                timeout=min(30.0, args.timeout + 5.0),
            )
        except subprocess.TimeoutExpired as error:
            _write_summary(session_export_json, {"status": "timeout"})
            raise E2EValidationError("OpenCode session export timed out") from error
        except OSError as error:
            _write_summary(
                session_export_json,
                _redact_payload({"status": "error", "error": str(error)}, env),
            )
            raise E2EValidationError(f"could not export OpenCode session: {error}") from error
        if export_result.returncode != 0:
            export_failure = _redact_payload(
                {
                    "status": "fail",
                    "returncode": export_result.returncode,
                    "stdout": export_result.stdout,
                    "stderr": export_result.stderr,
                },
                env,
            )
            assert isinstance(export_failure, dict)
            _write_summary(session_export_json, export_failure)
            detail = _redact((export_result.stderr or export_result.stdout or "").strip(), env)
            suffix = f": {detail}" if detail else ""
            raise E2EValidationError(
                f"OpenCode session export exited with status {export_result.returncode}{suffix}"
            )
        try:
            session_export: Any = json.loads(export_result.stdout)
        except json.JSONDecodeError as error:
            invalid_export = _redact_payload(
                {"status": "invalid", "raw": export_result.stdout},
                env,
            )
            assert isinstance(invalid_export, dict)
            _write_summary(session_export_json, invalid_export)
            raise E2EValidationError("OpenCode session export is not valid JSON") from error
        redacted_export = _redact_payload(session_export, env)
        if not isinstance(redacted_export, dict):
            redacted_export = {"status": "invalid", "payload": redacted_export}
        _write_summary(session_export_json, redacted_export)
        transcript = validate_session_export(
            session_export,
            session_id=session_id,
            target=target,
            expected_version=args.expected_opencode_version,
            expected_agent=args.agent,
            expected_model=args.model,
        )
        if (
            probe_transcript.target_id != transcript.target_id
            or probe_transcript.tools != transcript.tools
            or probe_transcript.actions != transcript.actions
            or probe_transcript.final_response != transcript.final_response
        ):
            raise E2EValidationError("probe transcript does not match the complete session export")
        provider, model_id = _model_identity(args.model)
        summary = {
            **base_summary,
            "actions": transcript.actions,
            "elapsed_ms": transcript.elapsed_ms,
            "final_response": transcript.final_response,
            "model_id": model_id,
            "provider": provider,
            "session_id": session_id,
            "status": "pass",
            "target": {
                "citation": target.citation,
                "line": target.line,
                "name": target.name,
                "path": target.relative_path,
                "sha256": target.sha256,
                "target_id": transcript.target_id,
            },
            "tools": transcript.tools,
        }
        summary = _redact_payload(summary, env)
        assert isinstance(summary, dict)
        _write_summary(summary_json, summary)
        return summary
    except E2EValidationError as error:
        failure = {
            **base_summary,
            "error": _redact(str(error), env),
            "status": "fail",
        }
        failure = _redact_payload(failure, env)
        assert isinstance(failure, dict)
        _write_summary(summary_json, failure)
        raise
    finally:
        try:
            materialize_redacted_jsonl(raw_temp, raw_jsonl, env)
        finally:
            _discard_sensitive_tree(ephemeral_state_root)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run_e2e(args)
    except E2EValidationError:
        print(f"OpenRouter E2E failed; see {_artifact_path(args.artifact_dir.resolve(), args.summary_json)}")
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
