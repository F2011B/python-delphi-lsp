from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from delphi_lsp import agent_cli
from delphi_lsp.agent_cache import start_cache
from delphi_lsp.parallel_outline import ParallelOutlineError


class _ReconfigurableBuffer(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.encoding_requests: list[str] = []

    def reconfigure(self, *, encoding: str) -> None:
        self.encoding_requests.append(encoding)


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_reconfigures_text_streams_to_utf8(monkeypatch) -> None:
    stdout = _ReconfigurableBuffer()
    stderr = _ReconfigurableBuffer()
    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(func=lambda _args: 0),
    )
    monkeypatch.setattr(agent_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    result = agent_cli.main([])

    assert result == 0
    assert stdout.encoding_requests == ["utf-8"]
    assert stderr.encoding_requests == ["utf-8"]


def test_cli_reports_unavoidable_encoding_errors(monkeypatch) -> None:
    def fail(_args) -> None:
        raise UnicodeEncodeError("ascii", "é", 0, 1, "not encodable")

    stderr = io.StringIO()
    parser = SimpleNamespace(
        parse_args=lambda _argv: SimpleNamespace(func=fail),
    )
    monkeypatch.setattr(agent_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", stderr)

    result = agent_cli.main([])

    assert result == 1
    assert stderr.getvalue().startswith("cli_error:encoding_error: ")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_cache_cli_rejects_non_positive_idle_timeout(value: str) -> None:
    parser = agent_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["cache", "start", "--idle-timeout", value])

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "cache",
                "serve",
                "--root",
                ".",
                "--max-memory",
                "1M",
                "--workers",
                "1",
                "--idle-timeout",
                value,
            ]
        )


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--max-items", "0"),
        ("--max-items", "51"),
        ("--max-chars", "255"),
        ("--max-chars", "40001"),
    ],
)
def test_query_cli_rejects_protocol_limits_before_contacting_cache(
    option: str,
    value: str,
) -> None:
    parser = agent_cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["query", "open", option, value])


def test_cache_start_help_documents_optional_dproj(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = agent_cli.build_parser()

    with pytest.raises(SystemExit) as stopped:
        parser.parse_args(["cache", "start", "--help"])

    assert stopped.value.code == 0
    assert ".dproj" in capsys.readouterr().out


def test_cache_cli_exposes_disk_budget_and_clear(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = agent_cli.build_parser()
    start = parser.parse_args(
        ["cache", "start", "--max-disk-cache", "64M"]
    )
    assert start.max_disk_cache == 64 * 1024**2

    navigation = (
        tmp_path / ".delphi-lsp" / "agent-cache" / "navigation-v1" / "aa"
    )
    navigation.mkdir(parents=True)
    (navigation / "stale.json").write_text("{}", encoding="utf-8")
    clear = parser.parse_args(["cache", "clear", "--root", str(tmp_path)])

    assert clear.func(clear) == 0
    assert not navigation.parent.exists()
    assert json.loads(capsys.readouterr().out) == {"cleared": True}


@pytest.mark.parametrize("value", [0, -1, True])
def test_start_cache_rejects_non_positive_idle_timeout(
    tmp_path: Path,
    value: int,
) -> None:
    with pytest.raises(ValueError, match="idle_timeout must be greater than zero"):
        start_cache(tmp_path, idle_timeout=value)


@pytest.mark.parametrize(
    "arguments",
    [
        ("view", "--layer", "overview", "--format", "json"),
        ("index",),
        ("worker",),
        ("cache", "start"),
        ("cache", "status"),
        ("cache", "stop"),
        ("query", "open"),
    ],
)
def test_cli_rejects_missing_workspace_without_traceback(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    missing = tmp_path / "missing"
    command = [arguments[0]]
    if arguments[0] == "worker":
        command.extend(["--root", str(missing)])
    else:
        command.extend(arguments[1:])
        command.extend(["--root", str(missing)])

    completed = _run_cli(*command)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == (
        f"cli_error:workspace_not_found: Workspace root does not exist: {missing}\n"
    )
    assert "Traceback" not in completed.stderr


def test_cli_rejects_file_as_workspace_without_traceback(tmp_path: Path) -> None:
    source = tmp_path / "UnitA.pas"
    source.write_text("unit UnitA; interface implementation end.\n", encoding="utf-8")

    completed = _run_cli(
        "view",
        "--root",
        str(source),
        "--layer",
        "overview",
        "--format",
        "json",
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == (
        f"cli_error:workspace_not_directory: Workspace root is not a directory: {source}\n"
    )
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("command", ["index", "skill", "opencode"])
def test_cli_reports_filesystem_errors_without_traceback(
    tmp_path: Path,
    command: str,
) -> None:
    if command == "index":
        (tmp_path / "Main.dpr").write_text(
            "program Main; begin end.\n",
            encoding="utf-8",
        )
        arguments = (
            "index",
            "--root",
            str(tmp_path),
            "--out",
            str(tmp_path),
        )
    else:
        target = tmp_path / "not-a-directory"
        target.write_text("content\n", encoding="utf-8")
        arguments = (command, "install", "--target", str(target))

    completed = _run_cli(*arguments)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("cli_error:io_error: ")
    assert "Traceback" not in completed.stderr


def test_cli_reports_parallel_setup_failure_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    def fail(*_args, **_kwargs):
        raise ParallelOutlineError("process workers unavailable")

    monkeypatch.setattr(agent_cli, "build_codebase_index", fail)

    result = agent_cli.main(
        ["view", "--layer", "overview", "--workers", "2"]
    )

    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "cli_error:parallel_failed: process workers unavailable\n"
    )


def test_view_resolves_relative_project_file_from_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "Main.dpr").write_text("program Main; begin end.\n", encoding="utf-8")

    completed = _run_cli(
        "view",
        "--root",
        str(workspace),
        "--project-file",
        "Main.dpr",
        "--layer",
        "overview",
        "--format",
        "json",
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["projects"] == [str((workspace / "Main.dpr").resolve())]
    assert not any(problem["kind"] == "cant_read_project" for problem in payload["problems"])


@pytest.mark.parametrize(
    "arguments",
    [
        ("view", "--layer", "overview", "--format", "json"),
        ("index",),
        ("wiki", "export"),
        ("worker",),
        ("cache", "start", "--startup-timeout", "1"),
    ],
)
def test_cli_reports_invalid_project_toml_without_traceback(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    (tmp_path / "Main.dpr").write_text(
        "program Main; begin end.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".delphi-lsp.toml"
    config_path.write_text("[projects\n", encoding="utf-8")
    command = [*arguments, "--root", str(tmp_path)]
    if arguments[:2] == ("wiki", "export"):
        command.extend(["--out", str(tmp_path / "wiki"), "--quiet"])

    completed = _run_cli(*command)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith(
        f"cli_error:project_config_invalid: {config_path}: Invalid TOML:"
    )
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("command", ["skill", "opencode"])
def test_install_conflict_is_reported_without_traceback(
    tmp_path: Path,
    command: str,
) -> None:
    first = _run_cli(command, "install", "--target", str(tmp_path))
    assert first.returncode == 0

    installed = Path(first.stdout.splitlines()[0])
    installed.write_text("user-owned content\n", encoding="utf-8")
    completed = _run_cli(command, "install", "--target", str(tmp_path))

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("cli_error:install_conflict: ")
    assert "Traceback" not in completed.stderr
