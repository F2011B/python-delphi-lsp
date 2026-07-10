from __future__ import annotations

import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from delphi_lsp import agent_cli
from delphi_lsp.agent_protocol import AgentProtocolError


def _write_source(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")


def _worker(
    root: Path,
    payload: bytes,
    *,
    project_file: Path | None = None,
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

    worker = parser.parse_args(["worker", "--root", "workspace", "--project-file", "Main.dpr"])
    view = parser.parse_args(["view", "--layer", "overview"])

    assert worker.command == "worker"
    assert worker.root == Path("workspace")
    assert worker.project_file == Path("Main.dpr")
    assert view.command == "view"
    assert view.layer == "overview"


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
