from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_openrouter_github_e2e.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_openrouter_github_e2e", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    source = workspace / "mormot2" / "src" / "core" / "mormot.core.base.pas"
    source.parent.mkdir(parents=True)
    source.write_text(
        "unit mormot.core.base;\n"
        "interface\n"
        "TSynLogInfo = TSynLogLevel;\n"
        "implementation\n"
        "end.\n",
        encoding="utf-8",
    )
    relative = source.relative_to(workspace).as_posix()
    manifest = {
        "schema_version": 1,
        "target_lines": 5,
        "line_count": 5,
        "file_count": 1,
        "target_reached": True,
        "corpora": [
            {
                "name": "mormot2",
                "repository": "https://github.com/synopse/mORMot2.git",
                "revision": "58b4e9a8ca1e292d6beb89bb3ad05d3826f314f6",
                "line_count": 5,
                "file_count": 1,
                "files": [
                    {
                        "path": relative,
                        "lines": 5,
                        "bytes": source.stat().st_size,
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    }
                ],
            }
        ],
    }
    manifest_path = workspace / "corpus-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return workspace, manifest_path


def _tool_event(
    tool: str,
    tool_input: dict[str, object],
    output: str,
    *,
    start: int,
    status: str = "completed",
) -> dict[str, object]:
    return {
        "type": "tool_use",
        "part": {
            "tool": tool,
            "state": {
                "status": status,
                "input": tool_input,
                "output": output,
                "time": {"start": start, "end": start + 10},
            },
        },
    }


def _good_events(target_id: str = "target_v2_123") -> list[dict[str, object]]:
    path = "mormot2/src/core/mormot.core.base.pas"
    return [
        _tool_event(
            "skill",
            {"name": "python-delphi-lsp"},
            "Loaded skill: python-delphi-lsp",
            start=10,
        ),
        _tool_event(
            "delphi_codebase",
            {"action": "open", "max_items": 20},
            json.dumps({"schema": 2, "result": [{"path": path}]}),
            start=20,
        ),
        _tool_event(
            "delphi_codebase",
            {"action": "find", "query": "TSynLogInfo", "max_items": 10},
            json.dumps(
                {
                    "schema": 2,
                    "result": [
                        {
                            "name": "TSynLogInfo",
                            "path": path,
                            "line": 3,
                            "target_id": target_id,
                        }
                    ],
                }
            ),
            start=30,
        ),
        _tool_event(
            "delphi_codebase",
            {"action": "focus", "target_id": target_id},
            json.dumps({"schema": 2, "focus": {"target_id": target_id}}),
            start=40,
        ),
        _tool_event(
            "delphi_codebase",
            {"action": "inspect", "target_id": target_id, "detail": "declaration"},
            json.dumps(
                {
                    "schema": 2,
                    "result": [
                        {
                            "path": path,
                            "start_line": 3,
                            "end_line": 3,
                            "target_id": target_id,
                            "text": "TSynLogInfo = TSynLogLevel;",
                        }
                    ],
                }
            ),
            start=50,
        ),
        {
            "type": "text",
            "part": {
                "text": "TSynLogInfo is defined at "
                "mormot2/src/core/mormot.core.base.pas:3."
            },
        },
    ]


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _session_events(
    *, session_id: str = "ses_release_gate", secret: str | None = None
) -> list[dict[str, object]]:
    events = _good_events()
    for event in events:
        event["sessionID"] = session_id
        part = event.get("part")
        if isinstance(part, dict):
            part["sessionID"] = session_id
    if secret is not None:
        part = events[-1]["part"]
        assert isinstance(part, dict)
        part["diagnostics"] = {
            "authorization": f"Bearer {secret}",
            "nested": [{"apiKey": secret}],
        }
    return events


def _session_export(
    *,
    session_id: str = "ses_release_gate",
    version: str = "1.17.18",
    agent: str = "python-delphi-lsp",
    provider: str = "openrouter",
    model: str = "google/gemma-4-31b-it",
    finish: str = "stop",
    extra_tool: dict[str, object] | None = None,
    secret: str | None = None,
) -> dict[str, object]:
    messages: list[dict[str, object]] = [
        {
            "info": {
                "role": "user",
                "agent": agent,
                "model": {"providerID": provider, "modelID": model},
                "sessionID": session_id,
            },
            "parts": [{"type": "text", "text": "release gate", "sessionID": session_id}],
        }
    ]
    events = _good_events()
    for index, event in enumerate(events):
        part = dict(event["part"])
        part["sessionID"] = session_id
        if event["type"] == "tool_use":
            part["type"] = "tool"
            message_finish = "tool-calls"
        else:
            part["type"] = "text"
            message_finish = finish
        messages.append(
            {
                "info": {
                    "role": "assistant",
                    "mode": agent,
                    "agent": agent,
                    "providerID": provider,
                    "modelID": model,
                    "finish": message_finish,
                    "sessionID": session_id,
                },
                "parts": [part],
            }
        )
        if index == len(events) - 2 and extra_tool is not None:
            injected = dict(extra_tool)
            injected["type"] = "tool"
            injected["sessionID"] = session_id
            messages.append(
                {
                    "info": {
                        "role": "assistant",
                        "mode": agent,
                        "agent": agent,
                        "providerID": provider,
                        "modelID": model,
                        "finish": "tool-calls",
                        "sessionID": session_id,
                    },
                    "parts": [injected],
                }
            )
    payload: dict[str, object] = {
        "info": {
            "id": session_id,
            "agent": agent,
            "model": {"id": model, "providerID": provider},
            "version": version,
        },
        "messages": messages,
    }
    if secret is not None:
        payload["credentials"] = {
            "OPENROUTER_API_KEY": secret,
            "headers": [{"authorization": f"Bearer {secret}"}],
        }
    return payload


