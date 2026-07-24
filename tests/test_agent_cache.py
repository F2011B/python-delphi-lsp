from __future__ import annotations

from collections.abc import Iterator, Mapping
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import sys
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


def test_workspace_change_watcher_invalidates_revision_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from delphi_lsp import agent_cache

    stop = threading.Event()
    invalidations: list[None] = []

    def changed(*_args, **_kwargs):
        yield {(1, str(tmp_path / "Changed.pas"))}
        stop.set()

    monkeypatch.setattr(agent_cache, "watch", changed)

    agent_cache.watch_workspace_changes(
        tmp_path,
        stop_event=stop,
        on_change=lambda: invalidations.append(None),
    )

    assert invalidations == [None]


def test_workspace_change_watcher_invalidates_when_backend_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from delphi_lsp import agent_cache

    def failed_watch(*_args, **_kwargs):
        raise OSError("watch backend unavailable")

    invalidations: list[None] = []
    monkeypatch.setattr(agent_cache, "watch", failed_watch)

    agent_cache.watch_workspace_changes(
        tmp_path,
        stop_event=threading.Event(),
        on_change=lambda: invalidations.append(None),
    )

    assert invalidations == [None]


def test_current_process_rss_dispatches_platform_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    monkeypatch.setattr(agent_cache.sys, "platform", "darwin")
    monkeypatch.setattr(agent_cache, "_darwin_process_rss_bytes", lambda: 123)
    assert agent_cache.current_process_rss_bytes() == 123

    monkeypatch.setattr(agent_cache.sys, "platform", "linux")
    monkeypatch.setattr(
        agent_cache.Path,
        "read_text",
        lambda _path, **_options: "100 12 0 0 0 0 0",
    )
    monkeypatch.setattr(agent_cache.os, "sysconf", lambda _name: 4096, raising=False)
    assert agent_cache.current_process_rss_bytes() == 12 * 4096

    monkeypatch.setattr(agent_cache.sys, "platform", "win32")
    monkeypatch.setattr(agent_cache.os, "name", "nt")
    monkeypatch.setattr(agent_cache, "_windows_process_rss_bytes", lambda: 456)
    assert agent_cache.current_process_rss_bytes() == 456


