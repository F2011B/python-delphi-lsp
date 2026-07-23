from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import delphi_lsp.parallel_outline as parallel_outline
from delphi_lsp.parallel_outline import (
    OutlineResult,
    OutlineTask,
    ParallelOutlineError,
    parse_worker_setting,
    resolve_worker_count,
    run_outline_tasks,
)


def _write_source(path: Path, unit_name: str) -> None:
    path.write_text(
        f"unit {unit_name};\ninterface\ntype T{unit_name} = class end;\nimplementation\nend.\n",
        encoding="utf-8",
    )


def test_parse_worker_setting_accepts_auto_and_bounded_integers() -> None:
    assert parse_worker_setting("auto") == 0
    assert parse_worker_setting("1") == 1
    assert parse_worker_setting("32") == 32
    for value in ("", "0", "33", "-1", "many"):
        with pytest.raises(ValueError):
            parse_worker_setting(value)


def test_auto_workers_respect_cpu_tasks_and_cache_budget() -> None:
    assert resolve_worker_count(0, task_count=20, cpu_count=8, memory_budget_bytes=512 * 1024**2) == 4
    assert resolve_worker_count(0, task_count=20, cpu_count=8, memory_budget_bytes=128 * 1024**2) == 1
    assert resolve_worker_count(0, task_count=2, cpu_count=8, memory_budget_bytes=None) == 2
    assert resolve_worker_count(7, task_count=3, cpu_count=2, memory_budget_bytes=1) == 3


def test_resolve_worker_count_rejects_programmatic_values_outside_supported_range() -> None:
    for configured_workers in (-1, 33):
        with pytest.raises(ValueError, match="workers"):
            resolve_worker_count(configured_workers, task_count=2)


def test_serial_outline_task_returns_model_text_and_counts(tmp_path: Path) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")

    batch = run_outline_tasks([OutlineTask(0, str(source), (), True)], configured_workers=1)

    assert batch.stats.effective_workers == 1
    assert batch.stats.files_completed == 1
    assert batch.results[0].text.startswith("unit One")
    assert batch.results[0].model is not None
    assert batch.results[0].model.unit_scope.name == "One"
    assert batch.results[0].lines_processed == 5
    assert batch.results[0].symbols_discovered >= 1