def test_online_harness_script_exists() -> None:
    assert SCRIPT.is_file()


def test_defaults_and_probe_command_pin_model_agent_and_strict_evidence(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
        ]
    )
    raw = tmp_path / "raw.jsonl"

    command = harness.build_probe_command(args, raw)

    assert args.model == "openrouter/google/gemma-4-31b-it"
    assert args.agent == "python-delphi-lsp"
    assert args.opencode == "opencode"
    assert args.expected_opencode_version == "1.17.18"
    assert command[0] == sys.executable
    assert command[1] == str(ROOT / "scripts" / "run_opencode_lsp_probe.py")
    assert command[command.index("--model") + 1] == "openrouter/google/gemma-4-31b-it"
    assert command[command.index("--agent") + 1] == "python-delphi-lsp"
    assert command[command.index("--opencode") + 1] == "opencode"
    assert "--inherit-process-group" in command
    requirements = [command[index + 1] for index, item in enumerate(command) if item == "--require-tool"]
    assert requirements == [
        "skill:python-delphi-lsp",
        'delphi_codebase.open:"schema":2',
        "delphi_codebase.find:TSynLogInfo",
        "delphi_codebase.focus:target_id",
        "delphi_codebase.inspect:TSynLogInfo",
    ]
    forbidden = [command[index + 1] for index, item in enumerate(command) if item == "--forbid-tool"]
    assert forbidden == ["bash", "read", "grep", "glob", "list", "invalid"]
    prompt = command[2]
    assert "skill, open, find, focus, inspect" in prompt
    assert "TSynLogInfo" in prompt
    assert "mormot2/src/core/mormot.core.base.pas:3" in prompt


def test_target_must_match_manifest_hash_and_source_line(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )

    assert target.source_line == "TSynLogInfo = TSynLogLevel;"

    (workspace / target.relative_path).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(harness.E2EValidationError, match="hash"):
        harness.validate_target(
            workspace=workspace,
            manifest_path=manifest,
            name="TSynLogInfo",
            relative_path=target.relative_path,
            line=3,
        )


def test_transcript_requires_exact_sequence_and_source_backed_final(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )

    evidence = harness.validate_transcript(_good_events(), target)

    assert evidence.target_id == "target_v2_123"
    assert evidence.tools == ["skill", "delphi_codebase", "delphi_codebase", "delphi_codebase", "delphi_codebase"]
    assert evidence.actions == ["skill", "open", "find", "focus", "inspect"]
    assert evidence.elapsed_ms == {"skill": 10, "open": 10, "find": 10, "focus": 10, "inspect": 10}
    assert evidence.final_response.endswith("mormot2/src/core/mormot.core.base.pas:3.")


