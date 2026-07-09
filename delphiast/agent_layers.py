from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import json

from .lsp_server import build_outline_semantic_model, outline_large_source
from .project_discovery import DelphiProjectDiscovery, discover_delphi_project
from .project_indexer import ProjectIndexResult, ProjectIndexer
from .semantic import Scope, SourceRange, Symbol, SymbolIndex, SymbolKind
from .semantic_builder import SemanticModel
from .source_reader import read_source_text


@dataclass
class CodebaseIndex:
    root: str
    discovery: DelphiProjectDiscovery
    models: dict[str, SemanticModel]
    symbol_index: SymbolIndex
    project_results: dict[str, ProjectIndexResult]


def build_codebase_index(
    root: str | Path,
    *,
    project_file: str | Path | None = None,
    index_projects: bool = False,
) -> CodebaseIndex:
    discovery = discover_delphi_project(root, project_file=project_file)
    models: dict[str, SemanticModel] = {}
    for source in discovery.source_files:
        path = Path(source)
        if path.suffix.casefold() not in {".pas", ".dpr", ".dpk", ".inc"}:
            continue
        try:
            text = read_source_text(path)
        except (OSError, UnicodeError):
            continue
        models[source] = build_outline_semantic_model(outline_large_source(text, 1), source)

    symbol_index = SymbolIndex()
    for model in models.values():
        symbol_index.register_unit(model.unit_scope.name, model.unit_scope)
    for model in models.values():
        model.index = symbol_index

    project_results: dict[str, ProjectIndexResult] = {}
    if index_projects:
        for project in discovery.project_files:
            indexer = ProjectIndexer(
                search_paths=discovery.search_paths,
                include_paths=discovery.include_paths,
                defines=discovery.defines,
            )
            project_results[project] = indexer.index(project)

    return CodebaseIndex(
        root=str(Path(root).expanduser().resolve()),
        discovery=discovery,
        models=models,
        symbol_index=symbol_index,
        project_results=project_results,
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
    return {"layer": "symbols", "root": index.root, "query": query, "items": symbols[:200]}


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

    matches = [symbol for symbol in _all_symbols(index) if symbol.name.casefold() == needle]
    if not matches:
        matches = [
            symbol
            for symbol in _all_symbols(index)
            if symbol.kind.value != "unit" and needle in symbol.name.casefold()
        ]

    items: list[dict[str, Any]] = []
    source_cache: dict[str, list[str]] = {}
    seen_keys: set[tuple[str, str, int, str]] = set()
    for symbol in matches:
        item = _implementation_item(index, symbol, source_cache)
        if item is None:
            continue
        key = (item["name"].casefold(), item["path"], item["line"], item["kind"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(item)
    items.sort(key=lambda item: (item["path"].casefold(), item["line"], item["name"].casefold()))
    return {"layer": "implementation", "root": index.root, "query": query, "items": items[:50]}


def _implementation_item(
    index: CodebaseIndex,
    symbol: Symbol,
    source_cache: dict[str, list[str]],
) -> dict[str, Any] | None:
    fragments: list[dict[str, Any]] = []
    if symbol.kind in {SymbolKind.CLASS, SymbolKind.RECORD, SymbolKind.INTERFACE}:
        declaration = _source_fragment(symbol.decl_range, "declaration", source_cache)
        if declaration is not None:
            fragments.append(declaration)
        fragments.extend(_implementation_fragments_for_type(index, symbol, source_cache))
    elif symbol.kind in _ROUTINE_KINDS:
        fragment = _source_fragment(symbol.decl_range, "implementation", source_cache)
        if fragment is not None:
            fragments.append(fragment)
    else:
        fragment = _source_fragment(symbol.decl_range, "declaration", source_cache)
        if fragment is not None:
            fragments.append(fragment)
        fragments.extend(_implementation_fragments_for_member(index, symbol, source_cache))
    if not fragments:
        return None
    item = _symbol_item(symbol)
    item["fragments"] = fragments
    return item


def _implementation_fragments_for_type(
    index: CodebaseIndex,
    symbol: Symbol,
    source_cache: dict[str, list[str]],
) -> list[dict[str, Any]]:
    prefix = f"{symbol.name.casefold()}."
    fragments: list[dict[str, Any]] = []
    seen_ranges: set[SourceRange] = set()
    for candidate in _all_symbols(index):
        if candidate.kind not in _ROUTINE_KINDS:
            continue
        if candidate.decl_range.file_name != symbol.decl_range.file_name:
            continue
        if not candidate.name.casefold().startswith(prefix):
            continue
        fragment = _source_fragment(candidate.decl_range, "implementation", source_cache)
        if fragment is None or candidate.decl_range in seen_ranges:
            continue
        seen_ranges.add(candidate.decl_range)
        fragment["symbol"] = candidate.name
        fragments.append(fragment)
    fragments.sort(key=lambda item: (item["range"]["start_line"], item["range"]["start_col"]))
    return fragments


def _implementation_fragments_for_member(
    index: CodebaseIndex,
    symbol: Symbol,
    source_cache: dict[str, list[str]],
) -> list[dict[str, Any]]:
    owner = symbol.scope.owner
    if owner is None or owner.kind not in {SymbolKind.CLASS, SymbolKind.RECORD, SymbolKind.INTERFACE}:
        return []
    qualified_name = f"{owner.name.casefold()}.{symbol.name.casefold()}"
    fragments: list[dict[str, Any]] = []
    for candidate in _all_symbols(index):
        if candidate.kind not in _ROUTINE_KINDS:
            continue
        if candidate.decl_range.file_name != symbol.decl_range.file_name:
            continue
        if candidate.name.casefold() != qualified_name:
            continue
        fragment = _source_fragment(candidate.decl_range, "implementation", source_cache)
        if fragment is not None:
            fragment["symbol"] = candidate.name
            fragments.append(fragment)
    return fragments


def _source_fragment(
    source_range: SourceRange,
    fragment_kind: str,
    source_cache: dict[str, list[str]],
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
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CodebaseIndex",
    "build_codebase_index",
    "layer_payload",
    "render_layer",
]