def test_current_process_rss_returns_zero_when_measurement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    monkeypatch.setattr(agent_cache.sys, "platform", "darwin")
    monkeypatch.setattr(
        agent_cache,
        "_darwin_process_rss_bytes",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    assert agent_cache.current_process_rss_bytes() == 0


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
        assert response.payload["schema"] == 3
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


def test_cache_daemon_reports_ready_before_slow_prewarm_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    prewarm_started = threading.Event()
    allow_prewarm = threading.Event()
    real_prewarm = agent_cache._CacheService.prewarm

    def slow_prewarm(service: agent_cache._CacheService) -> None:
        prewarm_started.set()
        assert allow_prewarm.wait(timeout=5)
        real_prewarm(service)

    monkeypatch.setattr(agent_cache._CacheService, "prewarm", slow_prewarm)
    daemon = threading.Thread(
        target=agent_cache.run_cache_daemon,
        args=(tmp_path,),
        kwargs={"idle_timeout": 10},
        daemon=True,
    )
    daemon.start()

    try:
        assert prewarm_started.wait(timeout=2)
        deadline = time.monotonic() + 2
        metadata = agent_cache._read_metadata(tmp_path)
        while metadata is None and time.monotonic() < deadline:
            time.sleep(0.01)
            metadata = agent_cache._read_metadata(tmp_path)

        assert metadata is not None
        status = agent_cache._client_exchange(
            metadata,
            {"action": "status", "_startup_probe": True},
        ).payload
        assert status["cache_state"] == "warming"
    finally:
        allow_prewarm.set()
        deadline = time.monotonic() + 2
        metadata = agent_cache._read_metadata(tmp_path)
        while metadata is None and time.monotonic() < deadline:
            time.sleep(0.01)
            metadata = agent_cache._read_metadata(tmp_path)
        if metadata is not None:
            with contextlib.suppress(agent_cache.CacheClientError):
                agent_cache._client_exchange(metadata, {"action": "stop"})
        daemon.join(timeout=3)


def test_cache_daemon_reports_ready_before_slow_workspace_discovery_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    discovery_started = threading.Event()
    allow_discovery = threading.Event()
    real_open = agent_cache.AgentContext.open

    def slow_open(*args, **kwargs):  # noqa: ANN002, ANN003
        discovery_started.set()
        assert allow_discovery.wait(timeout=5)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(agent_cache.AgentContext, "open", slow_open)
    daemon = threading.Thread(
        target=agent_cache.run_cache_daemon,
        args=(tmp_path,),
        kwargs={"idle_timeout": 10},
        daemon=True,
    )
    daemon.start()

    try:
        assert discovery_started.wait(timeout=2)
        deadline = time.monotonic() + 2
        metadata = agent_cache._read_metadata(tmp_path)
        while metadata is None and time.monotonic() < deadline:
            time.sleep(0.01)
            metadata = agent_cache._read_metadata(tmp_path)

        assert metadata is not None
        status = agent_cache._client_exchange(
            metadata,
            {"action": "status", "_startup_probe": True},
        ).payload
        assert status["cache_state"] == "warming"
        assert status["workspace_revision"] == ""
    finally:
        allow_discovery.set()
        deadline = time.monotonic() + 2
        metadata = agent_cache._read_metadata(tmp_path)
        while metadata is None and time.monotonic() < deadline:
            time.sleep(0.01)
            metadata = agent_cache._read_metadata(tmp_path)
        if metadata is not None:
            with contextlib.suppress(agent_cache.CacheClientError):
                agent_cache._client_exchange(metadata, {"action": "stop"})
        daemon.join(timeout=3)


def test_query_cache_retries_while_daemon_is_warming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    metadata = agent_cache.CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        512 * 1024**2,
        0,
        10,
        time.time(),
    )
    attempts = 0

    def exchange(
        _metadata: agent_cache.CacheMetadata,
        _request: dict[str, object],
    ) -> agent_cache.CacheClientResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise agent_cache.CacheClientError(
                "cache_warming",
                "Cache is still warming.",
            )
        return agent_cache.CacheClientResponse({"schema": 2})

    monkeypatch.setattr(agent_cache, "_read_metadata", lambda _root: metadata)
    monkeypatch.setattr(agent_cache, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(agent_cache, "_client_exchange", exchange)
    monkeypatch.setattr(agent_cache.time, "sleep", lambda _seconds: None)

    response = agent_cache.query_cache(tmp_path, {"action": "open"})

    assert response.payload == {"schema": 2}
    assert attempts == 2


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
        deadline = time.monotonic() + 5
        after = before
        while after == before and time.monotonic() < deadline:
            after = query_cache(tmp_path, {"action": "open"}).payload["workspace_revision"]
            if after == before:
                time.sleep(0.05)
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
        deadline = time.monotonic() + 5
        rebuilt = {"result": []}
        while time.monotonic() < deadline:
            rebuilt = query_cache(tmp_path, {"action": "find", "query": "TAdded"}).payload
            if any(item["name"] == "TAdded" for item in rebuilt["result"]):
                break
            time.sleep(0.05)
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
    assert "timed out after 30.0s" in error.value.message
    assert str(tmp_path.resolve()) in error.value.message
    assert sys.executable in error.value.message


def test_windows_cache_daemon_starts_without_a_console_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    create_no_window = 0x08000000
    create_new_process_group = 0x00000200
    detached_process = 0x00000008
    monkeypatch.setattr(agent_cache.os, "name", "nt")
    monkeypatch.setattr(
        agent_cache.subprocess,
        "CREATE_NO_WINDOW",
        create_no_window,
        raising=False,
    )
    monkeypatch.setattr(
        agent_cache.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        create_new_process_group,
        raising=False,
    )
    monkeypatch.setattr(
        agent_cache.subprocess,
        "DETACHED_PROCESS",
        detached_process,
        raising=False,
    )

    options = agent_cache._daemon_process_options()

    assert options["creationflags"] == (
        create_no_window | create_new_process_group
    )
    assert options["creationflags"] & detached_process == 0
    assert options["stdin"] is agent_cache.subprocess.DEVNULL
    assert options["stdout"] is agent_cache.subprocess.DEVNULL
    assert "start_new_session" not in options


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
        assert query_cache(tmp_path, {"action": "open"}).payload["schema"] == 3
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
    from delphi_lsp.agent_cache import CacheClientError, cache_metadata_path, cache_status, start_cache, stop_cache

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    try:
        start_cache(tmp_path, idle_timeout=1)
        deadline = time.monotonic() + 3
        while cache_metadata_path(tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.2)
            if cache_metadata_path(tmp_path).exists():
                try:
                    cache_status(tmp_path)
                except CacheClientError as error:
                    assert error.code in {"cache_not_running", "unavailable"}
                    continue
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
    monkeypatch.setattr(service.context, "prewarm_navigation", lambda: (_ for _ in ()).throw(AgentProtocolError("project_required", "Select a project.")))
    service.prewarm()
    assert service.cache_state == "ready"
    monkeypatch.setattr(service.context, "prewarm_navigation", lambda: (_ for _ in ()).throw(AgentProtocolError("invalid_request", "Bad request.")))
    with pytest.raises(AgentProtocolError, match="Bad request"):
        service.prewarm()


def test_multiple_projects_prewarm_repository_navigation_cache(
    tmp_path: Path,
) -> None:
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService

    write_source(
        tmp_path / "A.dpr",
        "program A; uses AUnit in 'AUnit.pas'; begin end.",
    )
    write_source(
        tmp_path / "B.dpr",
        "program B; uses BUnit in 'BUnit.pas'; begin end.",
    )
    write_source(
        tmp_path / "AUnit.pas",
        """
        unit AUnit;
        interface
        type
          TARepositoryCache = class
          end;
        implementation
        end.
        """,
    )
    write_source(
        tmp_path / "BUnit.pas",
        """
        unit BUnit;
        interface
        type
          TBRepositoryCache = class
          end;
        implementation
        end.
        """,
    )
    metadata = CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        512 * 1024**2,
        0,
        10,
        time.time(),
    )
    service = _CacheService(metadata)

    service.prewarm()

    assert service.context.navigation_cache_is_warm
    assert service.cache_state == "warm"
    assert service.status()["current_bytes"] > 0
    assert service.status()["cpg_cache_entries"] == 0
    assert service.status()["cpg_cache_bytes"] == 0
    result = service.request(
        {"action": "find", "query": "RepositoryCache"}
    ).payload["result"]
    assert {item["name"] for item in result} == {
        "TARepositoryCache",
        "TBRepositoryCache",
    }