def test_transcript_accepts_inspect_of_confirmed_implicit_focus(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    events = _good_events()
    inspect_state = events[4]["part"]["state"]
    inspect_state["input"].pop("target_id")
    inspect_output = json.loads(inspect_state["output"])
    inspect_output["focus"] = {"target_id": "target_v2_123"}
    inspect_state["output"] = json.dumps(inspect_output)

    evidence = harness.validate_transcript(events, target)

    assert evidence.target_id == "target_v2_123"


def test_transcript_rejects_unconfirmed_implicit_inspect_focus(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    events = _good_events()
    inspect_state = events[4]["part"]["state"]
    inspect_state["input"].pop("target_id")
    inspect_output = json.loads(inspect_state["output"])
    inspect_output["focus"] = {"target_id": "target_v2_wrong"}
    inspect_state["output"] = json.dumps(inspect_output)

    with pytest.raises(harness.E2EValidationError, match="focused target_id"):
        harness.validate_transcript(events, target)


@pytest.mark.parametrize("forbidden", ["bash", "read", "grep", "glob", "list", "invalid"])
def test_transcript_rejects_every_forbidden_tool(tmp_path: Path, forbidden: str) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    events = _good_events()
    events.insert(2, _tool_event(forbidden, {"path": "x"}, "x", start=25))

    with pytest.raises(harness.E2EValidationError, match="forbidden|unexpected"):
        harness.validate_transcript(events, target)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda events: events.insert(2, _tool_event("skill", {"name": "python-delphi-lsp"}, "x", start=25)), "exactly"),
        (lambda events: events[2]["part"]["state"].update(input={}), "empty"),
        (lambda events: events[2]["part"]["state"].update(input={"action": "find"}), "query"),
        (lambda events: events[3]["part"]["state"].update(input={"action": "focus"}), "target_id"),
        (
            lambda events: events[4]["part"]["state"].update(
                input={"action": "inspect", "target_id": "target_v2_wrong", "detail": "declaration"}
            ),
            "target_id",
        ),
        (lambda events: events[1]["part"]["state"].update(status="error"), "completed"),
    ],
)
def test_transcript_rejects_extra_empty_missing_and_mismatched_calls(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    events = _good_events()
    mutate(events)

    with pytest.raises(harness.E2EValidationError, match=message):
        harness.validate_transcript(events, target)


def test_stream_requires_one_session_and_rejects_error_events() -> None:
    harness = _load_module()
    events = _session_events()

    assert harness.validate_stream(events) == "ses_release_gate"

    events[1]["sessionID"] = "ses_other"
    with pytest.raises(harness.E2EValidationError, match="exactly one sessionID"):
        harness.validate_stream(events)

    nested_events = _session_events()
    nested_events[1]["part"]["sessionID"] = "ses_other"
    with pytest.raises(harness.E2EValidationError, match="exactly one sessionID"):
        harness.validate_stream(nested_events)

    error_events = _session_events()
    error_events.append(
        {
            "type": "error",
            "sessionID": "ses_release_gate",
            "error": {"name": "ProviderError", "message": "upstream failed"},
        }
    )
    with pytest.raises(harness.E2EValidationError, match="error event"):
        harness.validate_stream(error_events)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["info"].update(version="1.17.17"), "version"),
        (lambda payload: payload["info"].update(agent="build"), "agent"),
        (
            lambda payload: payload["info"]["model"].update(providerID="ollama"),
            "provider",
        ),
        (
            lambda payload: payload["info"]["model"].update(id="google/gemma-3-27b-it"),
            "model",
        ),
        (
            lambda payload: payload["messages"][-1]["info"].update(finish="tool-calls"),
            "finish=stop",
        ),
    ],
)
def test_export_pins_version_agent_provider_model_and_finish(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    payload = _session_export()
    mutate(payload)

    with pytest.raises(harness.E2EValidationError, match=message):
        harness.validate_session_export(
            payload,
            session_id="ses_release_gate",
            target=target,
            expected_version="1.17.18",
            expected_agent="python-delphi-lsp",
            expected_model="openrouter/google/gemma-4-31b-it",
        )


def test_export_rejects_tool_call_after_probe_evidence(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    extra = _tool_event("bash", {"command": "cat source"}, "source", start=60)["part"]
    payload = _session_export(extra_tool=extra)

    with pytest.raises(harness.E2EValidationError, match="forbidden|exactly five"):
        harness.validate_session_export(
            payload,
            session_id="ses_release_gate",
            target=target,
            expected_version="1.17.18",
            expected_agent="python-delphi-lsp",
            expected_model="openrouter/google/gemma-4-31b-it",
        )


def test_export_rejects_nested_session_id_mismatch(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    payload = _session_export()
    payload["messages"][0]["parts"][0]["sessionID"] = "ses_other"

    with pytest.raises(harness.E2EValidationError, match="another sessionID"):
        harness.validate_session_export(
            payload,
            session_id="ses_release_gate",
            target=target,
            expected_version="1.17.18",
            expected_agent="python-delphi-lsp",
            expected_model="openrouter/google/gemma-4-31b-it",
        )


def test_export_requires_citation_in_terminal_stop_message(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    payload = _session_export()
    terminal = payload["messages"][-1]
    terminal["parts"] = []
    payload["messages"][-2]["parts"].append(
        {
            "type": "text",
            "text": "TSynLogInfo is defined at mormot2/src/core/mormot.core.base.pas:3.",
            "sessionID": "ses_release_gate",
        }
    )

    with pytest.raises(harness.E2EValidationError, match="terminal.*finish=stop"):
        harness.validate_session_export(
            payload,
            session_id="ses_release_gate",
            target=target,
            expected_version="1.17.18",
            expected_agent="python-delphi-lsp",
            expected_model="openrouter/google/gemma-4-31b-it",
        )


def test_export_rejects_message_after_terminal_stop(tmp_path: Path) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    target = harness.validate_target(
        workspace=workspace,
        manifest_path=manifest,
        name="TSynLogInfo",
        relative_path="mormot2/src/core/mormot.core.base.pas",
        line=3,
    )
    payload = _session_export()
    payload["messages"].append(
        {
            "info": {
                "role": "user",
                "agent": "python-delphi-lsp",
                "model": {
                    "providerID": "openrouter",
                    "modelID": "google/gemma-4-31b-it",
                },
                "sessionID": "ses_release_gate",
            },
            "parts": [
                {
                    "type": "text",
                    "text": "trailing message",
                    "sessionID": "ses_release_gate",
                }
            ],
        }
    )

    with pytest.raises(harness.E2EValidationError, match=r"messages\[-1\].*finish=stop"):
        harness.validate_session_export(
            payload,
            session_id="ses_release_gate",
            target=target,
            expected_version="1.17.18",
            expected_agent="python-delphi-lsp",
            expected_model="openrouter/google/gemma-4-31b-it",
        )


def test_redacted_jsonl_is_recursive_and_temp_file_is_removed(tmp_path: Path) -> None:
    harness = _load_module()
    secret = "sk-or-v1-jsonl-secret"
    raw = tmp_path / ".opencode-unredacted.jsonl"
    final = tmp_path / "opencode.jsonl"
    events = _session_events(secret=secret)
    events[-1]["part"][secret] = "secret used as a key"
    _write_jsonl(raw, events)

    harness.materialize_redacted_jsonl(
        raw,
        final,
        {"OPENROUTER_API_KEY": secret},
    )

    artifact = final.read_text(encoding="utf-8")
    assert secret not in artifact
    assert "[REDACTED]" in artifact
    assert raw.exists() is False
    assert all(json.loads(line) for line in artifact.splitlines())


def test_timeout_terminates_entire_probe_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _load_module()
    popen_kwargs: dict[str, object] = {}
    signals: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self, _command, **kwargs) -> None:
            popen_kwargs.update(kwargs)
            self.killed = False

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["probe"], timeout)

        def poll(self):
            return None

        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired(["probe"], timeout)
            self.returncode = -getattr(signal, "SIGKILL", 9)
            return self.returncode

    process: FakeProcess | None = None

    def fake_popen(command, **kwargs):
        nonlocal process
        process = FakeProcess(command, **kwargs)
        return process

    def fake_killpg(pid: int, sig: int) -> None:
        if sig == 0:
            if process is not None and process.killed:
                raise ProcessLookupError
            return
        signals.append((pid, sig))
        if sig == getattr(signal, "SIGKILL", 9):
            assert process is not None
            process.killed = True

    monkeypatch.setattr(harness, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(harness.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(harness.os, "killpg", fake_killpg, raising=False)

    with pytest.raises(subprocess.TimeoutExpired):
        harness.run_command(["probe"], cwd=Path("/tmp"), env={}, timeout=0.01)

    assert popen_kwargs["start_new_session"] is True
    assert signals == [(4242, signal.SIGTERM), (4242, getattr(signal, "SIGKILL", 9))]


def test_run_command_cleans_up_on_any_communicate_failure_without_masking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    original_error = KeyboardInterrupt("operator cancelled")
    cleanup_calls: list[tuple[object, object]] = []

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):
            del timeout
            raise original_error

    process = FakeProcess()
    windows_job = object()
    monkeypatch.setattr(
        harness,
        "_start_owned_process",
        lambda *_args, **_kwargs: (process, windows_job),
    )

    def fail_cleanup(candidate, *, windows_job):
        cleanup_calls.append((candidate, windows_job))
        raise RuntimeError("cleanup also failed")

    monkeypatch.setattr(harness, "_terminate_process_group", fail_cleanup)

    with pytest.raises(KeyboardInterrupt) as captured:
        harness.run_command(["probe"], cwd=Path("/tmp"), env={}, timeout=0.01)

    assert captured.value is original_error
    assert cleanup_calls == [(process, windows_job)]


def test_windows_timeout_terminates_entire_probe_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    popen_kwargs: dict[str, object] = {}
    popen_command: list[str] = []
    job_calls: list[tuple[object, float]] = []
    lifecycle: list[str] = []

    class FakeProcess:
        pid = 4242
        returncode = None
        stdin = None

        def __init__(self, _command, **kwargs) -> None:
            popen_command.extend(_command)
            lifecycle.append("popen")
            popen_kwargs.update(kwargs)
            self.stdin = FakeStdin()

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["probe"], timeout)

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -1
            return self.returncode

    class FakeStdin:
        def fileno(self):
            return 81

        def close(self):
            lifecycle.append("stdin-close")

    class FakeWindowsJob:
        def assign(self, process):
            assert process is not None
            lifecycle.append("assign")

        def terminate_and_wait(self, process, *, timeout):
            job_calls.append((process, timeout))

    process: FakeProcess | None = None

    def fake_popen(command, **kwargs):
        nonlocal process
        process = FakeProcess(command, **kwargs)
        return process

    job = FakeWindowsJob()

    def fake_new_windows_job():
        lifecycle.append("new-job")
        return job

    monkeypatch.setattr(harness, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(harness.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        harness.os,
        "write",
        lambda fd, payload: (
            lifecycle.append("release")
            or (len(payload) if fd == 81 else pytest.fail("unexpected bootstrap fd"))
        ),
    )
    monkeypatch.setattr(
        harness,
        "_new_windows_job",
        fake_new_windows_job,
        raising=False,
    )
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "Windows cleanup must not resolve an executable via PATH"
        ),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        harness.run_command(["probe"], cwd=Path("C:/tmp"), env={}, timeout=0.01)

    assert popen_kwargs["creationflags"] == harness._CREATE_NEW_PROCESS_GROUP
    assert "start_new_session" not in popen_kwargs
    assert process is not None
    assert lifecycle[:5] == ["new-job", "popen", "assign", "release", "stdin-close"]
    assert popen_command[:4] == [
        sys.executable,
        "-I",
        "-c",
        harness._WINDOWS_BOOTSTRAP,
    ]
    assert popen_command[4:] == ["probe"]
    assert popen_kwargs["stdin"] is subprocess.PIPE
    assert job_calls == [(process, harness._WINDOWS_JOB_WAIT_TIMEOUT)]


def test_windows_release_failure_stops_assigned_wrapper_before_target_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    lifecycle: list[str] = []

    class FakeStdin:
        def fileno(self):
            return 82

        def close(self):
            lifecycle.append("stdin-close")

    class FakeProcess:
        stdin = FakeStdin()

        def communicate(self, timeout=None):
            del timeout
            pytest.fail("wrapper must not communicate after release failure")

    class FakeJob:
        def assign(self, process):
            assert process is fake_process
            lifecycle.append("assign")

        def terminate_and_wait(self, process, *, timeout):
            assert process is fake_process
            lifecycle.append(f"terminate:{timeout}")

    fake_process = FakeProcess()
    fake_job = FakeJob()
    monkeypatch.setattr(harness, "_IS_WINDOWS", True)
    monkeypatch.setattr(harness, "_new_windows_job", lambda: fake_job)
    monkeypatch.setattr(
        harness.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake_process,
    )
    monkeypatch.setattr(
        harness.os,
        "write",
        lambda *_args: (_ for _ in ()).throw(OSError("release pipe failed")),
    )

    with pytest.raises(
        RuntimeError,
        match="could not release Windows process bootstrap",
    ) as captured:
        harness.run_command(["target-with-secret-argument"], cwd=Path("C:/tmp"), env={}, timeout=1)

    assert lifecycle == [
        "assign",
        "stdin-close",
        f"terminate:{harness._WINDOWS_JOB_WAIT_TIMEOUT}",
    ]
    assert fake_process.stdin is None
    assert "target-with-secret-argument" not in str(captured.value)


def test_windows_popen_failure_closes_job_without_command_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    lifecycle: list[str] = []

    class FakeJob:
        def close(self):
            lifecycle.append("job-close")

    monkeypatch.setattr(harness, "_IS_WINDOWS", True)
    monkeypatch.setattr(harness, "_new_windows_job", lambda: FakeJob())
    monkeypatch.setattr(
        harness.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("spawn failed for target-with-secret-argument")
        ),
    )
    monkeypatch.setattr(
        harness.os,
        "write",
        lambda *_args: pytest.fail("failed wrapper must never be released"),
    )

    with pytest.raises(
        OSError,
        match="could not start Windows process bootstrap",
    ) as captured:
        harness.run_command(["target-with-secret-argument"], cwd=Path("C:/tmp"), env={}, timeout=1)

    assert lifecycle == ["job-close"]
    assert "target-with-secret-argument" not in str(captured.value)


def test_windows_bootstrap_mirrors_target_exitcode_stdout_and_stderr() -> None:
    harness = _load_module()
    target = [
        sys.executable,
        "-c",
        "import sys; print('target-out'); print('target-err', file=sys.stderr); raise SystemExit(9)",
    ]

    completed = subprocess.run(
        harness._windows_bootstrap_command(target),
        input=harness._WINDOWS_BOOTSTRAP_RELEASE,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 9
    assert completed.stdout.splitlines() == [b"target-out"]
    assert completed.stderr.splitlines() == [b"target-err"]

    missing = subprocess.run(
        harness._windows_bootstrap_command(["missing-target-with-secret-argument"]),
        input=harness._WINDOWS_BOOTSTRAP_RELEASE,
        capture_output=True,
        check=False,
    )
    assert missing.returncode == 126
    assert missing.stdout == b""
    assert missing.stderr.splitlines() == [b"Windows process bootstrap could not start target"]


def test_windows_cleanup_uses_job_even_when_leader_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    calls: list[tuple[object, float]] = []

    class ExitedLeader:
        pid = 4343
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class FakeWindowsJob:
        def terminate_and_wait(self, process, *, timeout):
            calls.append((process, timeout))

    process = ExitedLeader()
    job = FakeWindowsJob()
    monkeypatch.setattr(harness, "_IS_WINDOWS", True, raising=False)

    harness._terminate_process_group(process, windows_job=job, grace_seconds=0.3)

    assert calls == [(process, harness._WINDOWS_JOB_WAIT_TIMEOUT)]


def test_windows_job_waits_for_descendants_before_closing_handle() -> None:
    harness = _load_module()
    events: list[object] = []

    class FakeApi:
        active = iter([1, 0])

        def terminate(self, handle, exit_code):
            events.append(("terminate", handle, exit_code))

        def active_processes(self, handle):
            events.append(("query", handle))
            return next(self.active)

        def close(self, handle):
            events.append(("close", handle))

    class ExitedLeader:
        def wait(self, timeout=None):
            events.append(("wait", timeout))
            return 0

    job = harness._WindowsJob(api=FakeApi(), handle=92)

    job.terminate_and_wait(ExitedLeader(), timeout=0.2)

    assert events[0] == ("terminate", 92, 1)
    assert events.count(("query", 92)) == 2
    assert events[-1] == ("close", 92)
    assert any(isinstance(event, tuple) and event[0] == "wait" for event in events)


def test_windows_job_close_can_retry_after_close_handle_failure() -> None:
    harness = _load_module()
    attempts: list[int] = []

    class FlakyApi:
        def close(self, handle):
            attempts.append(handle)
            if len(attempts) == 1:
                raise OSError("CloseHandle failed")

    job = harness._WindowsJob(api=FlakyApi(), handle=93)

    with pytest.raises(OSError, match="CloseHandle failed"):
        job.close()

    assert job._closed is False
    job.close()
    job.close()

    assert attempts == [93, 93]
    assert job._closed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX can address a process group after its leader exits")
def test_outer_cleanup_kills_descendants_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    group_alive = True
    signals: list[int] = []

    class ExitedLeader:
        pid = 4343

        def poll(self):
            return 0

        def wait(self, timeout=None):
            del timeout
            return 0

    def fake_killpg(pid: int, sig: int) -> None:
        nonlocal group_alive
        assert pid == 4343
        if sig == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append(sig)
        if sig == signal.SIGKILL:
            group_alive = False

    monkeypatch.setattr(harness.os, "killpg", fake_killpg)

    harness._terminate_process_group(ExitedLeader(), grace_seconds=0.01)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert group_alive is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_outer_cleanup_waits_for_exiting_group_after_sigterm_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    group_probes = 0
    wait_calls: list[float | None] = []

    class ExitedLeader:
        pid = 4444

        def poll(self):
            return 0

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            return 0

    def fake_killpg(pid: int, sig: int) -> None:
        nonlocal group_probes
        assert pid == 4444
        if sig == signal.SIGTERM:
            raise PermissionError(1, "process is already exiting")
        assert sig == 0
        group_probes += 1
        if group_probes >= 2:
            raise ProcessLookupError

    monkeypatch.setattr(harness.os, "killpg", fake_killpg)

    harness._terminate_process_group(ExitedLeader(), grace_seconds=0.01)

    assert group_probes == 2
    assert wait_calls == [0.01]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_outer_cleanup_does_not_mask_persistent_sigterm_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    permission_error = PermissionError(1, "process group cannot be signaled")
    waits: list[tuple[object, int, float]] = []

    class ExitedLeader:
        pid = 4545

        def poll(self):
            return 0

    process = ExitedLeader()

    def fake_killpg(pid: int, sig: int) -> None:
        assert (pid, sig) == (4545, signal.SIGTERM)
        raise permission_error

    def group_never_exits(candidate, process_group_id: int, timeout: float) -> bool:
        waits.append((candidate, process_group_id, timeout))
        return False

    monkeypatch.setattr(harness.os, "killpg", fake_killpg)
    monkeypatch.setattr(harness, "_wait_for_process_group_exit", group_never_exits)

    with pytest.raises(PermissionError) as captured:
        harness._terminate_process_group(process, grace_seconds=0.01)

    assert captured.value is permission_error
    assert waits == [(process, 4545, 5.0)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_outer_cleanup_waits_for_exiting_group_after_sigkill_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    signals: list[int] = []
    exit_checks = iter([False, True])
    wait_calls: list[float | None] = []

    class ExitedLeader:
        pid = 4646

        def poll(self):
            return 0

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            return 0

    process = ExitedLeader()

    def fake_killpg(pid: int, sig: int) -> None:
        assert pid == 4646
        signals.append(sig)
        if sig == getattr(signal, "SIGKILL", 9):
            raise PermissionError(1, "process is already exiting")

    def fake_wait(candidate, process_group_id: int, timeout: float) -> bool:
        assert candidate is process
        assert process_group_id == 4646
        assert timeout in {0.01, 5.0}
        return next(exit_checks)

    monkeypatch.setattr(harness.os, "killpg", fake_killpg)
    monkeypatch.setattr(harness, "_wait_for_process_group_exit", fake_wait)

    harness._terminate_process_group(process, grace_seconds=0.01)

    assert signals == [signal.SIGTERM, getattr(signal, "SIGKILL", 9)]
    assert wait_calls == [0.01]


def test_outer_timeout_kills_probe_opencode_and_real_descendant(tmp_path: Path) -> None:
    harness = _load_module()
    process_record = tmp_path / "processes.json"
    # OpenCode's first argument is the `run` subcommand. Pointing --opencode at
    # Python therefore executes this script portably as `python run ...`.
    fake_opencode = tmp_path / "run"
    fake_opencode.write_text(
        "import json, os, pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"path = pathlib.Path({str(process_record)!r})\n"
        "path.write_text(json.dumps({'opencode': os.getpid(), 'child': child.pid}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    output = tmp_path / "probe.jsonl"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_opencode_lsp_probe.py"),
        "run the release probe",
        "--cwd",
        str(tmp_path),
        "--opencode",
        sys.executable,
        "--timeout",
        "30",
        "--inherit-process-group",
        "--output",
        str(output),
    ]
    pids: list[int] = []

    try:
        with pytest.raises(subprocess.TimeoutExpired):
            harness.run_command(
                command,
                cwd=tmp_path,
                env=os.environ.copy(),
                timeout=3.0,
            )
        record = json.loads(process_record.read_text(encoding="utf-8"))
        pids = [record["opencode"], record["child"]]
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if all(not _pid_exists(pid) for pid in pids):
                break
            time.sleep(0.02)
        assert all(not _pid_exists(pid) for pid in pids)
    finally:
        for pid in pids:
            _kill_pid(pid)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Job Objects")
def test_windows_cleanup_kills_child_after_leader_has_exited(tmp_path: Path) -> None:
    harness = _load_module()
    child_record = tmp_path / "child.txt"
    leader = tmp_path / "leader.py"
    leader.write_text(
        "import pathlib, subprocess, sys\n"
        "flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=flags)\n"
        f"pathlib.Path({str(child_record)!r}).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    child_pid: int | None = None

    try:
        completed = harness.run_command(
            [sys.executable, str(leader)],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=10.0,
        )
        assert completed.returncode == 0
        child_pid = int(child_record.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_exists(child_pid):
            time.sleep(0.02)
        assert not _pid_exists(child_pid)
    finally:
        if child_pid is not None:
            _kill_pid(child_pid)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.windll.kernel32
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(synchronize, False, pid)
        if not handle:
            return False
        try:
            return wait_for_single_object(handle, 0) == wait_timeout
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        terminate_process = kernel32.TerminateProcess
        terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
        terminate_process.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x0001, False, pid)
        if handle:
            try:
                terminate_process(handle, 1)
            finally:
                close_handle(handle)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_run_validates_export_and_retains_only_redacted_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    secret = "sk-or-v1-full-session-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    commands: list[list[str]] = []

    def fake_command(command, *, cwd, env, timeout):
        del cwd, env, timeout
        command = list(command)
        commands.append(command)
        if command == ["/opt/release/opencode", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="1.17.18\n", stderr="")
        if command[:2] == ["/opt/release/opencode", "export"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_session_export(secret=secret)),
                stderr="",
            )
        output = Path(command[command.index("--output") + 1])
        _write_jsonl(output, _session_events(secret=secret))
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(harness, "run_command", fake_command)
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy runner used")),
    )
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
            "--opencode",
            "/opt/release/opencode",
        ]
    )

    summary = harness.run_e2e(args)

    assert commands[0] == ["/opt/release/opencode", "--version"]
    assert commands[-1] == ["/opt/release/opencode", "export", "ses_release_gate"]
    assert summary["session_id"] == "ses_release_gate"
    assert summary["opencode_version"] == "1.17.18"
    assert summary["provider"] == "openrouter"
    assert summary["model_id"] == "google/gemma-4-31b-it"
    for name in ("opencode.jsonl", "session-export.json", "summary.json"):
        artifact = (artifacts / name).read_text(encoding="utf-8")
        assert secret not in artifact
        assert "OPENROUTER_API_KEY" not in artifact
    assert list(artifacts.glob("*.unredacted*")) == []


