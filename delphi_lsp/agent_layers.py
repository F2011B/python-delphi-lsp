from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
import json

from .agent_metrics import build_path_metrics
from .parallel_outline import OutlineResult, OutlineTask, ParallelBuildStats, run_outline_tasks
from .project_discovery import (
    DelphiProjectDiscovery,
    discover_delphi_project,
    populate_workspace_sources,
)
from .project_indexer import ProjectIndexResult, ProjectIndexer
from .progress import ProgressCallback, ProgressEvent
from .semantic import Scope, SourceRange, Symbol, SymbolIndex, SymbolKind
from .semantic_builder import SemanticModel
from .source_reader import read_source_text


_MAX_IMPLEMENTATION_ITEMS = 50
_MAX_IMPLEMENTATION_SOURCE_FILES = 4


@dataclass
class CodebaseIndex:
    root: str
    discovery: DelphiProjectDiscovery
    models: dict[str, SemanticModel]
    symbol_index: SymbolIndex
    project_results: dict[str, ProjectIndexResult]
    parallel_stats: ParallelBuildStats = ParallelBuildStats(0, 0, 0, 0.0, 0)


def build_codebase_index(
    root: str | Path,
    *,
    project_file: str | Path | None = None,
    index_projects: bool = False,
    main_projects_only: bool = False,
    retain_project_syntax: bool = True,
    project_source_roots: Iterable[str | Path] = (),
    on_progress: ProgressCallback | None = None,
    workers: int = 0,
) -> CodebaseIndex:
    progress = _MonotonicProgress(on_progress)
    root_path = Path(root).expanduser().resolve()
    discovery = discover_delphi_project(
        root_path,
        project_file=project_file,
        main_projects_only=main_projects_only,
        scan_workspace_sources=not (main_projects_only and index_projects),
        on_progress=progress,
    )
    project_results: dict[str, ProjectIndexResult] = {}
    if index_projects and main_projects_only:
        project_results = _build_project_results(
            discovery,
            retain_project_syntax=retain_project_syntax,
            source_roots=tuple(project_source_roots),
            progress=progress,
            on_progress=on_progress,
        )
        _set_project_source_inventory(discovery, project_results)
        if not discovery.project_files:
            populate_workspace_sources(discovery, on_progress=progress)

    models: dict[str, SemanticModel] = {}
    lines_processed = 0
    symbols_discovered = 0
    completed_outlines = 0
    outline_tasks = tuple(
        OutlineTask(ordinal, source, tuple(discovery.defines), False)
        for ordinal, source in enumerate(discovery.source_files)
        if Path(source).suffix.casefold() in {".pas", ".dpr", ".dpk", ".inc"}
    )
    outline_task_count = len(outline_tasks)

    def on_outline_complete(result: OutlineResult) -> None:
        nonlocal completed_outlines, lines_processed, symbols_discovered
        completed_outlines += 1
        if not result.read_error:
            lines_processed += result.lines_processed
            symbols_discovered += result.symbols_discovered
        _emit_progress(
            progress,
            "outline",
            result.source_path,
            len(discovery.source_files),
            completed_outlines,
            outline_task_count,
            "source unreadable" if result.read_error else "source outlined",
            lines_processed=lines_processed,
            symbols_discovered=symbols_discovered,
        )

    outline_batch = run_outline_tasks(
        outline_tasks,
        configured_workers=workers,
        on_complete=on_outline_complete,
    )
    for result in outline_batch.results:
        if result.read_error or result.model is None:
            continue
        models[result.source_path] = result.model

    symbol_index = SymbolIndex()
    for model in models.values():
        symbol_index.register_unit(model.unit_scope.name, model.unit_scope)
    for model in models.values():
        model.index = symbol_index

    _emit_progress(
        progress,
        "relations",
        str(root_path),
        len(discovery.source_files),
        len(discovery.source_files),
        len(discovery.source_files),
        "semantic relations indexed",
        lines_processed=lines_processed,
        symbols_discovered=sum(len(items) for items in symbol_index.name_index.values()),
    )

    if index_projects and not main_projects_only:
        project_results = _build_project_results(
            discovery,
            retain_project_syntax=retain_project_syntax,
            source_roots=tuple(project_source_roots),
            progress=progress,
            on_progress=on_progress,
        )

    index = CodebaseIndex(
        root=str(root_path),
        discovery=discovery,
        models=models,
        symbol_index=symbol_index,
        project_results=project_results,
        parallel_stats=outline_batch.stats,
    )
    _emit_progress(
        progress,
        "complete",
        str(root_path),
        len(discovery.source_files),
        len(discovery.source_files),
        len(discovery.source_files),
        "codebase index complete",
        lines_processed=lines_processed,
        symbols_discovered=sum(len(items) for items in symbol_index.name_index.values()),
    )
    return index


