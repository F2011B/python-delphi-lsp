from __future__ import annotations

import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from delphi_lsp import agent_cli
from delphi_lsp.agent_cache import DEFAULT_MAX_MEMORY_BYTES
from delphi_lsp.agent_protocol import AgentProtocolError


def _write_source(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def _worker(
    root: Path,
    payload: bytes,
    *,
    project_file: Path | None = None,
    workers: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        sys.executable,
        "-m",
        "delphi_lsp.agent_cli",
        "worker",
        "--root",
        str(root),
    ]
    if project_file is not None:
        command.extend(["--project-file", str(project_file)])
    if workers is not None:
        command.extend(["--workers", workers])
    return subprocess.run(
        command,
        input=payload,
        capture_output=True,
        check=False,
    )


def _lines(completed: subprocess.CompletedProcess[bytes]) -> list[dict[str, object]]:
    return [json.loads(line) for line in completed.stdout.splitlines()]


def test_parser_adds_worker_without_changing_legacy_commands() -> None:
    parser = agent_cli.build_parser()

    worker = parser.parse_args(["worker", "--root", "workspace", "--project-file", "Main.dpr", "--workers", "2"])
    auto_worker = parser.parse_args(["worker", "--root", "workspace", "--workers", "auto"])
    view = parser.parse_args(["view", "--layer", "overview", "--workers", "3"])
    metrics = parser.parse_args(["view", "--layer", "metrics"])
    index = parser.parse_args(["index", "--workers", "auto"])

    assert worker.command == "worker"
    assert worker.root == Path("workspace")
    assert worker.project_file == Path("Main.dpr")
    assert worker.workers == 2
    assert auto_worker.workers == 0
    assert view.command == "view"
    assert view.layer == "overview"
    assert view.workers == 3
    assert metrics.layer == "metrics"
    assert metrics.workers == 0
    assert index.workers == 0


def test_worker_parallel_output_is_deterministic(tmp_path: Path) -> None:
    for name in ("Alpha", "Bravo", "Charlie"):
        _write_source(
            tmp_path / f"{name}.pas",
            f"unit {name}; interface type T{name} = class end; implementation end.\n",
        )

    payload = b'{"action":"find","query":"T","max_items":20}\n'
    serial = _worker(tmp_path, payload, workers="1")
    parallel = _worker(tmp_path, payload, workers="2")

    assert serial.returncode == parallel.returncode == 0
    assert _lines(parallel) == _lines(serial)
    assert parallel.stderr == b""


def test_parser_adds_cache_lifecycle_and_ergonomic_query_commands() -> None:
    parser = agent_cli.build_parser()

    start = parser.parse_args(["cache", "start"])
    configured_start = parser.parse_args(
        ["cache", "start", "--workers", "2", "--startup-timeout", "45.5"]
    )
    status = parser.parse_args(["cache", "status", "--root", "workspace", "--format", "json"])
    stop = parser.parse_args(["cache", "stop", "--root", "workspace"])
    serve = parser.parse_args(
        [
            "cache",
            "serve",
            "--root",
            "workspace",
            "--max-memory",
            "2M",
            "--workers",
            "2",
            "--idle-timeout",
            "90",
        ]
    )
    query = parser.parse_args(
        ["query", "--root", "workspace", "find", "TCustomer", "--project-id", "Main.dpr", "--max-items", "4"]
    )
    defaults = parser.parse_args(["query", "open"])

    assert start.cache_command == "start"
    assert start.root == Path(".")
    assert start.max_memory == DEFAULT_MAX_MEMORY_BYTES
    assert start.idle_timeout == 1800
    assert start.workers == 0
    assert start.startup_timeout == 120.0
    assert configured_start.workers == 2
    assert configured_start.startup_timeout == 45.5
    assert status.format == "json"
    assert stop.root == Path("workspace")
    assert serve.max_memory == 2 * 1024**2
    assert serve.workers == 2
    assert serve.idle_timeout == 90
    assert query.action == "find"
    assert query.value == "TCustomer"
    assert query.project_id == "Main.dpr"
    assert query.max_items == 4
    assert defaults.value == ""
    assert defaults.project_id == ""
    assert defaults.detail == "summary"
    assert defaults.relation is None
    assert defaults.cursor == ""
    assert defaults.max_items == 12
    assert defaults.max_chars == 12000
    with pytest.raises(SystemExit):
        parser.parse_args(["cache", "serve", "--root", "workspace"])


def test_query_maps_project_id_value_and_protocol_defaults(monkeypatch, capsys) -> None:
    args = agent_cli.build_parser().parse_args(
        ["query", "--root", "workspace", "find", "TCustomer", "--project-id", "Main.dpr"]
    )
    captured: dict[str, object] = {}

    def query(root: Path, request: dict[str, object]) -> SimpleNamespace:
        captured["root"] = root
        captured["request"] = request
        return SimpleNamespace(payload={"schema": 2}, warning="")

    monkeypatch.setattr(agent_cli, "query_cache", query)

    assert agent_cli._query(args) == 0
    assert captured == {
        "root": Path("workspace"),
        "request": {
            "action": "find",
            "query": "TCustomer",
            "project_id": "Main.dpr",
            "detail": "summary",
            "cursor": "",
            "max_items": 12,
            "max_chars": 12000,
        },
    }
    assert capsys.readouterr().out == '{"schema":2}\n'


def test_cache_cli_lifecycle_query_and_warning_streams(tmp_path: Path) -> None:
    _write_source(
        tmp_path / "Customer.pas",
        """unit Customer;
interface
type
  TCustomer = class
  end;
implementation
end.
""",
    )
    start = subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "start", "--root", str(tmp_path), "--max-memory", "1K"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        started = json.loads(start.stdout)
        assert start.returncode == 0
        assert started["pid"] > 0
        assert "Warning:" in start.stderr

        query = subprocess.run(
            [sys.executable, "-m", "delphi_lsp.agent_cli", "query", "--root", str(tmp_path), "find", "TCustomer"],
            capture_output=True,
            text=True,
            check=False,
        )
        response = json.loads(query.stdout)
        assert query.returncode == 0
        assert any(item["name"] == "TCustomer" for item in response["result"])
        assert "Warning:" in query.stderr

        status = subprocess.run(
            [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "status", "--root", str(tmp_path), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        reported = json.loads(status.stdout)
        assert status.returncode == 0
        assert reported["pid"] == started["pid"]
        assert reported["warning_threshold_percent"] == 80
        assert status.stderr == ""

        text_status = subprocess.run(
            [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "status", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert text_status.returncode == 0
        assert text_status.stdout.startswith(f"running pid={started['pid']} state=")
        assert text_status.stderr == ""
    finally:
        stop = subprocess.run(
            [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "stop", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    assert stop.returncode == 0
    assert json.loads(stop.stdout) == {"stopped": True}
    assert stop.stderr == ""


def test_query_does_not_start_a_missing_cache_and_sanitizes_errors(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "query", "--root", str(tmp_path), "find", "TCustomer"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "cache_error:cache_not_running: Cache daemon is not running.\n"


def test_cache_stop_cleans_missing_or_stale_metadata_idempotently(tmp_path: Path) -> None:
    missing = subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "stop", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 0
    assert json.loads(missing.stdout) == {"stopped": False}

    metadata = tmp_path / ".delphi-lsp" / "agent-cache" / "daemon.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"schema": 1, "root": str(tmp_path.resolve()), "pid": 999999, "port": 1, "token": "x" * 32, "version": "x", "project_file": "", "max_memory_bytes": 1024, "idle_timeout": 10, "started_at": 1.0}), encoding="utf-8")
    if os.name != "nt":
        metadata.chmod(0o600)
    stale = subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "cache", "stop", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 0
    assert json.loads(stale.stdout) == {"stopped": False}
    assert not metadata.exists()


def test_worker_preserves_focus_across_requests_in_one_process(tmp_path: Path) -> None:
    _write_source(
        tmp_path / "Main.pas",
        """unit Main;
interface
type
  TWorker = class
    procedure Run;
  end;
implementation
end.
""",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "worker", "--root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(b'{"action":"find","query":"TWorker"}\n')
    process.stdin.flush()
    found = json.loads(process.stdout.readline())
    target_id = found["result"][0]["target_id"]
    process.stdin.write(json.dumps({"action": "focus", "target_id": target_id}).encode("utf-8") + b"\n")
    process.stdin.flush()
    focused = json.loads(process.stdout.readline())
    process.stdin.write(b'{"action":"inspect"}\n')
    process.stdin.close()
    inspected = json.loads(process.stdout.readline())
    stderr = process.stderr.read()
    assert process.wait(timeout=5) == 0

    assert focused["focus"]["target_id"] == target_id
    assert inspected["focus"]["target_id"] == target_id
    assert inspected["schema"] == 2
    assert stderr == b""


def test_worker_survives_malformed_json_and_protocol_errors(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")

    completed = _worker(
        tmp_path,
        b"{not json}\n{\"action\":\"jump\"}\n{\"action\":\"open\"}\n",
    )
    responses = _lines(completed)

    assert completed.returncode == 0
    assert responses[0] == {"schema": 2, "error": {"code": "invalid_json", "message": "Invalid JSON request."}}
    assert responses[1] == {
        "schema": 2,
        "error": {
            "code": "invalid_action",
            "message": "Unsupported action value: 'jump'.",
        },
    }
    assert responses[2]["schema"] == 2
    assert completed.stderr == b""


def test_worker_ignores_blank_lf_records_and_emits_exact_success_shape(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")

    completed = _worker(tmp_path, b"\n \t\n{\"action\":\"open\"}\n\n")
    raw_lines = completed.stdout.splitlines()
    response = json.loads(raw_lines[0])

    assert completed.returncode == 0
    assert len(raw_lines) == 1
    assert set(response) == {
        "schema",
        "workspace_revision",
        "focus",
        "result",
        "page",
        "context",
    }
    assert response["schema"] == 2
    assert completed.stderr == b""


def test_worker_project_file_selects_and_opens_that_project(tmp_path: Path) -> None:
    _write_source(tmp_path / "A.dpr", "program A; begin end.\n")
    _write_source(tmp_path / "B.dpr", "program B; begin end.\n")

    completed = _worker(
        tmp_path,
        b'{"action":"open","max_items":50,"max_chars":40000}\n',
        project_file=Path("B.dpr"),
    )
    response = _lines(completed)[0]
    projects = [item for item in response["result"] if item["item_type"] == "project"]
    units = [item for item in response["result"] if item["item_type"] == "unit"]

    assert completed.returncode == 0
    assert [(item["name"], item["active"]) for item in projects] == [("B", True)]
    assert [item["name"] for item in units] == ["B"]
    assert response["focus"]["project_id"] == projects[0]["project_id"]
    assert completed.stderr == b""


def test_worker_accepts_crlf_and_unicode_requests(tmp_path: Path) -> None:
    _write_source(tmp_path / "Grüße.pas", "unit Grüße; interface implementation end.\n")

    completed = _worker(tmp_path, '{"action":"open","query":"Grüße"}\r\n'.encode("utf-8"))

    assert completed.returncode == 0
    assert _lines(completed)[0]["schema"] == 2
    assert completed.stderr == b""


def test_worker_writes_utf8_when_pythonioencoding_is_ascii(tmp_path: Path) -> None:
    _write_source(tmp_path / "Grüße.pas", "unit Grüße; interface implementation end.\n")
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "ascii:strict"

    completed = subprocess.run(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "worker", "--root", str(tmp_path)],
        input=b'{"action":"open"}\n',
        capture_output=True,
        check=False,
        env=environment,
    )
    decoded = completed.stdout.decode("utf-8")
    response = json.loads(decoded)

    assert completed.returncode == 0
    assert any(item.get("name") == "Grüße" for item in response["result"])
    assert "internal_error" not in decoded
    assert completed.stderr == b""


def test_worker_drains_oversize_record_then_serves_later_request(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")

    completed = _worker(tmp_path, b"{" + (b" " * (1024 * 1024)) + b"}\n{\"action\":\"open\"}\n")
    responses = _lines(completed)

    assert completed.returncode == 0
    assert responses[0] == {"schema": 2, "error": {"code": "request_too_large", "message": "Request exceeds the 1 MiB limit."}}
    assert responses[1]["schema"] == 2
    assert completed.stderr == b""


def test_worker_reports_unterminated_oversize_record_before_newline(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")
    process = subprocess.Popen(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "worker", "--root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    first_line: queue.Queue[bytes] = queue.Queue()
    reader = threading.Thread(target=lambda: first_line.put(process.stdout.readline()), daemon=True)
    reader.start()

    try:
        process.stdin.write(b"x" * ((1024 * 1024) + 1))
        process.stdin.flush()
        try:
            oversized = first_line.get(timeout=3)
        except queue.Empty:
            pytest.fail("worker did not report an unterminated oversized record before newline")

        assert json.loads(oversized) == {
            "schema": 2,
            "error": {
                "code": "request_too_large",
                "message": "Request exceeds the 1 MiB limit.",
            },
        }

        process.stdin.write(b"discarded tail\n{\"action\":\"open\"}\n")
        process.stdin.flush()
        recovered = json.loads(process.stdout.readline())
        process.stdin.close()

        assert recovered["schema"] == 2
        assert process.wait(timeout=5) == 0
        assert process.stderr.read() == b""
        assert process.stdout.readline() == b""
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=1)


def test_worker_cleanly_exits_at_eof_with_ndjson_only(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")

    completed = _worker(tmp_path, b"")

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_worker_broken_stdout_exits_one_without_shutdown_warning(tmp_path: Path) -> None:
    _write_source(tmp_path / "Main.dpr", "program Main; begin end.\n")
    process = subprocess.Popen(
        [sys.executable, "-m", "delphi_lsp.agent_cli", "worker", "--root", str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdout.close()

    try:
        process.stdin.write(b'{"action":"open"}\n')
        process.stdin.close()
        assert process.wait(timeout=5) == 1
        assert process.stderr.read() == b""
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_main_flushes_stdout_and_hard_exits_after_a_deferred_broken_pipe(monkeypatch) -> None:
    class Parser:
        def parse_args(self, argv):
            return SimpleNamespace(func=lambda args: None)

    class BrokenStdout:
        encoding = "utf-8"

        def flush(self) -> None:
            raise BrokenPipeError

        def close(self) -> None:
            raise BrokenPipeError

    monkeypatch.setattr(agent_cli, "build_parser", Parser)
    broken_stdout = BrokenStdout()
    monkeypatch.setattr(agent_cli.sys, "stdout", broken_stdout)
    monkeypatch.setattr(agent_cli.sys, "__stdout__", broken_stdout)
    exit_codes: list[int] = []

    def exit_process(code: int) -> None:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(agent_cli.os, "_exit", exit_process)

    with pytest.raises(SystemExit) as exit_info:
        agent_cli.main([])

    assert exit_info.value.code == 1
    assert exit_codes == [1]
    assert agent_cli.sys.stdout is agent_cli.sys.__stdout__


def test_worker_reports_invalid_encoding_and_hides_internal_failures() -> None:
    class BrokenContext:
        def handle(self, request: object) -> object:
            raise RuntimeError("/private/source.pas must not escape")

    output = io.BytesIO()
    errors = io.StringIO()

    agent_cli._serve_worker(
        BrokenContext(),
        io.BytesIO(b"\xff\n{\"action\":\"open\"}\n"),
        output,
        errors,
    )

    assert [json.loads(line) for line in output.getvalue().splitlines()] == [
        {"schema": 2, "error": {"code": "invalid_encoding", "message": "Invalid UTF-8 request."}},
        {"schema": 2, "error": {"code": "internal_error", "message": "Internal request error."}},
    ]
    assert errors.getvalue() == "RuntimeError\n"


def test_worker_treats_output_oserror_as_transport_failure_without_logging() -> None:
    class StaticResponse:
        def to_mapping(self) -> dict[str, object]:
            return {"schema": 2, "result": []}

    class StaticContext:
        def handle(self, request: object) -> StaticResponse:
            return StaticResponse()

    class BrokenOutput:
        def write(self, data: bytes) -> int:
            raise OSError("closed pipe")

        def flush(self) -> None:
            raise AssertionError("flush must not run after a failed write")

    errors = io.StringIO()

    with pytest.raises(BrokenPipeError):
        agent_cli._serve_worker(
            StaticContext(),
            io.BytesIO(b'{"action":"open"}\n'),
            BrokenOutput(),
            errors,
        )

    assert errors.getvalue() == ""


def test_worker_redacts_source_unavailable_protocol_details() -> None:
    class MissingSourceContext:
        def handle(self, request: object) -> object:
            raise AgentProtocolError(
                "source_unavailable",
                "Could not read selected source /private/secret/Unit.pas: permission denied.",
            )

    output = io.BytesIO()
    errors = io.StringIO()

    agent_cli._serve_worker(
        MissingSourceContext(),
        io.BytesIO(b'{"action":"find","query":"TSecret"}\n'),
        output,
        errors,
    )

    assert json.loads(output.getvalue()) == {
        "schema": 2,
        "error": {
            "code": "source_unavailable",
            "message": "Selected source is unavailable.",
        },
    }
    assert b"/private/secret/Unit.pas" not in output.getvalue()
    assert b"permission denied" not in output.getvalue()
    assert errors.getvalue() == ""


def test_worker_flushes_after_every_nonblank_record() -> None:
    class StaticResponse:
        def to_mapping(self) -> dict[str, object]:
            return {"schema": 2, "result": []}

    class StaticContext:
        def handle(self, request: object) -> StaticResponse:
            return StaticResponse()

    class TrackingOutput(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_count = 0

        def flush(self) -> None:
            self.flush_count += 1
            super().flush()

    output = TrackingOutput()

    agent_cli._serve_worker(
        StaticContext(),
        io.BytesIO(b'{"action":"open"}\n{bad json}\n\n{\"action\":\"open\"}\n'),
        output,
        io.StringIO(),
    )

    assert output.flush_count == 3
    assert len(output.getvalue().splitlines()) == 3