def test_windows_spawn_failure_reaches_main_and_writes_sanitized_fail_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    leaked_spawn_detail = "target-with-secret-argument"
    close_calls: list[str] = []

    class FakeJob:
        def close(self):
            close_calls.append("close")

    monkeypatch.setattr(harness, "_IS_WINDOWS", True)
    monkeypatch.setattr(harness, "_new_windows_job", lambda: FakeJob())
    monkeypatch.setattr(
        harness.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(f"spawn failed for {leaked_spawn_detail}")
        ),
    )

    exit_code = harness.main(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
            "--opencode",
            "/opt/release/opencode",
        ]
    )

    output = capsys.readouterr()
    failure = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert close_calls == ["close"]
    assert failure["status"] == "fail"
    assert failure["error"] == (
        "could not run OpenCode version check: "
        "could not start Windows process bootstrap"
    )
    assert output.out == f"OpenRouter E2E failed; see {(artifacts / 'summary.json').resolve()}\n"
    assert output.err == ""
    assert leaked_spawn_detail not in json.dumps(failure)
    assert leaked_spawn_detail not in output.out


def test_failed_probe_still_redacts_partial_jsonl_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    secret = "sk-or-v1-partial-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    def fake_command(command, *, cwd, env, timeout):
        del cwd, env, timeout
        command = list(command)
        if command == ["opencode", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="1.17.18\n", stderr="")
        output = Path(command[command.index("--output") + 1])
        _write_jsonl(output, _session_events(secret=secret)[:2])
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr=f"Authorization: Bearer {secret}",
        )

    monkeypatch.setattr(harness, "run_command", fake_command)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
        ]
    )

    with pytest.raises(harness.E2EValidationError, match="probe exited with status 2"):
        harness.run_e2e(args)

    assert secret not in (artifacts / "opencode.jsonl").read_text(encoding="utf-8")
    assert secret not in (artifacts / "summary.json").read_text(encoding="utf-8")
    assert list(artifacts.glob("*.unredacted*")) == []


