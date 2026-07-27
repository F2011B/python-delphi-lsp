from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import unicodedata
from typing import Any, Iterable

from ._version import __version__
from .agent_layers import (
    CodebaseIndex,
    _all_symbols,
    _source_fragment,
    build_codebase_index,
    layer_payload,
)
from .agent_metrics import build_path_metrics
from .agent_protocol import (
    SCHEMA_VERSION,
    SUPPORTED_ACTIONS,
    SUPPORTED_DETAILS,
    SUPPORTED_DIRECTIONS,
    SUPPORTED_GRAPHS,
    SUPPORTED_RELATIONS,
)
from .semantic import Symbol, SymbolReference


OKF_VERSION = "0.2"


class WikiExportError(RuntimeError):
    """Raised when an OKF bundle cannot be created safely."""


@dataclass(frozen=True)
class WikiExportResult:
    output: str
    documents: int
    projects: int
    units: int
    symbols: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "format": "okf",
            "okf_version": OKF_VERSION,
            **asdict(self),
        }


@dataclass(frozen=True)
class _SymbolRecord:
    symbol: Symbol
    page: PurePosixPath


@dataclass(frozen=True)
class _UnitRecord:
    source_path: str
    name: str
    page: PurePosixPath


class _BoundedSourceCache(OrderedDict[str, list[str]]):
    """Keep only a few decoded source files resident during page generation."""

    def __init__(self, max_entries: int = 4) -> None:
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


def export_okf_wiki(
    root: str | Path,
    output: str | Path,
    *,
    project_file: str | Path | None = None,
    workers: int = 0,
    force: bool = False,
) -> WikiExportResult:
    """Export all unique layered codebase knowledge as an OKF 0.2 bundle."""

    root_path = Path(root).expanduser().resolve()
    requested_output, output_path = _resolve_output_destination(output)
    _validate_paths(root_path, output_path, force=force)

    index = build_codebase_index(
        root_path,
        project_file=project_file,
        index_projects=True,
        workers=workers,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.tmp-", dir=output_path.parent)
    )
    try:
        writer = _WikiWriter(index, temporary, workers=workers)
        writer.write()
        documents = writer.documents
        _install_completed_bundle(
            temporary,
            output_path,
            requested_output=requested_output,
            force=force,
        )
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise

    return WikiExportResult(
        output=str(output_path),
        documents=documents,
        projects=len(index.discovery.project_files),
        units=len(index.models),
        symbols=writer.symbol_count,
    )


def _resolve_output_destination(output: str | Path) -> tuple[Path, Path]:
    requested = Path(os.path.abspath(Path(output).expanduser()))
    if requested.is_symlink():
        raise WikiExportError(f"Wiki destination must not be a symlink: {requested}")
    canonical = requested.parent.resolve() / requested.name
    return requested, canonical


def _validate_paths(root: Path, output: Path, *, force: bool) -> None:
    if not root.is_dir():
        raise WikiExportError(f"Repository root is not a directory: {root}")
    if output == root:
        raise WikiExportError("Wiki destination must not be the repository root.")
    if root.is_relative_to(output):
        raise WikiExportError("Wiki destination must not contain the repository root.")
    if output.is_symlink():
        raise WikiExportError(f"Wiki destination must not be a symlink: {output}")
    if output.exists() and not output.is_dir():
        raise WikiExportError(f"Wiki destination already exists and is not a directory: {output}")
    if output.exists() and not force and _directory_has_entries(output):
        raise WikiExportError(
            f"Wiki destination already exists: {output}. Pass --force to replace it."
        )


