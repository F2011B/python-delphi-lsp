from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path

from .agent_workspace import (
    AgentWorkspace,
    unit_display_path,
    unit_source_path,
    unit_target_id,
)
from .metrics import (
    MetricProblem,
    ProjectMetrics,
    UnitMetrics,
    aggregate_project_metrics,
    analyze_unit,
)
from .parallel_outline import run_outline_tasks
from .parser_backend import ParserMode
from .source_reader import read_source_text


_LARGE_PROJECT_UNIT_THRESHOLD = 256
_LARGE_PROJECT_BYTE_THRESHOLD = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _MetricTask:
    ordinal: int
    source_path: str
    display_path: str
    unit_id: str
    defines: tuple[str, ...]
    include_paths: tuple[str, ...]
    parser_mode: ParserMode


@dataclass(frozen=True, slots=True)
class _MetricResult:
    ordinal: int
    source_path: str
    metrics: UnitMetrics | None
    problem: MetricProblem | None


def _analyze_metric_task(task: _MetricTask) -> _MetricResult:
    try:
        source = read_source_text(Path(task.source_path))
    except (OSError, UnicodeError):
        return _MetricResult(
            task.ordinal,
            task.source_path,
            None,
            MetricProblem(
                kind="source_unavailable",
                path=task.display_path,
                message="Could not read source.",
            ),
        )
    metrics = analyze_unit(
        source,
        task.display_path,
        defines=task.defines,
        include_paths=task.include_paths,
        parser_mode=task.parser_mode,
    )
    return _MetricResult(
        task.ordinal,
        task.source_path,
        replace(metrics, unit_id=task.unit_id),
        None,
    )


def build_workspace_metrics(
    workspace: AgentWorkspace,
    *,
    workers: int = 0,
    worker_memory_budget_bytes: int | None = None,
) -> ProjectMetrics:
    units = tuple(workspace.units)
    source_bytes = 0
    for unit in units:
        try:
            source_bytes += unit_source_path(workspace.root, unit).stat().st_size
        except OSError:
            continue
    parser_mode = (
        ParserMode.TOLERANT
        if len(units) >= _LARGE_PROJECT_UNIT_THRESHOLD
        or source_bytes >= _LARGE_PROJECT_BYTE_THRESHOLD
        else ParserMode.STRICT
    )
    analyzed_units: list[UnitMetrics] = []
    problems: list[MetricProblem] = []

    def consume_result(result: _MetricResult) -> None:
        if result.metrics is not None:
            analyzed_units.append(result.metrics)
        if result.problem is not None:
            problems.append(result.problem)

    run_outline_tasks(
        (
            _MetricTask(
                ordinal,
                str(unit_source_path(workspace.root, unit)),
                unit_display_path(workspace.root, unit),
                unit_target_id(workspace.root, unit),
                workspace.defines,
                workspace.include_paths,
                parser_mode,
            )
            for ordinal, unit in enumerate(units)
        ),
        configured_workers=workers,
        memory_budget_bytes=worker_memory_budget_bytes,
        on_complete=consume_result,
        retain_results=False,
        task_runner=_analyze_metric_task,
    )

    include_loc = 0
    seen_includes: set[str] = set()
    for include_file in workspace.include_files:
        display_path = include_file["path"]
        normalized_path = str(display_path).replace("\\", "/").casefold()
        if normalized_path in seen_includes:
            continue
        seen_includes.add(normalized_path)
        source_path = _workspace_path(workspace.root, display_path)
        try:
            include_source = read_source_text(source_path)
            include_loc += len(include_source.splitlines()) if include_source else 0
        except (OSError, UnicodeError):
            problems.append(
                MetricProblem(
                    kind="include_unavailable",
                    path=display_path,
                    message="Could not read include file.",
                )
            )

    project = workspace.active_project
    metrics = aggregate_project_metrics(
        analyzed_units,
        include_loc=include_loc,
        project_id=workspace.active_project_id,
        project_name=project.name if project is not None else "",
    )
    if problems:
        metrics = replace(
            metrics,
            problems=(
                *metrics.problems,
                *sorted(problems, key=lambda item: (item.path.casefold(), item.kind)),
            ),
        )
    return metrics


def project_metric_item(metrics: ProjectMetrics) -> dict[str, object]:
    return {"item_type": "project_metrics", **metrics.to_mapping()}


def unit_metric_item(metrics: UnitMetrics, *, detail: bool = False) -> dict[str, object]:
    return {"item_type": "unit_metrics", **metrics.to_mapping(detail=detail)}


def _workspace_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


__all__ = ["build_workspace_metrics", "project_metric_item", "unit_metric_item"]