def test_run_uses_isolated_writable_state_and_writes_redacted_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    secret = "sk-or-v1-super-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd, env, timeout):
        command = list(command)
        if command == ["opencode", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="1.17.18\n", stderr="")
        if command[:2] == ["opencode", "export"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_session_export(secret=secret)),
                stderr="",
            )
        state_keys = (
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "NPM_CONFIG_CACHE",
        )
        captured.update(
            command=command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            state_existed=all(Path(env[key]).is_dir() for key in state_keys),
        )
        output = Path(command[command.index("--output") + 1])
        _write_jsonl(output, _session_events(secret=secret))
        return subprocess.CompletedProcess(command, 0, stdout='{"evidences":[]}', stderr="")

    monkeypatch.setattr(harness, "run_command", fake_run)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
            "--timeout",
            "17",
        ]
    )

    summary = harness.run_e2e(args)

    env = captured["env"]
    assert captured["state_existed"] is True
    for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "NPM_CONFIG_CACHE"):
        state_path = Path(env[key])
        assert state_path.is_relative_to(artifacts) is False
        assert state_path.exists() is False
    assert (artifacts / "xdg").exists() is False
    assert (artifacts / "npm-cache").exists() is False
    assert captured["timeout"] == 22.0
    assert summary["status"] == "pass"
    assert summary["model"] == "openrouter/google/gemma-4-31b-it"
    assert (artifacts / "opencode.jsonl").is_file()
    summary_text = (artifacts / "summary.json").read_text(encoding="utf-8")
    assert secret not in summary_text
    assert "OPENROUTER_API_KEY" not in summary_text
    assert json.loads(summary_text)["target"]["citation"] == "mormot2/src/core/mormot.core.base.pas:3"


