from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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
    assert command[0] == sys.executable
    assert command[1] == str(ROOT / "scripts" / "run_opencode_lsp_probe.py")
    assert command[command.index("--model") + 1] == "openrouter/google/gemma-4-31b-it"
    assert command[command.index("--agent") + 1] == "python-delphi-lsp"
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

    def fake_run(command, *, cwd, env, text, capture_output, timeout, check):
        captured.update(command=command, cwd=cwd, env=env, timeout=timeout, check=check)
        output = Path(command[command.index("--output") + 1])
        events = _good_events()
        events[-1]["part"]["text"] += f" diagnostic={secret}"
        _write_jsonl(output, events)
        return subprocess.CompletedProcess(command, 0, stdout='{"evidences":[]}', stderr="")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
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
    assert env["XDG_DATA_HOME"] == str(artifacts / "xdg" / "data")
    assert env["XDG_CACHE_HOME"] == str(artifacts / "xdg" / "cache")
    assert env["XDG_STATE_HOME"] == str(artifacts / "xdg" / "state")
    assert env["NPM_CONFIG_CACHE"] == str(artifacts / "npm-cache")
    for key in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "NPM_CONFIG_CACHE"):
        assert Path(env[key]).is_dir()
        assert os.access(env[key], os.W_OK)
    assert captured["timeout"] == 22.0
    assert summary["status"] == "pass"
    assert summary["model"] == "openrouter/google/gemma-4-31b-it"
    assert (artifacts / "opencode.jsonl").is_file()
    summary_text = (artifacts / "summary.json").read_text(encoding="utf-8")
    assert secret not in summary_text
    assert "OPENROUTER_API_KEY" not in summary_text
    assert json.loads(summary_text)["target"]["citation"] == "mormot2/src/core/mormot.core.base.pas:3"


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
        return subprocess.CompletedProcess(command, 2, stdout="", stderr=f"Bearer {secret}")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
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