def test_outline_symbol_count_matches_semantic_model_name_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    model = SimpleNamespace(index=SimpleNamespace(name_index={"One": [object()]}), unit_scope=object())
    monkeypatch.setattr(parallel_outline, "build_outline_semantic_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(parallel_outline, "iter_symbols", lambda _scope: iter((object(), object(), object())), raising=False)

    batch = run_outline_tasks([OutlineTask(0, str(source), (), False)], configured_workers=1)

    assert batch.results[0].symbols_discovered == sum(len(items) for items in model.index.name_index.values())


def test_missing_source_returns_read_error_with_os_detail(tmp_path: Path) -> None:
    missing = tmp_path / "Missing.pas"

    batch = run_outline_tasks([OutlineTask(0, str(missing), (), False)], configured_workers=1)

    result = batch.results[0]
    assert result.model is None
    assert result.read_error
    assert "No such file" in result.read_error


def test_parser_failure_is_a_parallel_outline_error_with_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")

    def fail_parser(*_: object, **__: object) -> object:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(parallel_outline, "build_outline_semantic_model", fail_parser)

    with pytest.raises(ParallelOutlineError, match=str(source)):
        run_outline_tasks([OutlineTask(0, str(source), (), False)], configured_workers=1)


def test_effective_one_worker_never_constructs_a_process_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")

    def fail_pool(*_: object, **__: object) -> object:
        raise AssertionError("process pool must not be constructed")

    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", fail_pool)

    batch = run_outline_tasks(
        [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
        configured_workers=0,
        cpu_count=8,
        memory_budget_bytes=128 * 1024**2,
    )

    assert batch.stats.effective_workers == 1
    assert batch.stats.files_completed == 2


def test_parallel_spawn_results_are_ordinal_sorted(tmp_path: Path) -> None:
    tasks = []
    for ordinal, name in enumerate(("Three", "One", "Two")):
        path = tmp_path / f"{name}.pas"
        _write_source(path, name)
        tasks.append(OutlineTask(ordinal, str(path), (), False))

    batch = run_outline_tasks(tasks, configured_workers=2)

    assert batch.stats.effective_workers == 2
    assert [result.ordinal for result in batch.results] == [0, 1, 2]
    assert [result.model.unit_scope.name for result in batch.results if result.model] == ["Three", "One", "Two"]


def test_retain_results_false_delivers_callbacks_without_retaining_models(tmp_path: Path) -> None:
    tasks = []
    for ordinal, name in enumerate(("Three", "One", "Two")):
        path = tmp_path / f"{name}.pas"
        _write_source(path, name)
        tasks.append(OutlineTask(ordinal, str(path), (), True))
    received: list[OutlineResult] = []

    batch = run_outline_tasks(
        tasks,
        configured_workers=1,
        retain_results=False,
        on_complete=received.append,
    )

    assert [result.ordinal for result in received] == [0, 1, 2]
    assert all(result.model is not None and result.text for result in received)
    assert batch.results == ()
    assert batch.stats.files_completed == 3


class _FutureResultTypeError:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.cancelled = False

    def result(self) -> OutlineResult:
        raise self.error

    def cancel(self) -> None:
        self.cancelled = True


class _SerializationFailureExecutor:
    futures: list[_FutureResultTypeError] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def submit(self, *_: object) -> _FutureResultTypeError:
        future = _FutureResultTypeError(TypeError("cannot pickle semantic model"))
        self.futures.append(future)
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        del wait, cancel_futures


def test_auto_mode_falls_back_once_after_future_serialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    _SerializationFailureExecutor.futures = []
    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", _SerializationFailureExecutor)
    monkeypatch.setattr(parallel_outline, "as_completed", lambda submitted: list(submitted))

    batch = run_outline_tasks(
        [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
        configured_workers=0,
        cpu_count=3,
    )

    assert batch.stats.effective_workers == 1
    assert batch.stats.fallbacks == 1
    assert batch.stats.files_completed == 2


def test_explicit_mode_wraps_future_serialization_failure_with_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    _SerializationFailureExecutor.futures = []
    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", _SerializationFailureExecutor)
    monkeypatch.setattr(parallel_outline, "as_completed", lambda submitted: list(submitted))

    with pytest.raises(ParallelOutlineError, match=str(source)):
        run_outline_tasks(
            [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
            configured_workers=2,
        )


class _PoolStartupFailure:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("pool unavailable")


def test_auto_mode_falls_back_to_serial_before_any_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", _PoolStartupFailure)

    batch = run_outline_tasks(
        [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
        configured_workers=0,
        cpu_count=3,
    )

    assert batch.stats.effective_workers == 1
    assert batch.stats.fallbacks == 1
    assert batch.results[0].model is not None


def test_explicit_mode_reports_pool_startup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", _PoolStartupFailure)

    with pytest.raises(ParallelOutlineError, match="parallel outline execution failed"):
        run_outline_tasks(
            [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
            configured_workers=2,
        )


def test_callback_failure_cancels_and_propagates(tmp_path: Path) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")

    def fail_callback(_: object) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        run_outline_tasks(
            [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
            configured_workers=2,
            on_complete=fail_callback,
        )


def test_parallel_callback_failure_cancels_pending_futures_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    result = OutlineResult(0, str(source), "", None, 0, 0)

    class FakeFuture:
        def __init__(self, value: OutlineResult) -> None:
            self.value = value
            self.cancelled = False

        def result(self) -> OutlineResult:
            return self.value

        def cancel(self) -> None:
            self.cancelled = True

    futures: list[FakeFuture] = []

    class FakeExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> "FakeExecutor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            shutdown_calls.append((wait, cancel_futures))

        def submit(self, *_: object) -> FakeFuture:
            future = FakeFuture(result)
            futures.append(future)
            return future

    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(parallel_outline, "as_completed", lambda submitted: list(submitted))
    shutdown_calls: list[tuple[bool, bool]] = []

    def fail_callback(_: OutlineResult) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        run_outline_tasks(
            [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
            configured_workers=2,
            on_complete=fail_callback,
    )

    assert len(futures) == 2
    assert futures[0].cancelled is False
    assert futures[1].cancelled is True
    assert shutdown_calls == [(False, True)]


def test_successful_parallel_batch_waits_for_executor_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "One.pas"
    _write_source(source, "One")
    result = OutlineResult(0, str(source), "", None, 0, 0)
    shutdown_calls: list[tuple[bool, bool]] = []

    class FakeFuture:
        def result(self) -> OutlineResult:
            return result

        def cancel(self) -> None:
            raise AssertionError("successful futures must not be cancelled")

    class FakeExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def submit(self, *_: object) -> FakeFuture:
            return FakeFuture()

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            shutdown_calls.append((wait, cancel_futures))

    monkeypatch.setattr(parallel_outline, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(parallel_outline, "as_completed", lambda submitted: list(submitted))

    batch = run_outline_tasks(
        [OutlineTask(0, str(source), (), False), OutlineTask(1, str(source), (), False)],
        configured_workers=2,
    )

    assert batch.stats.files_completed == 2
    assert shutdown_calls == [(True, False)]