def test_explicit_xdg_paths_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    configured = {
        "--xdg-data-home": tmp_path / "user-data",
        "--xdg-cache-home": tmp_path / "user-cache",
        "--xdg-state-home": tmp_path / "user-state",
        "--npm-cache": tmp_path / "user-npm-cache",
    }
    for path in configured.values():
        path.mkdir()
        (path / "keep.txt").write_text("keep", encoding="utf-8")

    def fake_run(command, *, cwd, env, timeout):
        del cwd, env, timeout
        command = list(command)
        if command == ["opencode", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="1.17.18\n", stderr="")
        if command[:2] == ["opencode", "export"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_session_export()),
                stderr="",
            )
        output = Path(command[command.index("--output") + 1])
        _write_jsonl(output, _session_events())
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(harness, "run_command", fake_run)
    argv = [
        "--workspace",
        str(workspace),
        "--manifest",
        str(manifest),
        "--artifact-dir",
        str(artifacts),
        "--target-name",
        "TSynLogInfo",
        "--target-path",
        "mormot2/src/core/mormot.core.base.pas",
        "--target-line",
        "3",
    ]
    for option, path in configured.items():
        argv.extend([option, str(path)])

    harness.run_e2e(harness.parse_args(argv))

    for path in configured.values():
        assert (path / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("option", ["--xdg-data-home", "--npm-cache"])
def test_state_path_inside_artifacts_is_rejected_before_opencode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    alias = tmp_path / "artifact-alias"
    alias.symlink_to(artifacts, target_is_directory=True)
    invoked = False

    def unexpected_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("OpenCode must not run for retained state paths")

    monkeypatch.setattr(harness, "run_command", unexpected_run)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
            option,
            str(alias / "retained-state"),
        ]
    )

    with pytest.raises(harness.E2EValidationError, match="retained artifact directory"):
        harness.run_e2e(args)

    assert invoked is False
    assert (artifacts / "retained-state").exists() is False


