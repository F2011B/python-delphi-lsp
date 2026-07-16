from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .agent_workspace import (
    AgentWorkspace,
    unit_display_path,
    unit_source_path,
    unit_target_id,
)
from .metrics import MetricProblem, ProjectMetrics, UnitMetrics, analyze_project
from .source_reader import read_source_text


def build_workspace_metrics(workspace: AgentWorkspace) -> ProjectMetrics:
    sources: dict[str, str] = {}
    include_sources: dict[str, str] = {}
    unit_ids: dict[str, str] = {}
    problems: list[MetricProblem] = []

    for unit in workspace.units:
        display_path = unit_display_path(workspace.root, unit)
        source_path = unit_source_path(workspace.root, unit)
        try:
            sources[display_path] = read_source_text(source_path)
        except OSError:
            problems.append(
                MetricProblem(
                    kind="source_unavailable",
                    path=display_path,
                    message="Could not read source.",
                )
            )
            continue
        unit_ids[display_path] = unit_target_id(workspace.root, unit)

    for include_file in workspace.include_files:
        display_path = include_file["path"]
        source_path = _workspace_path(workspace.root, display_path)
        try:
            include_sources[display_path] = read_source_text(source_path)
        except OSError:
            problems.append(
                MetricProblem(
                    kind="include_unavailable",
                    path=display_path,
                    message="Could not read include file.",
                )
            )

    project = workspace.active_project
    metrics = analyze_project(
        sources,
        include_sources=include_sources,
        defines=workspace.defines,
        include_paths=workspace.include_paths,
        project_id=workspace.active_project_id,
        project_name=project.name if project is not None else "",
        unit_ids=unit_ids,
    )
    if problems:
        metrics = replace(metrics, problems=(*metrics.problems, *problems))
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