def test_cache_service_uses_constant_time_accounting_instead_of_deep_graph_walk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from delphi_lsp import agent_cache
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService

    write_source(
        tmp_path / "Demo.dpr",
        """
        program Demo;
        type
          TIndexed = class
          end;
        begin
        end.
        """,
    )
    metadata = CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        512 * 1024**2,
        1,
        10,
        time.time(),
    )
    service = _CacheService(metadata)
    monkeypatch.setattr(
        agent_cache,
        "estimate_deep_size",
        lambda _value: (_ for _ in ()).throw(AssertionError("deep graph walk entered request path")),
    )

    service.prewarm()
    response = service.request({"action": "find", "query": "TIndexed"})

    assert any(item["name"] == "TIndexed" for item in response.payload["result"])
    assert service.last_budget.retained_bytes >= service.context.estimated_cache_bytes


def test_cache_service_accounts_for_unestimated_process_rss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from delphi_lsp import agent_cache
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    monkeypatch.setattr(agent_cache, "current_process_rss_bytes", lambda: 1_000)
    metadata = CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        10_000,
        1,
        10,
        time.time(),
    )
    service = _CacheService(metadata)
    monkeypatch.setattr(
        type(service.context),
        "estimated_cache_bytes",
        property(lambda _context: 100),
    )
    monkeypatch.setattr(agent_cache, "current_process_rss_bytes", lambda: 9_500)

    assert service._measure_retained_bytes() == 8_500
    result = service.budget.enforce(
        measure=service._measure_retained_bytes,
        evict_auxiliary=lambda: None,
        evict_navigation=lambda: None,
    )
    assert result.utilization_percent == 85.0
    assert result.warning_active is True
    assert result.warning_triggered is True


