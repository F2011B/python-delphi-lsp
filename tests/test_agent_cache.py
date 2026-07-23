from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import textwrap
import threading
import time
import pytest

from delphi_lsp.agent_cache import (
    BudgetResult,
    CacheBudget,
    CacheStats,
    cache_warning,
    estimate_deep_size,
    parse_memory_size,
)
from delphi_lsp.agent_context import AgentContext


ROOT = Path(__file__).resolve().parents[1]


def test_cache_daemon_lifecycle_reuses_one_authenticated_process(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_metadata_path, cache_status, query_cache, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", """program Demo;
    uses UnitA in 'UnitA.pas';
    begin
    end.""")
    write_source(tmp_path / "UnitA.pas", """unit UnitA;
    interface
    type
      TDemo = class
      end;
    implementation
    end.""")
    try:
        metadata = start_cache(tmp_path, max_memory_bytes=512 * 1024**2, workers=2, startup_timeout=30)
        first = metadata
        second = start_cache(tmp_path, max_memory_bytes=512 * 1024**2, workers=2, startup_timeout=30)
        assert first.pid == second.pid
        response = query_cache(tmp_path, {"action": "open"})
        assert response.payload["schema"] == 2
        status = cache_status(tmp_path)
        assert status["pid"] == first.pid
        assert status["workers_configured"] == 2
        assert status["workers_effective"] == 2
        assert status["parallel_files_completed"] >= 2
        assert status["prewarm_seconds"] >= status["parallel_seconds"] >= 0
        assert status["parallel_fallbacks"] == 0
        rendered = json.dumps(status, sort_keys=True)
        assert metadata.token not in rendered
        metadata_file = cache_metadata_path(tmp_path)
        assert metadata.token in metadata_file.read_text(encoding="utf-8")
        if os.name != "nt":
            assert metadata_file.stat().st_mode & 0o777 == 0o600
            assert metadata_file.parent.stat().st_mode & 0o777 == 0o700
    finally:
        stop_cache(tmp_path)


def test_cache_daemon_compacts_handles_bad_clients_and_idles(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_metadata_path, cache_status, query_cache, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        metadata = start_cache(tmp_path, max_memory_bytes=1, idle_timeout=1)
        with socket.create_connection(("127.0.0.1", metadata.port)) as connection:
            connection.sendall(b"{not json}\n")
            assert b"invalid_request" in connection.recv(4096)
        with socket.create_connection(("127.0.0.1", metadata.port)) as connection:
            connection.sendall(b"\xff\n")
            assert b"invalid_request" in connection.recv(4096)
        with socket.create_connection(("127.0.0.1", metadata.port)) as connection:
            connection.sendall(b"x" * (1024 * 1024 + 1))
            assert b"invalid_request" in connection.recv(4096)
        response = query_cache(tmp_path, {"action": "open"})
        assert response.warning
        status = cache_status(tmp_path)
        assert status["cache_state"] == "compact"
        assert status["evictions"] >= 1
        assert "idle_seconds" in status and "last_activity_at" in status
        deadline = time.monotonic() + 3
        while cache_metadata_path(tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not cache_metadata_path(tmp_path).exists()
        stop_cache(tmp_path)
    finally:
        stop_cache(tmp_path)


def test_cache_daemon_stale_metadata_restarts_and_tracks_source_revision(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_metadata_path, cache_status, query_cache, start_cache, stop_cache

    source = tmp_path / "UnitA.pas"
    write_source(tmp_path / "Demo.dpr", "program Demo; uses UnitA in 'UnitA.pas'; begin end.")
    write_source(source, """unit UnitA;
interface
type
  TOriginal = class
  end;
implementation
end.
""")
    metadata_path = cache_metadata_path(tmp_path)
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(json.dumps({"schema": 1, "root": str(tmp_path.resolve()), "pid": 999999, "port": 1, "token": "x" * 32, "version": "x", "project_file": "", "max_memory_bytes": 1024, "idle_timeout": 10, "started_at": 1.0}), encoding="utf-8")
    if os.name != "nt":
        metadata_path.chmod(0o600)
    try:
        start_cache(tmp_path, max_memory_bytes=1024)
        before = query_cache(tmp_path, {"action": "open"}).payload["workspace_revision"]
        source.write_text("""unit UnitA;
interface
type
  TOriginal = class
  end;
implementation
end. { changed }
""", encoding="utf-8")
        after = query_cache(tmp_path, {"action": "open"}).payload["workspace_revision"]
        assert before != after
        assert cache_status(tmp_path)["invalidations"] >= 1
        source.write_text("""unit UnitA;
interface
type
  TOriginal = class
  end;
  TAdded = class
  end;
implementation
end.
""", encoding="utf-8")
        rebuilt = query_cache(tmp_path, {"action": "find", "query": "TAdded"}).payload
        assert any(item["name"] == "TAdded" for item in rebuilt["result"])
        stop_cache(tmp_path)
        stop_cache(tmp_path)
    finally:
        stop_cache(tmp_path)


def test_cache_daemon_rejects_symlinked_metadata_directory(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, start_cache

    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / ".delphi-lsp").symlink_to(target, target_is_directory=True)
    with pytest.raises(CacheClientError, match="unsafe"):
        start_cache(tmp_path)


def test_live_unreachable_metadata_is_preserved_without_spawning(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, cache_metadata_path, start_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    path = cache_metadata_path(tmp_path)
    path.parent.mkdir(parents=True)
    raw = {"schema": 1, "root": str(tmp_path.resolve()), "pid": os.getpid(), "port": 1, "token": "x" * 32, "version": "x", "project_file": "", "max_memory_bytes": 1024, "idle_timeout": 10, "started_at": 1.0}
    path.write_text(json.dumps(raw), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(CacheClientError, match="Live cache daemon is unavailable"):
        start_cache(tmp_path, max_memory_bytes=1024)
    assert json.loads(path.read_text(encoding="utf-8")) == raw


def test_live_cache_rejects_conflicting_worker_configuration(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        start_cache(tmp_path, workers=2)
        with pytest.raises(CacheClientError) as error:
            start_cache(tmp_path, workers=1)
        assert error.value.code == "configuration_conflict"
    finally:
        stop_cache(tmp_path)


def test_cache_startup_reports_child_diagnostics_on_failure(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, cache_metadata_path, _read_metadata, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    with pytest.raises(CacheClientError) as error:
        start_cache(tmp_path, max_memory_bytes=0)
    assert error.value.code == "startup_failed"
    message = str(error.value)
    assert "Cache daemon did not become ready." in message
    assert "max_bytes must be greater than zero." in message
    assert not re.search(r"[A-Za-z0-9_-]{32,}", message)
    assert cache_metadata_path(tmp_path).exists() is False
    assert _read_metadata(tmp_path) is None
    stop_cache(tmp_path)


def test_startup_timeout_replaces_the_old_ten_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from delphi_lsp import agent_cache

    class PendingProcess:
        pid = 424242

        def __init__(self) -> None:
            self.killed = False
            self.polls = 0

        def poll(self) -> int | None:
            self.polls += 1
            return -9 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self) -> int:
            return -9

    process = PendingProcess()
    ticks = iter((0.0, 11.0, 22.0, 31.0))
    monkeypatch.setattr(agent_cache.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(agent_cache.time, "sleep", lambda _: None)
    monkeypatch.setattr(agent_cache.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(agent_cache, "_read_metadata", lambda _: None)

    with pytest.raises(agent_cache.CacheClientError) as error:
        agent_cache._start_cache_unlocked(tmp_path, startup_timeout=30)

    assert error.value.code == "startup_failed"
    assert process.polls >= 3
    assert process.killed is True


@pytest.mark.parametrize("startup_timeout", [0, -1, float("nan"), float("inf")])
def test_start_cache_rejects_non_positive_startup_timeout(
    tmp_path: Path,
    startup_timeout: float,
) -> None:
    from delphi_lsp.agent_cache import start_cache

    with pytest.raises(ValueError, match="startup_timeout"):
        start_cache(tmp_path, startup_timeout=startup_timeout)


def test_startup_diagnostic_truncates_normalizes_and_redacts_tokens() -> None:
    from delphi_lsp.agent_cache import _truncate_and_sanitize_startup_diagnostics

    raw = b"line1\x00line2\r\nline3\x1b[token=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ABCD]"
    cleaned = _truncate_and_sanitize_startup_diagnostics(raw, max_bytes=16000)
    assert "\x00" not in cleaned
    assert "<redacted>" in cleaned
    assert "line1 line2 line3" in cleaned

    repeated = b"x" * (20000)
    truncated = _truncate_and_sanitize_startup_diagnostics(repeated, max_bytes=16000)
    assert len(truncated) <= 16000


def test_partial_client_disconnect_does_not_stop_daemon(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_status, query_cache, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        metadata = start_cache(tmp_path)
        with socket.create_connection(("127.0.0.1", metadata.port)) as connection:
            connection.sendall(b'{"token":"partial"')
        assert query_cache(tmp_path, {"action": "open"}).payload["schema"] == 2
        assert cache_status(tmp_path)["pid"] == metadata.pid
    finally:
        stop_cache(tmp_path)


def test_idle_client_times_out_without_blocking_daemon_or_idle_shutdown(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_metadata_path, cache_status, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        metadata = start_cache(tmp_path, idle_timeout=3)
        connection = socket.create_connection(("127.0.0.1", metadata.port))
        time.sleep(2.2)
        assert cache_status(tmp_path)["pid"] == metadata.pid
        connection.close()
        deadline = time.monotonic() + 4
        while cache_metadata_path(tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not cache_metadata_path(tmp_path).exists()
    finally:
        stop_cache(tmp_path)


def test_status_polling_does_not_extend_cache_idle_lifetime(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import cache_metadata_path, cache_status, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        start_cache(tmp_path, idle_timeout=1)
        deadline = time.monotonic() + 3
        while cache_metadata_path(tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.2)
            if cache_metadata_path(tmp_path).exists():
                cache_status(tmp_path)
        assert not cache_metadata_path(tmp_path).exists()
    finally:
        stop_cache(tmp_path)


def test_warning_is_consumed_after_compaction_but_remains_while_active(monkeypatch, tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    metadata = CacheMetadata(2, str(tmp_path.resolve()), os.getpid(), 1, "x" * 32, "test", "", 100, 0, 10, time.time())
    service = _CacheService(metadata)
    compacted = BudgetResult(20, 20.0, 120.0, False, True, True)
    active = BudgetResult(80, 80.0, 80.0, True, True, False)
    monkeypatch.setattr(CacheBudget, "enforce", lambda self, **_: compacted)
    service.prewarm()
    assert service.request({"action": "status"}).warning
    assert not service.request({"action": "status"}).warning
    service.last_budget = active
    assert service.request({"action": "status"}).warning
    assert service.request({"action": "status"}).warning


def test_prewarm_only_tolerates_project_selection_error(monkeypatch, tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService
    from delphi_lsp.agent_protocol import AgentProtocolError

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    metadata = CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        1024 * 1024,
        0,
        10,
        time.time(),
    )
    service = _CacheService(metadata)
    monkeypatch.setattr(service.context, "handle", lambda request: (_ for _ in ()).throw(AgentProtocolError("project_required", "Select a project.")))
    service.prewarm()
    assert service.cache_state == "ready"
    monkeypatch.setattr(service.context, "handle", lambda request: (_ for _ in ()).throw(AgentProtocolError("invalid_request", "Bad request.")))
    with pytest.raises(AgentProtocolError, match="Bad request"):
        service.prewarm()


def test_concurrent_starts_reuse_one_daemon(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    results: list[int] = []
    errors: list[Exception] = []
    def start() -> None:
        try:
            results.append(start_cache(tmp_path).pid)
        except Exception as error:
            errors.append(error)
    workers = [threading.Thread(target=start) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    try:
        assert not errors
        assert len(set(results)) == 1
    finally:
        stop_cache(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata permissions")
def test_metadata_reader_rejects_symlink_and_unsafe_permissions(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, cache_metadata_path, query_cache, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        start_cache(tmp_path)
        path = cache_metadata_path(tmp_path)
        path.chmod(0o644)
        with pytest.raises(CacheClientError, match="unsafe"):
            query_cache(tmp_path, {"action": "open"})
        path.chmod(0o600)
        target = path.with_name("target.json")
        path.replace(target)
        path.symlink_to(target)
        with pytest.raises(CacheClientError, match="unsafe"):
            query_cache(tmp_path, {"action": "open"})
        path.unlink()
        target.replace(path)
        assert query_cache(tmp_path, {"action": "open"}).payload["schema"] == 2
    finally:
        stop_cache(tmp_path)


def test_cache_daemon_rejects_invalid_auth_without_dying(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import CacheClientError, cache_metadata_path, query_cache, start_cache, stop_cache
    import json

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        metadata = start_cache(tmp_path)
        path = cache_metadata_path(tmp_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["token"] = "0" * 32
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CacheClientError, match="authentication failed"):
            query_cache(tmp_path, {"action": "find", "query": "Demo"})
        raw["token"] = metadata.token
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert query_cache(tmp_path, {"action": "find", "query": "Demo"}).payload["schema"] == 2
    finally:
        stop_cache(tmp_path)


def test_readme_documents_bounded_cache_daemon_commands_and_retention_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    cli_contracts = [
        "delphi-lsp-agent cache start --root PATH",
        "delphi-lsp-agent cache status --root PATH",
        "delphi-lsp-agent cache stop --root PATH",
        "delphi-lsp-agent query --root PATH find TCustomer",
        "delphi-lsp-agent query --root PATH focus TARGET_ID",
        "delphi-lsp-agent query --root PATH inspect",
        "delphi-lsp-agent query --root PATH trace TARGET_ID --relation callers",
        "delphi-lsp-agent query --root PATH metrics UNIT_QUERY",
    ]
    for contract in cli_contracts:
        assert contract in readme

    assert "512 MiB" in readme
    assert "retained-cache budget" in readme
    assert "one daemon per canonical root" in readme
    assert "prewarms the navigation cache" in readme
    assert "not a hard RSS/parse peak" in readme
    assert "source revision" in readme
    assert "Warnings are emitted on stderr" in readme
    assert "80 percent" in readme
    assert "cache lifecycle JSON" in readme
    assert "stop status JSON" in readme
    assert "status JSON" in readme
    assert "Protocol v2 JSON" in readme
    assert "writes warnings to stderr" in readme
    assert "Eviction is ordered" in readme
    assert "auxiliary caches are evicted first" in readme
    assert "rebuilds the navigation state on demand" in readme
    assert "30-minute idle" in readme
    assert ".delphi-lsp/agent-cache/daemon.json" in readme
    assert "owner-only token" in readme
    assert "Do not copy or share this token" in readme
    assert "--workers auto|N" in readme
    assert "--startup-timeout 120" in readme
    assert "four worker processes" in readme
    assert "128 MiB" in readme
    assert "spawn" in readme
    assert "short-lived" in readme
    assert "transient worker memory" in readme
    assert "automatic serial fallback" in readme
    assert "workers_configured" in readme
    assert "workers_effective" in readme
    assert "parallel_files_completed" in readme
    assert "prewarm_seconds" in readme
    assert "parallel_seconds" in readme
    assert "parallel_fallbacks" in readme

def write_source(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")


def test_estimate_deep_size_handles_cycles_dataclasses_and_slots() -> None:
    @dataclass
    class Payload:
        values: list[object]

    class SlotPayload:
        __slots__ = ("payload", "__weakref__")

        def __init__(self, payload: object) -> None:
            self.payload = payload

    cyclic: list[object] = []
    cyclic.append(cyclic)
    value = SlotPayload(Payload([cyclic, {"payload": "value"}]))

    assert estimate_deep_size(value) > 0


def test_estimate_deep_size_counts_opaque_objects_without_introspection() -> None:
    class Opaque:
        def __getattribute__(self, name: str) -> object:
            if name == "__dict__":
                raise RuntimeError("opaque object")
            return super().__getattribute__(name)

    assert estimate_deep_size(Opaque()) > 0


def test_estimate_deep_size_ignores_broken_size_and_mapping_enumeration() -> None:
    class BrokenSize:
        def __sizeof__(self) -> int:
            raise RuntimeError("size unavailable")

    class BrokenMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def items(self):
            raise RuntimeError("items unavailable")

    assert estimate_deep_size(BrokenSize()) >= 0
    assert estimate_deep_size(BrokenMapping()) >= 0


def test_estimate_deep_size_reads_slotted_dataclass_fields_once() -> None:
    reads = 0

    @dataclass(slots=True)
    class SlottedPayload:
        value: object

        def __getattribute__(self, name: str) -> object:
            nonlocal reads
            if name == "value":
                reads += 1
            return super().__getattribute__(name)

    assert estimate_deep_size(SlottedPayload("payload")) > 0
    assert reads == 1


def test_navigation_cache_eviction_preserves_selection_and_rebuilds(tmp_path: Path) -> None:
    write_source(
        tmp_path / "Main.dpr",
        """
        program Main;
        uses UnitA in 'UnitA.pas';
        begin
        end.
        """,
    )
    write_source(
        tmp_path / "UnitA.pas",
        """
        unit UnitA;
        interface
        type
          TCustomer = class
          end;
        implementation
        end.
        """,
    )

    context = AgentContext.open(tmp_path)
    result = context.handle({"action": "find", "query": "TCustomer"})

    assert [item["name"] for item in result.result] == ["TCustomer"]
    assert context.navigation_cache_is_warm
    assert estimate_deep_size(context.cache_roots()) > 0
    active_project_id = context.workspace.active_project_id

    context.evict_auxiliary_caches()

    assert context.navigation_cache_is_warm

    context.evict_navigation_caches()

    assert not context.navigation_cache_is_warm
    assert context.workspace.active_project_id == active_project_id

    rebuilt = context.handle({"action": "find", "query": "TCustomer"})

    assert [item["name"] for item in rebuilt.result] == ["TCustomer"]
    assert context.navigation_cache_is_warm


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512M", 512 * 1024**2),
        ("1G", 1024**3),
        ("4096K", 4096 * 1024),
        ("1048576", 1048576),
    ],
)
def test_parse_memory_size(text: str, expected: int) -> None:
    assert parse_memory_size(text) == expected


@pytest.mark.parametrize(
    "text",
    ["0", "-1", "0K", "-2G", "1024T", "12.5M", ""],
)
def test_parse_memory_size_rejects_invalid_values(text: str) -> None:
    with pytest.raises(ValueError, match="Memory size must be a positive integer with optional K, M, or G suffix."):
        parse_memory_size(text)


def test_cache_stats_defaults() -> None:
    stats = CacheStats()

    assert stats.requests == 0
    assert stats.warm_hits == 0
    assert stats.rebuilds == 0
    assert stats.invalidations == 0
    assert stats.evictions == 0
    assert stats.parallel_fallbacks == 0


def test_warning_threshold_is_inclusive_and_evictions_are_ordered_and_compacted() -> None:
    calls: list[str] = []
    sizes = iter([80, 101, 90, 20])

    budget = CacheBudget(max_bytes=100, warning_percent=80)
    first = budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert first.warning_active is True
    assert first.warning_triggered is True
    assert first.compacted is False
    assert first.retained_bytes == 80
    assert calls == []

    compact_budget = CacheBudget(max_bytes=80, warning_percent=80)
    second = compact_budget.enforce(
        measure=lambda: next(sizes),
        evict_auxiliary=lambda: calls.append("auxiliary"),
        evict_navigation=lambda: calls.append("navigation"),
    )
    assert second.warning_active is False
    assert second.warning_triggered is True
    assert second.compacted is True
    assert second.utilization_percent == 25.0
    assert second.peak_utilization_percent == 126.25
    assert second.warning_triggered is True
    assert second.retained_bytes == 20
    assert calls == ["auxiliary", "navigation"]


@pytest.mark.parametrize(
    ("max_bytes", "warning_percent"),
    [
        (0, 80),
        (-1, 80),
        (100, 0),
        (100, 101),
        (100, -10),
    ],
)
def test_cache_budget_rejects_invalid_configuration(max_bytes: int, warning_percent: int) -> None:
    with pytest.raises(ValueError):
        CacheBudget(max_bytes=max_bytes, warning_percent=warning_percent)


def test_cache_warning_reports_peak_and_compaction_action_when_compacted() -> None:
    result = BudgetResult(
        retained_bytes=20,
        utilization_percent=20.0,
        peak_utilization_percent=126.3,
        warning_active=False,
        warning_triggered=True,
        compacted=True,
    )

    assert cache_warning(result, max_bytes=100) == (
        "Warning: Delphi cache peaked at 126.3% of the 100 byte budget; 20 bytes remain retained after compaction. "
        "Cache compacted. "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )


def test_cache_warning_reports_current_retention_without_compaction() -> None:
    result = BudgetResult(
        retained_bytes=80,
        utilization_percent=80.0,
        peak_utilization_percent=130.0,
        warning_active=True,
        warning_triggered=True,
        compacted=False,
    )

    assert cache_warning(result, max_bytes=100) == (
        "Warning: Delphi cache currently at 80.0% of the 100 byte budget; 80 bytes retained. "
        "Increase --max-memory, stop unused daemons, or allow compact mode."
    )


def test_cache_warning_empty_when_threshold_not_reached() -> None:
    result = BudgetResult(
        retained_bytes=20,
        utilization_percent=20.0,
        peak_utilization_percent=50.0,
        warning_active=False,
        warning_triggered=False,
        compacted=False,
    )

    assert cache_warning(result, max_bytes=100) == ""
