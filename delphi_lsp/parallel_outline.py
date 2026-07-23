from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
import multiprocessing
import os
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable

from .lsp_server import build_outline_semantic_model
from .semantic_builder import SemanticModel
from .source_reader import read_source_text


_MEBIBYTE = 1024 * 1024
_WORKER_MEMORY_BYTES = 128 * _MEBIBYTE


class ParallelOutlineError(RuntimeError):
    """Raised when a parallel outline operation cannot complete."""


@dataclass(frozen=True)
class OutlineTask:
    ordinal: int
    source_path: str
    defines: tuple[str, ...]
    return_text: bool


@dataclass(frozen=True)
class OutlineResult:
    ordinal: int
    source_path: str
    text: str
    model: SemanticModel | None
    lines_processed: int
    symbols_discovered: int
    read_error: str = ""


@dataclass(frozen=True)
class ParallelBuildStats:
    configured_workers: int
    effective_workers: int
    files_completed: int
    elapsed_seconds: float
    fallbacks: int


@dataclass(frozen=True)
class OutlineBatch:
    results: tuple[OutlineResult, ...]
    stats: ParallelBuildStats


def parse_worker_setting(value: str) -> int:
    if value == "auto":
        return 0
    try:
        workers = int(value)
    except ValueError as error:
        raise ValueError("workers must be 'auto' or an integer from 1 through 32") from error
    if not 1 <= workers <= 32:
        raise ValueError("workers must be 'auto' or an integer from 1 through 32")
    return workers


def resolve_worker_count(
    configured_workers: int,
    *,
    task_count: int,
    cpu_count: int | None = None,
    memory_budget_bytes: int | None = None,
) -> int:
    if not 0 <= configured_workers <= 32:
        raise ValueError("workers must be 'auto' or an integer from 1 through 32")
    if task_count <= 0:
        return 0
    if configured_workers:
        return min(configured_workers, task_count)

    detected_cpus = os.cpu_count() if cpu_count is None else cpu_count
    candidates = [task_count, 4, max(1, (detected_cpus or 1) - 1)]
    if memory_budget_bytes is not None:
        candidates.append(max(1, memory_budget_bytes // _WORKER_MEMORY_BYTES))
    return min(candidates)


def _parse_outline_task(task: OutlineTask) -> OutlineResult:
    try:
        text = read_source_text(Path(task.source_path))
    except (OSError, UnicodeError) as error:
        return OutlineResult(
            ordinal=task.ordinal,
            source_path=task.source_path,
            text="",
            model=None,
            lines_processed=0,
            symbols_discovered=0,
            read_error=str(error),
        )

    try:
        model = build_outline_semantic_model(text, task.source_path, defines=task.defines)
    except Exception as error:
        raise ParallelOutlineError(f"failed to parse {task.source_path}: {error}") from error
    return OutlineResult(
        ordinal=task.ordinal,
        source_path=task.source_path,
        text=text if task.return_text else "",
        model=model,
        lines_processed=len(text.splitlines()),
        symbols_discovered=sum(len(items) for items in model.index.name_index.values()),
    )


def run_outline_tasks(
    tasks: Iterable[OutlineTask],
    *,
    configured_workers: int,
    memory_budget_bytes: int | None = None,
    cpu_count: int | None = None,
    on_complete: Callable[[OutlineResult], None] | None = None,
) -> OutlineBatch:
    task_list = tuple(tasks)
    started = perf_counter()
    effective_workers = resolve_worker_count(
        configured_workers,
        task_count=len(task_list),
        cpu_count=cpu_count,
        memory_budget_bytes=memory_budget_bytes,
    )
    if effective_workers <= 1:
        results = _run_serial(task_list, on_complete)
        return _batch(configured_workers, effective_workers, results, started, fallbacks=0)

    accepted: list[OutlineResult] = []
    futures: dict[object, OutlineTask] = {}
    executor: ProcessPoolExecutor | None = None
    try:
        context = multiprocessing.get_context("spawn")
        executor = ProcessPoolExecutor(max_workers=effective_workers, mp_context=context)
        futures = {executor.submit(_parse_outline_task, task): task for task in task_list}
        for future in as_completed(futures):
            result = future.result()
            accepted.append(result)
            if on_complete is not None:
                on_complete(result)
    except (OSError, BrokenProcessPool) as error:
        _cancel_futures(futures)
        _shutdown_failed_executor(executor)
        if configured_workers == 0 and not accepted:
            results = _run_serial(task_list, on_complete)
            return _batch(configured_workers, 1, results, started, fallbacks=1)
        raise ParallelOutlineError(f"parallel outline execution failed: {error}") from error
    except BaseException:
        _cancel_futures(futures)
        _shutdown_failed_executor(executor)
        raise

    assert executor is not None
    executor.shutdown(wait=True, cancel_futures=False)

    return _batch(configured_workers, effective_workers, accepted, started, fallbacks=0)


def _run_serial(
    tasks: tuple[OutlineTask, ...], on_complete: Callable[[OutlineResult], None] | None
) -> list[OutlineResult]:
    results = []
    for task in tasks:
        result = _parse_outline_task(task)
        results.append(result)
        if on_complete is not None:
            on_complete(result)
    return results


def _cancel_futures(futures: dict[object, OutlineTask]) -> None:
    for future in futures:
        future.cancel()


def _shutdown_failed_executor(executor: ProcessPoolExecutor | None) -> None:
    if executor is None:
        return
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except BaseException:
        pass


def _batch(
    configured_workers: int,
    effective_workers: int,
    results: list[OutlineResult],
    started: float,
    *,
    fallbacks: int,
) -> OutlineBatch:
    ordered_results = tuple(sorted(results, key=lambda result: result.ordinal))
    return OutlineBatch(
        results=ordered_results,
        stats=ParallelBuildStats(
            configured_workers=configured_workers,
            effective_workers=effective_workers,
            files_completed=len(ordered_results),
            elapsed_seconds=perf_counter() - started,
            fallbacks=fallbacks,
        ),
    )