def test_ephemeral_state_is_removed_even_if_artifact_materialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    captured_root: Path | None = None

    def fake_run(command, *, cwd, env, timeout):
        nonlocal captured_root
        del cwd, timeout
        captured_root = Path(env["XDG_DATA_HOME"]).parent
        return subprocess.CompletedProcess(list(command), 2, stdout="", stderr="failed")

    def fail_materialization(*_args, **_kwargs) -> None:
        raise RuntimeError("artifact materialization failed")

    monkeypatch.setattr(harness, "run_command", fake_run)
    monkeypatch.setattr(harness, "materialize_redacted_jsonl", fail_materialization)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
        ]
    )

    try:
        with pytest.raises(RuntimeError, match="artifact materialization failed"):
            harness.run_e2e(args)
        assert captured_root is not None
        assert captured_root.exists() is False
    finally:
        if captured_root is not None:
            shutil.rmtree(captured_root, ignore_errors=True)


def test_nonzero_probe_status_is_release_blocking_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _load_module()
    workspace, manifest = _workspace(tmp_path)
    artifacts = tmp_path / "artifacts"
    secret = "sk-or-v1-never-log-this"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)

    def fake_run(command, **_kwargs):
        command = list(command)
        if command == ["opencode", "--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="1.17.18\n", stderr="")
        return subprocess.CompletedProcess(command, 2, stdout="", stderr=f"Bearer {secret}")

    monkeypatch.setattr(harness, "run_command", fake_run)
    args = harness.parse_args(
        [
            "--workspace",
            str(workspace),
            "--manifest",
            str(manifest),
            "--artifact-dir",
            str(artifacts),
            "--target-name",
            "TSynLogInfo",
            "--target-path",
            "mormot2/src/core/mormot.core.base.pas",
            "--target-line",
            "3",
        ]
    )

    with pytest.raises(harness.E2EValidationError, match="probe exited with status 2"):
        harness.run_e2e(args)

    failure_text = (artifacts / "summary.json").read_text(encoding="utf-8")
    assert secret not in failure_text
    assert "[REDACTED]" in failure_text