def _build_project_results(
    discovery: DelphiProjectDiscovery,
    *,
    retain_project_syntax: bool,
    source_roots: tuple[str | Path, ...],
    progress: ProgressCallback,
    on_progress: ProgressCallback | None,
) -> dict[str, ProjectIndexResult]:
    project_results: dict[str, ProjectIndexResult] = {}
    project_total = len(discovery.project_files)
    for ordinal, project in enumerate(discovery.project_files, start=1):
        indexer = ProjectIndexer(
            search_paths=discovery.search_paths,
            include_paths=discovery.include_paths,
            defines=discovery.defines,
            on_progress=progress,
            source_roots=source_roots,
            project_config=discovery.project_config,
        )
        raw_result = indexer.index(project)
        result = raw_result
        if not retain_project_syntax:
            result = replace(
                raw_result,
                parsed_units=[
                    replace(unit, syntax_tree=None)
                    for unit in raw_result.parsed_units
                ],
            )
        project_results[project] = result
        _emit_progress(
            on_progress,
            "projects",
            project,
            project_total,
            ordinal,
            project_total,
            "main project indexed",
        )
        del raw_result
        del indexer
    return project_results


def _set_project_source_inventory(
    discovery: DelphiProjectDiscovery,
    project_results: dict[str, ProjectIndexResult],
) -> None:
    sources: dict[str, str] = {}
    for result in project_results.values():
        for unit in result.parsed_units:
            resolved = str(Path(unit.path).expanduser().resolve())
            sources.setdefault(resolved.casefold(), resolved)
        for include in result.include_files:
            resolved = str(Path(include.path).expanduser().resolve())
            sources.setdefault(resolved.casefold(), resolved)
    discovery.source_files = sorted(sources.values(), key=str.casefold)
    discovery.unit_paths = {}
    for source in discovery.source_files:
        key = Path(source).stem.casefold()
        discovery.unit_paths.setdefault(key, []).append(source)


def _emit_progress(
    callback: ProgressCallback | None,
    phase: str,
    path: str,
    files_discovered: int,
    files_completed: int,
    files_total: int | None,
    detail: str,
    *,
    lines_processed: int = 0,
    symbols_discovered: int = 0,
) -> None:
    if callback is not None:
        callback(
            ProgressEvent(
                phase,
                "delphi",
                path,
                files_discovered,
                files_completed,
                files_total,
                lines_processed,
                symbols_discovered,
                0,
                detail,
            )
        )


class _MonotonicProgress:
    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._files_discovered = 0
        self._files_completed = 0
        self._files_total: int | None = None
        self._lines_processed = 0
        self._symbols_discovered = 0
        self._cached_files = 0

    def __call__(self, event: ProgressEvent) -> None:
        self._files_discovered = max(self._files_discovered, event.files_discovered)
        self._files_completed = max(self._files_completed, event.files_completed)
        if event.files_total is not None:
            self._files_total = max(self._files_total or 0, event.files_total)
        self._lines_processed = max(self._lines_processed, event.lines_processed)
        self._symbols_discovered = max(self._symbols_discovered, event.symbols_discovered)
        self._cached_files = max(self._cached_files, event.cached_files)
        if self._callback is not None:
            self._callback(
                replace(
                    event,
                    files_discovered=self._files_discovered,
                    files_completed=self._files_completed,
                    files_total=self._files_total,
                    lines_processed=self._lines_processed,
                    symbols_discovered=self._symbols_discovered,
                    cached_files=self._cached_files,
                )
            )


def render_layer(
    index: CodebaseIndex,
    layer: str,
    *,
    query: str = "",
    output_format: str = "markdown",
) -> str:
    payload = layer_payload(index, layer, query=query)
    if output_format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return _render_markdown(payload)


