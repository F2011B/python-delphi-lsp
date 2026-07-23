from __future__ import annotations

from pathlib import Path

import pytest

import delphi_lsp.parallel_outline as parallel_outline
from delphi_lsp.parallel_outline import (
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


class _PoolStartupFailure:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def __enter__(self) -> object:
        raise OSError("pool unavailable")

    def __exit__(self, *args: object) -> None:
        return None


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