def _directory_has_entries(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _install_completed_bundle(
    temporary: Path,
    output: Path,
    *,
    requested_output: Path,
    force: bool,
) -> None:
    _revalidate_output_destination(requested_output, output)
    if not output.exists():
        os.replace(temporary, output)
        return
    if not force and _directory_has_entries(output):
        raise WikiExportError(f"Wiki destination already exists: {output}")

    backup = output.parent / f".{output.name}.backup-{os.getpid()}"
    suffix = 0
    while backup.exists():
        suffix += 1
        backup = output.parent / f".{output.name}.backup-{os.getpid()}-{suffix}"
    os.replace(output, backup)
    try:
        os.replace(temporary, output)
    except Exception:
        os.replace(backup, output)
        raise
    shutil.rmtree(backup)


def _revalidate_output_destination(requested: Path, canonical: Path) -> None:
    if requested.is_symlink():
        raise WikiExportError(f"Wiki destination became a symlink: {requested}")
    current = requested.parent.resolve() / requested.name
    if current != canonical:
        raise WikiExportError(
            "Wiki destination parent changed while the bundle was generated."
        )
    if canonical.is_symlink():
        raise WikiExportError(f"Wiki destination must not be a symlink: {canonical}")


class _WikiWriter:
    def __init__(self, index: CodebaseIndex, destination: Path, *, workers: int = 0) -> None:
        self.index = index
        self.destination = destination
        self.workers = workers
        self.documents = 0
        self._source_cache: dict[str, list[str]] = _BoundedSourceCache()
        self.symbols = self._symbol_records()
        self.units = self._unit_records()
        self._symbol_by_key = {
            _symbol_key(record.symbol): record for record in self.symbols
        }
        self._unit_by_source = {
            _normalized_source(record.source_path): record for record in self.units
        }
        self._unit_by_name = {record.name.casefold(): record for record in self.units}
        self._references_by_target: dict[
            tuple[str, str, str, int, int, int, int], list[SymbolReference]
        ] = {}
        self._projects_by_source: dict[str, list[tuple[str, PurePosixPath]]] = {}
        self._unresolved_references: list[SymbolReference] = []
        self._collect_references()

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)

    def write(self) -> None:
        self._write_root_index()
        self._write_overview()
        self._write_manifest()
        self._write_projects()
        self._write_units()
        self._write_symbols()
        self._write_references()
        self._write_problems()
        self._write_metrics()
        self._write_reference_docs()

    def _symbol_records(self) -> list[_SymbolRecord]:
        unique: dict[tuple[str, str, str, int, int, int, int], Symbol] = {}
        for symbol in _all_symbols(self.index):
            if symbol.kind.value == "unit":
                continue
            unique.setdefault(_symbol_key(symbol), symbol)
        records = [
            _SymbolRecord(
                symbol,
                PurePosixPath("symbols")
                / _document_name(
                    symbol.name,
                    "|".join(str(part) for part in _symbol_key(symbol)),
                ),
            )
            for symbol in unique.values()
        ]
        return sorted(
            records,
            key=lambda record: (
                record.symbol.name.casefold(),
                _normalized_source(record.symbol.decl_range.file_name),
                record.symbol.decl_range.start_line,
                record.symbol.decl_range.start_col,
            ),
        )

    def _unit_records(self) -> list[_UnitRecord]:
        records = [
            _UnitRecord(
                source,
                model.unit_scope.name,
                PurePosixPath("units")
                / _document_name(model.unit_scope.name, _normalized_source(source)),
            )
            for source, model in self.index.models.items()
        ]
        return sorted(records, key=lambda record: (record.name.casefold(), record.source_path.casefold()))

    def _collect_references(self) -> None:
        seen_unresolved: set[tuple[str, str, int, int, str]] = set()
        for model in self.index.models.values():
            for reference in model.references:
                if reference.resolved is None:
                    key = (
                        reference.name.casefold(),
                        reference.ref_range.file_name,
                        reference.ref_range.start_line,
                        reference.ref_range.start_col,
                        reference.kind.value,
                    )
                    if key not in seen_unresolved:
                        seen_unresolved.add(key)
                        self._unresolved_references.append(reference)
                    continue
                key = _symbol_key(reference.resolved)
                self._references_by_target.setdefault(key, []).append(reference)
        self._unresolved_references.sort(key=_reference_sort_key)
        for references in self._references_by_target.values():
            references.sort(key=_reference_sort_key)

    def _write_root_index(self) -> None:
        body = [
            "---",
            f'okf_version: "{OKF_VERSION}"',
            "---",
            "# Delphi Codebase Wiki",
            "",
            "Portable knowledge export generated by "
            f"`python-delphi-lsp {__version__}`.",
            "",
            "## Start here",
            "",
            f"- {_link('Overview', PurePosixPath('overview.md'))}",
            f"- {_link('Projects', PurePosixPath('projects/index.md'))}",
            f"- {_link('Units', PurePosixPath('units/index.md'))}",
            f"- {_link('Symbols', PurePosixPath('symbols/index.md'))}",
            f"- {_link('References', PurePosixPath('references/index.md'))}",
            f"- {_link('Problems', PurePosixPath('problems/index.md'))}",
            f"- {_link('Metrics', PurePosixPath('metrics/index.md'))}",
            f"- {_link('Query and graph reference', PurePosixPath('reference/index.md'))}",
            f"- {_link('Export manifest', PurePosixPath('manifest.md'))}",
        ]
        self._write(PurePosixPath("index.md"), "\n".join(body) + "\n")

    def _write_overview(self) -> None:
        overview = layer_payload(self.index, "overview")
        body = [
            f"# {_escape( Path(self.index.root).name or 'Delphi workspace')}",
            "",
            f"- Repository root: `{_escape_code(self.index.root)}`",
            f"- Projects: {overview['project_count']}",
            f"- Sources: {overview['source_count']}",
            f"- Units: {overview['unit_count']}",
            f"- Symbols: {len(self.symbols)}",
            "",
            "## Defines",
            "",
            *_bullet_code_values(overview["defines"]),
            "",
            "## Search paths",
            "",
            *_bullet_code_values(overview["search_paths"]),
            "",
            "## Include paths",
            "",
            *_bullet_code_values(overview["include_paths"]),
            "",
            "## Related knowledge",
            "",
            f"- {_link('Projects', PurePosixPath('projects/index.md'))}",
            f"- {_link('Problems', PurePosixPath('problems/index.md'))}",
            f"- {_link('Workspace metrics', PurePosixPath('metrics/workspace.md'))}",
        ]
        self._concept(
            PurePosixPath("overview.md"),
            concept_type="Delphi Codebase",
            title="Delphi codebase overview",
            description="Discovered projects, sources, units, paths, defines, and diagnostics.",
            tags=("delphi", "codebase", "overview"),
            body=body,
        )

    def _write_manifest(self) -> None:
        body = [
            "# Export manifest",
            "",
            f"- Generator: `python-delphi-lsp/{__version__}`",
            f"- Open Knowledge Format: `{OKF_VERSION}`",
            f"- Projects: {len(self.index.discovery.project_files)}",
            f"- Units: {len(self.units)}",
            f"- Symbols: {len(self.symbols)}",
            "",
            "## Coverage",
            "",
            "- Layered views: overview, projects, units, unit, symbols, symbol, "
            "implementation, references, problems, and metrics.",
            "- Semantic references are attached to their resolved symbol pages; "
            "unresolved references are retained separately.",
            "- Protocol v3 query, relation, CPG, pagination, and cache semantics "
            "are documented under the reference section.",
            "- Lazy CPG results are intentionally not expanded for every symbol, "
            "which avoids redundant parsing and unbounded bundle growth.",
        ]
        self._concept(
            PurePosixPath("manifest.md"),
            concept_type="Knowledge Bundle Manifest",
            title="Export manifest",
            description="Generator, format, counts, and coverage of this knowledge bundle.",
            tags=("manifest", "okf", "delphi"),
            body=body,
        )

    def _write_projects(self) -> None:
        payload = layer_payload(self.index, "projects")
        pages: list[tuple[str, PurePosixPath]] = []
        for item in payload["items"]:
            project_path = str(item["path"])
            title = Path(project_path).name
            page = PurePosixPath("projects") / _document_name(title, _normalized_source(project_path))
            pages.append((title, page))
            project_membership = (
                project_path,
                *(str(unit["path"]) for unit in item["parsed_units"]),
            )
            for source_path in project_membership:
                memberships = self._projects_by_source.setdefault(
                    _normalized_source(source_path), []
                )
                membership = (title, page)
                if membership not in memberships:
                    memberships.append(membership)
            body = [
                f"# {_escape(title)}",
                "",
                f"- Path: `{_escape_code(self._display_path(project_path))}`",
                f"- Parsed units: {len(item['parsed_units'])}",
                f"- Include files: {len(item['include_files'])}",
                f"- Missing units: {len(item['not_found_units'])}",
                f"- Problems: {len(item['problems'])}",
                "",
                "## Parsed units",
                "",
            ]
            if item["parsed_units"]:
                for unit in item["parsed_units"]:
                    unit_record = self._find_unit(str(unit["path"]), str(unit["name"]))
                    label = f"{unit['name']} — {self._display_path(str(unit['path']))}"
                    body.append(
                        f"- {_link(label, unit_record.page) if unit_record else _escape(label)}"
                        f"{' (parse error)' if unit['has_error'] else ''}"
                    )
            else:
                body.append("- None")
            body.extend(["", "## Include files", ""])
            body.extend(
                f"- `{_escape_code(self._display_path(str(value['path'])))}`"
                for value in item["include_files"]
            )
            if not item["include_files"]:
                body.append("- None")
            body.extend(["", "## Missing units", ""])
            body.extend(f"- `{_escape_code(str(value))}`" for value in item["not_found_units"])
            if not item["not_found_units"]:
                body.append("- None")
            body.extend(["", "## Project-index problems", ""])
            body.extend(
                f"- **{_escape(str(problem['kind']))}:** "
                f"{_escape(str(problem['message']))} "
                f"(`{_escape_code(self._display_path(str(problem['file'])))}`)"
                for problem in item["problems"]
            )
            if not item["problems"]:
                body.append("- None")
            body.extend(["", "## Complete indexed data", "", _json_block(item)])
            self._concept(
                page,
                concept_type="Delphi Project",
                title=title,
                description=f"Project index for {title}.",
                tags=("delphi", "project"),
                body=body,
            )
        self._directory_index(
            PurePosixPath("projects/index.md"),
            "Projects",
            pages,
            empty="No Delphi project entry files were discovered.",
        )

    def _write_units(self) -> None:
        pages: list[tuple[str, PurePosixPath]] = []
        symbol_records_by_source: dict[str, list[_SymbolRecord]] = {}
        source_ordered_records = sorted(
            self.symbols,
            key=lambda record: (
                _normalized_source(record.symbol.decl_range.file_name),
                record.symbol.decl_range.start_line,
                record.symbol.decl_range.start_col,
                record.symbol.name.casefold(),
            ),
        )
        for record in source_ordered_records:
            symbol_records_by_source.setdefault(
                _normalized_source(record.symbol.decl_range.file_name), []
            ).append(record)

        for unit in self.units:
            pages.append((unit.name, unit.page))
            symbols = symbol_records_by_source.get(_normalized_source(unit.source_path), [])
            model = self.index.models[unit.source_path]
            references = sorted(model.references, key=_reference_sort_key)
            projects = self._projects_by_source.get(
                _normalized_source(unit.source_path), []
            )
            body = [
                f"# {_escape(unit.name)}",
                "",
                f"- Source: `{_escape_code(self._display_path(unit.source_path))}`",
                f"- Symbols: {len(symbols)}",
                f"- Semantic references: {len(references)}",
                "",
                "## Projects",
                "",
            ]
            body.extend(f"- {_link(title, page)}" for title, page in projects)
            if not projects:
                body.append("- No concrete project membership was resolved")
            body.extend(
                [
                    "",
                    "## Symbols",
                    "",
                ]
            )
            body.extend(
                f"- {_link(record.symbol.name, record.page)} "
                f"— `{record.symbol.kind.value}` at line {record.symbol.decl_range.start_line}"
                for record in symbols
            )
            if not symbols:
                body.append("- None")
            body.extend(["", "## References found in this unit", ""])
            body.extend(self._reference_lines(references))
            if not references:
                body.append("- None")
            metrics_page = PurePosixPath("metrics/units") / _document_name(
                unit.name, _normalized_source(unit.source_path)
            )
            body.extend(["", "## Related knowledge", "", f"- {_link('Unit metrics', metrics_page)}"])
            self._concept(
                unit.page,
                concept_type="Delphi Unit",
                title=unit.name,
                description=f"Symbols and references in {unit.name}.",
                tags=("delphi", "unit"),
                body=body,
            )
        pages.sort(key=lambda item: (item[0].casefold(), item[1].as_posix()))
        self._directory_index(
            PurePosixPath("units/index.md"),
            "Units",
            pages,
            empty="No Delphi units were indexed.",
        )

    def _write_symbols(self) -> None:
        pages: list[tuple[str, PurePosixPath]] = []
        children_by_owner: dict[tuple[str, str], list[_SymbolRecord]] = {}
        source_ordered_records = sorted(
            self.symbols,
            key=lambda record: (
                _normalized_source(record.symbol.decl_range.file_name),
                record.symbol.decl_range.start_line,
                record.symbol.decl_range.start_col,
                record.symbol.name.casefold(),
            ),
        )
        for record in source_ordered_records:
            owner = _symbol_owner(record.symbol)
            children_by_owner.setdefault(
                (_normalized_source(record.symbol.decl_range.file_name), owner.casefold()),
                [],
            ).append(record)

        for record in source_ordered_records:
            symbol = record.symbol
            pages.append((f"{symbol.name} ({symbol.kind.value})", record.page))
            source = self._display_path(symbol.decl_range.file_name)
            unit = self._unit_by_source.get(_normalized_source(symbol.decl_range.file_name))
            owner = _symbol_owner(symbol)
            owner_record = self._find_symbol_owner(symbol)
            children = [
                child
                for child in children_by_owner.get(
                    (_normalized_source(symbol.decl_range.file_name), symbol.name.casefold()),
                    [],
                )
                if child.page != record.page
            ]
            fragment = _source_fragment(symbol.decl_range, "declaration", self._source_cache)
            references = self._references_by_target.get(_symbol_key(symbol), [])
            modifiers = sorted(value.value for value in symbol.modifiers)
            body = [
                f"# {_escape(symbol.name)}",
                "",
                f"- Kind: `{symbol.kind.value}`",
                f"- Type: `{_escape_code(symbol.type_ref.display_name())}`",
                f"- Visibility: `{symbol.visibility.value}`",
                f"- Owner: "
                f"{_link(owner, owner_record.page) if owner_record else f'`{_escape_code(owner)}`'}",
                f"- Unit: {_link(unit.name, unit.page) if unit else '`unknown`'}",
                f"- Location: `{_escape_code(source)}:{symbol.decl_range.start_line}:"
                f"{symbol.decl_range.start_col}`",
                f"- Range: `{symbol.decl_range.start_line}:{symbol.decl_range.start_col}-"
                f"{symbol.decl_range.end_line}:{symbol.decl_range.end_col}`",
                f"- Modifiers: {', '.join(f'`{value}`' for value in modifiers) or 'None'}",
                f"- Base types: "
                f"{', '.join(f'`{_escape_code(value.display_name())}`' for value in symbol.base_types) or 'None'}",
            ]
            if symbol.doc:
                body.extend(["", "## Documentation", "", symbol.doc])
            if symbol.attributes:
                body.extend(["", "## Attributes", "", _json_block(symbol.attributes)])
            body.extend(["", "## Declaration or implementation", ""])
            if fragment is None:
                body.append("Source fragment unavailable.")
            else:
                body.extend(
                    [
                        f"`{_escape_code(self._display_path(str(fragment['range']['path'])))}:"
                        f"{fragment['range']['start_line']}`",
                        "",
                        _code_block(str(fragment["text"]), "pascal"),
                    ]
                )
            body.extend(["", "## Members", ""])
            body.extend(
                f"- {_link(child.symbol.name, child.page)} — `{child.symbol.kind.value}`"
                for child in children
            )
            if not children:
                body.append("- None")
            body.extend(["", "## Semantic references", ""])
            body.extend(self._reference_lines(references))
            if not references:
                body.append("- None")
            self._concept(
                record.page,
                concept_type=f"Delphi {symbol.kind.value.replace('_', ' ').title()} Symbol",
                title=symbol.name,
                description=(
                    f"{symbol.kind.value} {symbol.name} in {source} at "
                    f"line {symbol.decl_range.start_line}."
                ),
                tags=("delphi", "symbol", symbol.kind.value),
                body=body,
            )
        pages.sort(key=lambda item: (item[0].casefold(), item[1].as_posix()))
        self._directory_index(
            PurePosixPath("symbols/index.md"),
            "Symbols",
            pages,
            empty="No non-unit symbols were indexed.",
        )

    def _write_references(self) -> None:
        resolved_pages = [
            (f"{record.symbol.name} ({len(self._references_by_target[_symbol_key(record.symbol)])})", record.page)
            for record in self.symbols
            if _symbol_key(record.symbol) in self._references_by_target
        ]
        unresolved_page = PurePosixPath("references/unresolved.md")
        unresolved_body = [
            "# Unresolved semantic references",
            "",
            "These names were observed by the tolerant semantic index but could "
            "not be resolved soundly in the current workspace.",
            "",
            *self._reference_lines(self._unresolved_references),
        ]
        if not self._unresolved_references:
            unresolved_body.append("- None")
        self._concept(
            unresolved_page,
            concept_type="Delphi Unresolved References",
            title="Unresolved semantic references",
            description="References that remain unresolved in the tolerant semantic index.",
            tags=("delphi", "references", "diagnostics"),
            body=unresolved_body,
        )
        lines = [
            "# References",
            "",
            "Resolved references are embedded on their target symbol pages.",
            "",
        ]
        lines.extend(f"- {_link(label, page)}" for label, page in resolved_pages)
        if not resolved_pages:
            lines.append("- No resolved references")
        lines.extend(["", f"- {_link('Unresolved references', unresolved_page)}"])
        self._write(PurePosixPath("references/index.md"), "\n".join(lines) + "\n")

    def _write_problems(self) -> None:
        payload = layer_payload(self.index, "problems")
        page = PurePosixPath("problems/workspace.md")
        body = ["# Workspace problems", "", f"Problems: {len(payload['items'])}", ""]
        body.extend(
            f"- **{_escape(str(item['kind']))}:** {_escape(str(item['message']))} "
            f"(`{_escape_code(self._display_path(str(item.get('origin', ''))))}`)"
            for item in payload["items"]
        )
        if not payload["items"]:
            body.append("- None")
        body.extend(["", "## Complete problem data", "", _json_block(payload["items"])])
        self._concept(
            page,
            concept_type="Delphi Workspace Problems",
            title="Workspace problems",
            description="Discovery and deep-project indexing problems.",
            tags=("delphi", "problems", "diagnostics"),
            body=body,
        )
        self._directory_index(
            PurePosixPath("problems/index.md"),
            "Problems",
            [("Workspace problems", page)],
            empty="No problem report was generated.",
        )

    def _write_metrics(self) -> None:
        project_name = "Workspace"
        if len(self.index.discovery.project_files) == 1:
            project_name = Path(self.index.discovery.project_files[0]).stem
        metrics = build_path_metrics(
            self.index.root,
            self.index.discovery.source_files,
            defines=self.index.discovery.defines,
            include_paths=self.index.discovery.include_paths,
            project_name=project_name,
            workers=self.workers,
        )
        payload = {
            "project": metrics.to_mapping(),
            "items": [unit.to_mapping(detail=True) for unit in metrics.units],
        }
        workspace_page = PurePosixPath("metrics/workspace.md")
        project = payload["project"]
        workspace_body = [
            "# Workspace metrics",
            "",
            f"- Total LOC: {project['total_loc']}",
            f"- Total LOC with includes: {project['total_loc_with_includes']}",
            f"- Units: {project['unit_count']}",
            f"- Maintainability index: {project['maintainability_index']:.2f}",
            "",
            "## Complete metric data",
            "",
            _json_block(project),
        ]
        self._concept(
            workspace_page,
            concept_type="Delphi Project Metrics",
            title="Workspace metrics",
            description="Aggregate architecture and maintainability metrics.",
            tags=("delphi", "metrics", "architecture"),
            body=workspace_body,
        )

        unit_pages: list[tuple[str, PurePosixPath]] = []
        for item in payload["items"]:
            unit = self._find_unit(str(item["path"]), str(item["name"]))
            metric_identity = (
                _normalized_source(unit.source_path)
                if unit is not None
                else _normalized_source(str(item["path"]))
            )
            page = PurePosixPath("metrics/units") / _document_name(
                str(item["name"]), metric_identity
            )
            unit_pages.append((str(item["name"]), page))
            body = [
                f"# {_escape(str(item['name']))} metrics",
                "",
                f"- Unit: {_link(str(item['name']), unit.page) if unit else _escape(str(item['name']))}",
                f"- Source: `{_escape_code(self._display_path(str(item['path'])))}`",
                f"- LOC: {item['lines']['total_lines']}",
                f"- Cyclomatic maximum: {item['cyclomatic']['maximum']}",
                f"- Maintainability index: {item['maintainability_index']:.2f}",
                f"- Instability: {item['instability']:.3f}",
                f"- Abstractness: {item['abstractness']:.3f}",
                f"- Distance: {item['distance']:.3f}",
                "",
                "## Complete metric data",
                "",
                _json_block(item),
            ]
            self._concept(
                page,
                concept_type="Delphi Unit Metrics",
                title=f"{item['name']} metrics",
                description=f"Architecture and maintainability metrics for {item['name']}.",
                tags=("delphi", "metrics", "unit"),
                body=body,
            )
        self._directory_index(
            PurePosixPath("metrics/units/index.md"),
            "Unit metrics",
            unit_pages,
            empty="No per-unit metrics were calculated.",
        )
        self._directory_index(
            PurePosixPath("metrics/index.md"),
            "Metrics",
            [("Workspace metrics", workspace_page), ("Unit metrics", PurePosixPath("metrics/units/index.md"))],
            empty="No metrics were calculated.",
        )

    def _write_reference_docs(self) -> None:
        protocol_page = PurePosixPath("reference/protocol.md")
        protocol_body = [
            "# Agent Protocol v3",
            "",
            f"- Schema version: `{SCHEMA_VERSION}`",
            f"- Actions: {_inline_code(SUPPORTED_ACTIONS)}",
            f"- Detail modes: {_inline_code(SUPPORTED_DETAILS)}",
            f"- Relations: {_inline_code(SUPPORTED_RELATIONS)}",
            f"- Graphs: {_inline_code(SUPPORTED_GRAPHS)}",
            f"- Directions: {_inline_code(SUPPORTED_DIRECTIONS)}",
            "",
            "Query pages are revision-bound and may use cursors. Live requests "
            "bound result size with `max_items` and `max_chars`; this static "
            "bundle instead stores each unique exported fact once.",
        ]
        self._concept(
            protocol_page,
            concept_type="Delphi Agent Protocol Reference",
            title="Agent Protocol v3",
            description="Supported query actions, detail modes, relations, and pagination.",
            tags=("delphi", "protocol", "query"),
            body=protocol_body,
        )

        cpg_page = PurePosixPath("reference/cpg.md")
        cpg_body = [
            "# Code Property Graph",
            "",
            f"Available graph projections: {_inline_code(SUPPORTED_GRAPHS)}.",
            f"Traversal directions: {_inline_code(SUPPORTED_DIRECTIONS)}.",
            "",
            "CPG queries lazily combine AST, control-flow, data-flow, and call "
            "edges for a focused target. They remain live queries because "
            "eagerly materializing every overlapping target would repeat source "
            "parsing and make a multi-million-line export unbounded.",
            "",
            "The exporter retains symbol locations, implementations, semantic "
            "references, metrics, and the supported graph contract needed to "
            "select a target for a subsequent live CPG query.",
        ]
        self._concept(
            cpg_page,
            concept_type="Delphi Code Property Graph Reference",
            title="Code Property Graph",
            description="Available CPG projections, traversal controls, and export boundaries.",
            tags=("delphi", "cpg", "graph"),
            body=cpg_body,
        )

        cache_page = PurePosixPath("reference/cache.md")
        cache_body = [
            "# Cache behavior",
            "",
            "The shared cache daemon prewarms a repository navigation context, "
            "reuses it across CLI queries, invalidates it when the workspace "
            "revision changes, and keeps memory bounded by its configured limit.",
            "",
            "The wiki export is independent of daemon lifetime. It performs one "
            "parallel index build and writes a portable on-disk snapshot.",
        ]
        self._concept(
            cache_page,
            concept_type="Delphi Cache Reference",
            title="Cache behavior",
            description="Relationship between the live bounded cache and static wiki exports.",
            tags=("delphi", "cache", "performance"),
            body=cache_body,
        )

        layers_page = PurePosixPath("reference/layers.md")
        layers_body = [
            "# Layer mapping",
            "",
            "- `overview` → `overview.md`",
            "- `projects` → `projects/`",
            "- `units` and `unit` → `units/`",
            "- `symbols`, `symbol`, and `implementation` → `symbols/`",
            "- `references` → symbol pages and `references/`",
            "- `problems` → `problems/`",
            "- `metrics` → `metrics/`",
        ]
        self._concept(
            layers_page,
            concept_type="Delphi Layer Export Reference",
            title="Layer mapping",
            description="Mapping from live layered views to static wiki concepts.",
            tags=("delphi", "layers", "export"),
            body=layers_body,
        )
        self._directory_index(
            PurePosixPath("reference/index.md"),
            "Query and graph reference",
            [
                ("Agent Protocol v3", protocol_page),
                ("Layer mapping", layers_page),
                ("Code Property Graph", cpg_page),
                ("Cache behavior", cache_page),
            ],
            empty="No reference documents were generated.",
        )

    def _reference_lines(self, references: Iterable[SymbolReference]) -> list[str]:
        lines: list[str] = []
        for reference in references:
            location = (
                f"{self._display_path(reference.ref_range.file_name)}:"
                f"{reference.ref_range.start_line}:{reference.ref_range.start_col}"
            )
            target = self._symbol_by_key.get(_symbol_key(reference.resolved)) if reference.resolved else None
            target_text = (
                f" → {_link(reference.resolved.name, target.page)}"
                if reference.resolved is not None and target is not None
                else ""
            )
            lines.append(
                f"- `{reference.kind.value}` `{_escape_code(reference.name)}` at "
                f"`{_escape_code(location)}`{target_text}"
            )
        return lines

    def _find_unit(self, source: str, name: str) -> _UnitRecord | None:
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            source_path = Path(self.index.root) / source_path
        return self._unit_by_source.get(
            _normalized_source(str(source_path))
        ) or self._unit_by_name.get(name.casefold())

    def _find_symbol_owner(self, symbol: Symbol) -> _SymbolRecord | None:
        if symbol.scope.owner is None:
            return None
        return self._symbol_by_key.get(_symbol_key(symbol.scope.owner))

    def _display_path(self, value: str) -> str:
        if not value:
            return ""
        path = Path(value)
        try:
            return path.resolve().relative_to(Path(self.index.root)).as_posix()
        except (OSError, ValueError):
            return path.as_posix()

    def _concept(
        self,
        page: PurePosixPath,
        *,
        concept_type: str,
        title: str,
        description: str,
        tags: tuple[str, ...],
        body: list[str],
    ) -> None:
        frontmatter = [
            "---",
            f"type: {json.dumps(concept_type, ensure_ascii=False)}",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            f"description: {json.dumps(description, ensure_ascii=False)}",
            f"tags: {json.dumps(list(tags), ensure_ascii=False)}",
            'status: "stable"',
            "---",
        ]
        self._write(page, "\n".join([*frontmatter, *body]).rstrip() + "\n")

    def _directory_index(
        self,
        page: PurePosixPath,
        title: str,
        items: list[tuple[str, PurePosixPath]],
        *,
        empty: str,
    ) -> None:
        body = [f"# {_escape(title)}", ""]
        body.extend(f"- {_link(label, target)}" for label, target in items)
        if not items:
            body.append(empty)
        self._write(page, "\n".join(body).rstrip() + "\n")

    def _write(self, relative_path: PurePosixPath, text: str) -> None:
        target = self.destination.joinpath(*relative_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        self.documents += 1


def _symbol_key(symbol: Symbol | None) -> tuple[str, str, str, int, int, int, int]:
    if symbol is None:
        return ("", "", "", 0, 0, 0, 0)
    source_range = symbol.decl_range
    return (
        _normalized_source(source_range.file_name),
        symbol.name.casefold(),
        symbol.kind.value,
        source_range.start_line,
        source_range.start_col,
        source_range.end_line,
        source_range.end_col,
    )


def _symbol_owner(symbol: Symbol) -> str:
    return symbol.scope.owner.name if symbol.scope.owner is not None else symbol.scope.name


def _normalized_source(value: str) -> str:
    return os.path.normcase(os.path.abspath(value)).replace("\\", "/")


def _reference_sort_key(reference: SymbolReference) -> tuple[str, int, int, str, str]:
    return (
        _normalized_source(reference.ref_range.file_name),
        reference.ref_range.start_line,
        reference.ref_range.start_col,
        reference.kind.value,
        reference.name.casefold(),
    )


def _document_name(label: str, identity: str) -> str:
    normalized = unicodedata.normalize("NFKD", label)
    ascii_label = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_label).strip("-")[:64] or "concept"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}.md"


def _link(label: str, target: PurePosixPath) -> str:
    return f"[{_escape(label)}](/{target.as_posix()})"


def _escape(value: str) -> str:
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", value)


def _escape_code(value: str) -> str:
    return value.replace("`", "\\`")


def _bullet_code_values(values: Iterable[Any]) -> list[str]:
    rendered = [f"- `{_escape_code(str(value))}`" for value in values]
    return rendered or ["- None"]


def _inline_code(values: Iterable[str]) -> str:
    return ", ".join(f"`{_escape_code(value)}`" for value in values)


def _json_block(value: Any) -> str:
    return _code_block(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True), "json")


def _code_block(value: str, language: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"


__all__ = [
    "OKF_VERSION",
    "WikiExportError",
    "WikiExportResult",
    "export_okf_wiki",
]