def layer_payload(index: CodebaseIndex, layer: str, *, query: str = "") -> dict[str, Any]:
    normalized_layer = layer.casefold()
    if normalized_layer == "overview":
        return _overview_payload(index)
    if normalized_layer == "projects":
        return _projects_payload(index)
    if normalized_layer == "units":
        return _units_payload(index, query=query)
    if normalized_layer == "unit":
        return _unit_payload(index, query=query)
    if normalized_layer == "symbols":
        return _symbols_payload(index, query=query)
    if normalized_layer == "symbol":
        return _symbol_payload(index, query=query)
    if normalized_layer == "implementation":
        return _implementation_payload(index, query=query)
    if normalized_layer == "references":
        return _references_payload(index, query=query)
    if normalized_layer == "problems":
        return _problems_payload(index)
    if normalized_layer == "metrics":
        return _metrics_payload(index, query=query)
    raise ValueError(f"Unknown layer: {layer}")


def _overview_payload(index: CodebaseIndex) -> dict[str, Any]:
    return {
        "layer": "overview",
        "root": index.root,
        "project_count": len(index.discovery.project_files),
        "source_count": len(index.discovery.source_files),
        "unit_count": len(index.models),
        "search_paths": index.discovery.search_paths,
        "include_paths": index.discovery.include_paths,
        "defines": index.discovery.defines,
        "problems": [_problem_item(problem) for problem in index.discovery.problems],
        "projects": index.discovery.project_files,
    }


def _projects_payload(index: CodebaseIndex) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if index.project_results:
        for project, result in index.project_results.items():
            items.append(
                {
                    "path": project,
                    "parsed_units": [
                        {"name": unit.name, "path": unit.path, "has_error": unit.has_error}
                        for unit in result.parsed_units
                    ],
                    "include_files": [{"name": item.name, "path": item.path} for item in result.include_files],
                    "not_found_units": result.not_found_units,
                    "problems": [
                        {"kind": problem.problem_type.value, "file": problem.file_name, "message": problem.description}
                        for problem in result.problems
                    ],
                }
            )
    else:
        for project in index.discovery.project_files:
            items.append(
                {
                    "path": project,
                    "parsed_units": [],
                    "include_files": [],
                    "not_found_units": [],
                    "problems": [],
                    "deep_indexed": False,
                }
            )
    return {"layer": "projects", "root": index.root, "items": items}


def _units_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    items = []
    for file_name, model in sorted(index.models.items(), key=lambda item: item[0].casefold()):
        if needle and needle not in model.unit_scope.name.casefold() and needle not in file_name.casefold():
            continue
        items.append(
            {
                "name": model.unit_scope.name,
                "path": file_name,
                "symbol_count": sum(1 for _ in _iter_symbols(model.unit_scope, include_unit=False)),
            }
        )
    return {"layer": "units", "root": index.root, "items": items}


def _unit_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    items = []
    for file_name, model in sorted(index.models.items(), key=lambda item: item[0].casefold()):
        if needle and needle not in model.unit_scope.name.casefold() and needle not in file_name.casefold():
            continue
        items.append(
            {
                "name": model.unit_scope.name,
                "path": file_name,
                "symbols": [_symbol_item(symbol) for symbol in _iter_symbols(model.unit_scope, include_unit=False)],
            }
        )
    return {"layer": "unit", "root": index.root, "items": items}


def _symbols_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    symbols = []
    for symbol in _all_symbols(index):
        if symbol.kind.value == "unit":
            continue
        if needle and needle not in symbol.name.casefold():
            continue
        symbols.append(_symbol_item(symbol))
    symbols.sort(key=lambda item: (item["name"].casefold(), item["path"].casefold(), item["line"]))
    return {"layer": "symbols", "root": index.root, "query": query, "items": symbols}


def _symbol_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    matches = []
    for symbol in _all_symbols(index):
        if needle and needle not in symbol.name.casefold():
            continue
        item = _symbol_item(symbol)
        if symbol.member_scope is not None:
            item["children"] = [_symbol_item(child) for child in _iter_symbols(symbol.member_scope, include_unit=False)]
        matches.append(item)
    return {"layer": "symbol", "root": index.root, "query": query, "items": matches[:50]}