def test_cache_prewarm_builds_registry_without_running_a_find_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from delphi_lsp.agent_cache import CacheMetadata, _CacheService

    write_source(tmp_path / "Demo.dpr", "program Demo; begin end.")
    metadata = CacheMetadata(
        2,
        str(tmp_path.resolve()),
        os.getpid(),
        1,
        "x" * 32,
        "test",
        "",
        512 * 1024**2,
        1,
        10,
        time.time(),
    )
    service = _CacheService(metadata)
    called = 0
    real_prewarm = service.context.prewarm_navigation

    def record_prewarm() -> str:
        nonlocal called
        called += 1
        return real_prewarm()

    monkeypatch.setattr(service.context, "prewarm_navigation", record_prewarm)
    monkeypatch.setattr(
        service.context,
        "handle",
        lambda _request: (_ for _ in ()).throw(AssertionError("find response constructed")),
    )

    service.prewarm()

    assert called == 1
    assert service.context.navigation_cache_is_warm


def test_fresh_incomplete_start_lock_is_not_stale(tmp_path: Path) -> None:
    from delphi_lsp.agent_cache import _start_lock_is_stale

    path = tmp_path / "start.lock"
    path.write_bytes(b"")

    assert _start_lock_is_stale(path) is False
    old = time.time() - 5
    os.utime(path, (old, old))
    assert _start_lock_is_stale(path) is True


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


def test_child_reaping_is_portable_without_posix_waitpid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    monkeypatch.delattr(agent_cache.os, "waitpid", raising=False)
    monkeypatch.delattr(agent_cache.os, "WNOHANG", raising=False)

    assert agent_cache._reap_child_if_exited(424242) is False


def test_stop_waits_for_process_exit_after_owned_metadata_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from delphi_lsp import agent_cache

    metadata = agent_cache.CacheMetadata(
        agent_cache.DAEMON_SCHEMA,
        str(tmp_path.resolve()),
        424242,
        4242,
        "x" * 32,
        "test",
        "",
        1024 * 1024,
        0,
        10,
        time.time(),
    )
    metadata_reads = iter((metadata, None, None))
    pid_probes: list[int] = []
    pid_states = iter((True, True, False))
    monotonic_reads = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(agent_cache, "_read_metadata", lambda _root: next(metadata_reads, None))
    monkeypatch.setattr(
        agent_cache,
        "_pid_alive",
        lambda pid: pid_probes.append(pid) is None and next(pid_states),
    )
    monkeypatch.setattr(agent_cache, "_reap_child_if_exited", lambda _pid: False)
    monkeypatch.setattr(
        agent_cache,
        "_client_exchange",
        lambda _metadata, _request: agent_cache.CacheClientResponse({}),
    )
    monkeypatch.setattr(agent_cache.time, "monotonic", lambda: next(monotonic_reads))
    monkeypatch.setattr(agent_cache.time, "sleep", lambda _seconds: None)

    agent_cache.stop_cache(tmp_path)

    assert pid_probes == [metadata.pid, metadata.pid, metadata.pid]


def test_pid_probe_delegates_to_windows_process_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from delphi_lsp import agent_cache

    monkeypatch.setattr(agent_cache.os, "name", "nt")
    monkeypatch.setattr(agent_cache, "_windows_pid_alive", lambda pid: pid == 424242)

    assert agent_cache._pid_alive(424242) is True
    assert agent_cache._pid_alive(7) is False


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
        assert query_cache(tmp_path, {"action": "open"}).payload["schema"] == 3
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
        assert query_cache(tmp_path, {"action": "find", "query": "Demo"}).payload["schema"] == 3
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
        "delphi-lsp-agent query --root PATH focus --project-id PROJECT_ID",
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
    assert "Protocol v3 JSON" in readme
    assert "writes warnings to stderr" in readme
    assert "A `.dproj` is optional" in readme
    assert "Selecting a project this way also prewarms" in readme
    assert "Eviction is ordered" in readme
    assert "auxiliary caches are evicted first" in readme
    assert "rebuilds the navigation state on demand" in readme
    assert "30-minute idle" in readme
    assert ".delphi-lsp/agent-cache/daemon.json" in readme
    assert "owner-only token" in readme
    assert "Do not copy or share this token" in readme
    assert "--workers auto|N" in readme
    assert "--startup-timeout 120" in readme
    assert "eight worker processes" in readme
    assert "64 MiB" in readme
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
    assert "content-addressed JSON" in readme
    assert "OpenCode cache" in readme
    assert "contains no pickle" in readme
    assert "navigation_disk_hits" in readme
    assert "navigation_disk_misses" in readme

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
            return object.__getattribute__(self, name)

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