def _implementation_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    if not needle:
        return {
            "layer": "implementation",
            "root": index.root,
            "query": query,
            "items": [],
            "message": "Pass a class, routine, or member name in query to read focused source.",
        }

    all_symbols = tuple(_all_symbols(index))
    matches = [symbol for symbol in all_symbols if symbol.name.casefold() == needle]
    if not matches:
        matches = [
            symbol
            for symbol in all_symbols
            if symbol.kind.value != "unit" and needle in symbol.name.casefold()
        ]
    unique_matches: dict[tuple[str, str, int, int, str], Symbol] = {}
    for symbol in matches:
        unique_matches.setdefault(
            (
                symbol.decl_range.file_name,
                symbol.name.casefold(),
                symbol.decl_range.start_line,
                symbol.decl_range.start_col,
                symbol.kind.value,
            ),
            symbol,
        )
    matches = sorted(
        unique_matches.values(),
        key=lambda symbol: (
            symbol.decl_range.file_name.casefold(),
            symbol.decl_range.start_line,
            symbol.decl_range.start_col,
            symbol.name.casefold(),
            symbol.kind.value,
        ),
    )[:_MAX_IMPLEMENTATION_ITEMS]

    items: list[dict[str, Any]] = []
    source_cache = _BoundedSourceCache(_MAX_IMPLEMENTATION_SOURCE_FILES)
    routine_lookup = _build_routine_lookup(all_symbols)
    seen_keys: set[tuple[str, str, int, str]] = set()
    for symbol in matches:
        item = _implementation_item(symbol, source_cache, routine_lookup)
        if item is None:
            continue
        key = (item["name"].casefold(), item["path"], item["line"], item["kind"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(item)
    items.sort(key=lambda item: (item["path"].casefold(), item["line"], item["name"].casefold()))
    return {"layer": "implementation", "root": index.root, "query": query, "items": items}


class _BoundedSourceCache(OrderedDict[str, list[str]]):
    def __init__(self, max_entries: int) -> None:
        super().__init__()
        self.max_entries = max_entries

    def get(self, key: str, default: list[str] | None = None) -> list[str] | None:
        value = super().get(key, default)
        if key in self:
            self.move_to_end(key)
        return value

    def __setitem__(self, key: str, value: list[str]) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.max_entries:
            self.popitem(last=False)


@dataclass(frozen=True)
class _RoutineLookup:
    by_name: dict[tuple[str, str], tuple[Symbol, ...]]
    by_owner: dict[tuple[str, str], tuple[Symbol, ...]]


def _build_routine_lookup(symbols: Iterable[Symbol]) -> _RoutineLookup:
    by_name_lists: dict[tuple[str, str], list[Symbol]] = {}
    by_owner_lists: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in symbols:
        if symbol.kind not in _ROUTINE_KINDS:
            continue
        normalized_name = symbol.name.casefold()
        path = symbol.decl_range.file_name
        by_name_lists.setdefault((path, normalized_name), []).append(symbol)
        owner, separator, _member = normalized_name.rpartition(".")
        if separator:
            by_owner_lists.setdefault((path, owner), []).append(symbol)
    return _RoutineLookup(
        by_name={key: tuple(value) for key, value in by_name_lists.items()},
        by_owner={key: tuple(value) for key, value in by_owner_lists.items()},
    )


def _implementation_item(
    symbol: Symbol,
    source_cache: MutableMapping[str, list[str]],
    routine_lookup: _RoutineLookup,
) -> dict[str, Any] | None:
    fragments: list[dict[str, Any]] = []
    if symbol.kind in {SymbolKind.CLASS, SymbolKind.RECORD, SymbolKind.INTERFACE}:
        declaration = _source_fragment(symbol.decl_range, "declaration", source_cache)
        if declaration is not None:
            fragments.append(declaration)
        fragments.extend(
            _implementation_fragments_for_type(
                symbol, source_cache, routine_lookup
            )
        )
    elif symbol.kind in _ROUTINE_KINDS:
        fragment = _source_fragment(symbol.decl_range, "implementation", source_cache)
        if fragment is not None:
            fragments.append(fragment)
    else:
        fragment = _source_fragment(symbol.decl_range, "declaration", source_cache)
        if fragment is not None:
            fragments.append(fragment)
        fragments.extend(
            _implementation_fragments_for_member(
                symbol, source_cache, routine_lookup
            )
        )
    if not fragments:
        return None
    item = _symbol_item(symbol)
    item["fragments"] = fragments
    return item


def _implementation_fragments_for_type(
    symbol: Symbol,
    source_cache: MutableMapping[str, list[str]],
    routine_lookup: _RoutineLookup,
) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    seen_ranges: set[SourceRange] = set()
    candidates = routine_lookup.by_owner.get(
        (symbol.decl_range.file_name, symbol.name.casefold()), ()
    )
    for candidate in candidates:
        fragment = _source_fragment(candidate.decl_range, "implementation", source_cache)
        if fragment is None or candidate.decl_range in seen_ranges:
            continue
        seen_ranges.add(candidate.decl_range)
        fragment["symbol"] = candidate.name
        fragments.append(fragment)
    fragments.sort(key=lambda item: (item["range"]["start_line"], item["range"]["start_col"]))
    return fragments


def _implementation_fragments_for_member(
    symbol: Symbol,
    source_cache: MutableMapping[str, list[str]],
    routine_lookup: _RoutineLookup,
) -> list[dict[str, Any]]:
    owner = symbol.scope.owner
    if owner is None or owner.kind not in {SymbolKind.CLASS, SymbolKind.RECORD, SymbolKind.INTERFACE}:
        return []
    qualified_name = f"{owner.name.casefold()}.{symbol.name.casefold()}"
    fragments: list[dict[str, Any]] = []
    candidates = routine_lookup.by_name.get(
        (symbol.decl_range.file_name, qualified_name), ()
    )
    for candidate in candidates:
        fragment = _source_fragment(candidate.decl_range, "implementation", source_cache)
        if fragment is not None:
            fragment["symbol"] = candidate.name
            fragments.append(fragment)
    return fragments


def _source_fragment(
    source_range: SourceRange,
    fragment_kind: str,
    source_cache: MutableMapping[str, list[str]],
) -> dict[str, Any] | None:
    lines = source_cache.get(source_range.file_name)
    if lines is None:
        try:
            text = read_source_text(Path(source_range.file_name))
        except (OSError, UnicodeError):
            return None
        lines = text.splitlines(keepends=True)
        source_cache[source_range.file_name] = lines
    if source_range.start_line < 1 or source_range.start_line > len(lines):
        return None
    end_line = min(max(source_range.end_line, source_range.start_line), len(lines))
    snippet = "".join(lines[source_range.start_line - 1 : end_line]).rstrip("\r\n")
    return {
        "fragment_kind": fragment_kind,
        "range": _range_item(source_range),
        "line_count": end_line - source_range.start_line + 1,
        "text": snippet,
    }


def _references_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    needle = query.casefold().strip()
    items = []
    for symbol in _all_symbols(index):
        if needle and needle not in symbol.name.casefold():
            continue
        items.append(_symbol_item(symbol))
    return {"layer": "references", "root": index.root, "query": query, "items": items[:100]}


def _problems_payload(index: CodebaseIndex) -> dict[str, Any]:
    items = [_problem_item(problem) for problem in index.discovery.problems]
    for project, result in index.project_results.items():
        for problem in result.problems:
            items.append(
                {
                    "kind": problem.problem_type.value,
                    "origin": project,
                    "message": problem.description,
                    "file": problem.file_name,
                }
            )
    return {"layer": "problems", "root": index.root, "items": items}


def _metrics_payload(index: CodebaseIndex, *, query: str) -> dict[str, Any]:
    project_name = "Workspace"
    if len(index.discovery.project_files) == 1:
        project_name = Path(index.discovery.project_files[0]).stem
    metrics = build_path_metrics(
        index.root,
        index.discovery.source_files,
        defines=index.discovery.defines,
        include_paths=index.discovery.include_paths,
        project_config=index.discovery.project_config,
        project_name=project_name,
    )
    needle = query.casefold().strip()
    units = [
        unit.to_mapping(detail=True)
        for unit in metrics.units
        if not needle or needle in unit.name.casefold() or needle in unit.path.casefold()
    ]
    return {
        "layer": "metrics",
        "root": index.root,
        "project": metrics.to_mapping(),
        "items": units,
    }


def _all_symbols(index: CodebaseIndex) -> Iterable[Symbol]:
    for model in index.models.values():
        yield from _iter_symbols(model.unit_scope)


def _iter_symbols(scope: Scope, *, include_unit: bool = True, seen: set[int] | None = None) -> Iterable[Symbol]:
    if seen is None:
        seen = set()
    scope_id = id(scope)
    if scope_id in seen:
        return
    seen.add(scope_id)
    for symbols in scope.symbols.values():
        for symbol in symbols:
            if include_unit or symbol.kind.value != "unit":
                yield symbol
            if symbol.member_scope is not None:
                yield from _iter_symbols(symbol.member_scope, include_unit=include_unit, seen=seen)


def _symbol_item(symbol: Symbol) -> dict[str, Any]:
    return {
        "name": symbol.name,
        "kind": symbol.kind.value,
        "path": symbol.decl_range.file_name,
        "line": symbol.decl_range.start_line,
        "column": symbol.decl_range.start_col,
        "range": _range_item(symbol.decl_range),
        "visibility": symbol.visibility.value,
        "type": symbol.type_ref.display_name(),
        "owner": symbol.scope.owner.name if symbol.scope.owner is not None else symbol.scope.name,
    }


_ROUTINE_KINDS = {
    SymbolKind.PROCEDURE,
    SymbolKind.FUNCTION,
    SymbolKind.CONSTRUCTOR,
    SymbolKind.DESTRUCTOR,
}


def _range_item(source_range: SourceRange) -> dict[str, int | str]:
    return {
        "path": source_range.file_name,
        "start_line": source_range.start_line,
        "start_col": source_range.start_col,
        "end_line": source_range.end_line,
        "end_col": source_range.end_col,
    }


def _problem_item(problem: Any) -> dict[str, str]:
    return {
        "kind": problem.kind,
        "origin": problem.origin,
        "message": problem.message,
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# Delphi Codebase Layer: {payload['layer']}", ""]
    if payload["layer"] == "overview":
        lines.extend(
            [
                f"- Root: `{payload['root']}`",
                f"- Projects: {payload['project_count']}",
                f"- Sources: {payload['source_count']}",
                f"- Units: {payload['unit_count']}",
                f"- Defines: {', '.join(payload['defines']) or '(none)'}",
                "",
                "## Search Paths",
            ]
        )
        lines.extend(f"- `{path}`" for path in payload["search_paths"])
        lines.append("")
        lines.append("## Include Paths")
        lines.extend(f"- `{path}`" for path in payload["include_paths"])
    elif payload["layer"] in {"units", "symbols", "references"}:
        for item in payload["items"]:
            lines.append(f"- `{item['name']}` {item.get('kind', 'unit')} at `{item['path']}:{item.get('line', 1)}`")
    elif payload["layer"] in {"unit", "symbol"}:
        for item in payload["items"]:
            lines.append(f"## {item['name']}")
            lines.append(f"- Path: `{item['path']}`")
            for symbol in item.get("symbols", item.get("children", [])):
                lines.append(
                    f"- `{symbol['name']}` {symbol['kind']} at `{symbol['path']}:{symbol['line']}`"
                )
    elif payload["layer"] == "implementation":
        if payload.get("message"):
            lines.append(payload["message"])
        for item in payload["items"]:
            lines.append(f"## {item['name']}")
            lines.append(f"- Kind: `{item['kind']}`")
            lines.append(f"- Path: `{item['path']}`")
            for fragment in item["fragments"]:
                source_range = fragment["range"]
                lines.append("")
                lines.append(
                    f"### {fragment['fragment_kind']} `{source_range['path']}:{source_range['start_line']}`"
                )
                if fragment.get("symbol"):
                    lines.append(f"- Symbol: `{fragment['symbol']}`")
                lines.append(f"- Lines: {fragment['line_count']}")
                lines.append("")
                lines.append("```pascal")
                lines.append(fragment["text"])
                lines.append("```")
    elif payload["layer"] == "projects":
        for item in payload["items"]:
            lines.append(f"## `{item['path']}`")
            for unit in item["parsed_units"]:
                lines.append(f"- Unit `{unit['name']}` at `{unit['path']}`")
    elif payload["layer"] == "problems":
        for item in payload["items"]:
            lines.append(f"- {item['kind']}: {item['message']} (`{item.get('origin', '')}`)")
    elif payload["layer"] == "metrics":
        project = payload["project"]
        lines.extend(
            [
                f"- Project LOC: {project['total_loc']}",
                f"- Project LOC with includes: {project['total_loc_with_includes']}",
                f"- Units: {project['unit_count']}",
                f"- Maintainability index: {project['maintainability_index']:.2f}",
            ]
        )
        for item in payload["items"]:
            lines.extend(
                [
                    "",
                    f"## {item['name']}",
                    f"- Path: `{item['path']}`",
                    f"- LOC: {item['lines']['total_lines']}",
                    f"- Cyclomatic maximum: {item['cyclomatic']['maximum']}",
                    f"- Maintainability index: {item['maintainability_index']:.2f}",
                    f"- Instability: {item['instability']:.3f}",
                    f"- Abstractness: {item['abstractness']:.3f}",
                    f"- Distance: {item['distance']:.3f}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CodebaseIndex",
    "build_codebase_index",
    "layer_payload",
    "render_layer",
]
