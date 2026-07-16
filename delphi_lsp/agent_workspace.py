from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import unicodedata

from .agent_protocol import AgentProtocolError, Focus, make_target_id
from .consts import AttributeName, SyntaxNodeType
from .lsp_server import multiline_string_block_end, outline_source
from .project_discovery import (
    SKIP_DIRS,
    SOURCE_EXTENSIONS,
    DelphiProjectDiscovery,
    discover_delphi_project,
    discover_workspace_sources,
    populate_workspace_sources,
)
from .project_indexer import IncludeFileInfo, ProjectIndexResult, ProjectIndexer, UnitInfo


_PROJECT_SNAPSHOT_EXTENSIONS = (".pas", ".dpr", ".dpk", ".dproj", ".cfg", ".dof")
_UNIT_SNAPSHOT_EXTENSIONS = (".pas", ".dpr", ".dpk")
_RELATIVE_INCLUDE_EXTENSIONS = (".inc",)


@dataclass(frozen=True)
class AgentProject:
    project_id: str
    name: str
    path: str
    kind: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class AgentUnit:
    unit_id: str
    name: str
    path: str
    has_error: bool

    def to_mapping(self) -> dict[str, str | bool]:
        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "path": self.path,
            "has_error": self.has_error,
        }


@dataclass(frozen=True)
class _ProjectCache:
    result: ProjectIndexResult
    fingerprint: str


@dataclass
class _DirectorySnapshotSpec:
    path: Path
    immediate_extensions: set[str]
    recursive_extensions: set[str]


class AgentWorkspace:
    def __init__(
        self,
        root: Path,
        discovery: DelphiProjectDiscovery,
        projects: tuple[AgentProject, ...],
        project_paths: dict[str, Path | None],
    ) -> None:
        self._root = root
        self._discovery = discovery
        self._projects = projects
        self._project_paths = project_paths
        self._active_project_id = ""
        self._focus = Focus()
        self._active_discovery: DelphiProjectDiscovery | None = None
        self._active_result: ProjectIndexResult | None = None
        self._units: tuple[AgentUnit, ...] = ()
        self._include_files: tuple[dict[str, str], ...] = ()
        self._project_cache: dict[str, _ProjectCache] = {}

    @classmethod
    def open(
        cls,
        root: str | Path,
        project_file: str | Path | None = None,
    ) -> AgentWorkspace:
        root_path = Path(root).expanduser().resolve()
        resolved_project_file: Path | None = None
        if project_file is not None:
            resolved_project_file = Path(project_file).expanduser()
            if not resolved_project_file.is_absolute():
                resolved_project_file = root_path / resolved_project_file
            resolved_project_file = resolved_project_file.resolve()
        discovery = discover_delphi_project(
            root_path,
            project_file=resolved_project_file,
            scan_workspace_sources=False,
        )
        projects: list[AgentProject] = []
        project_paths: dict[str, Path | None] = {}
        if discovery.project_files:
            for value in discovery.project_files:
                path = Path(value)
                relative_path = _display_path(path, root_path)
                name = path.stem
                project_id = make_target_id("project", relative_path, name)
                project = AgentProject(
                    project_id=project_id,
                    name=name,
                    path=relative_path,
                    kind=_project_kind(path),
                )
                projects.append(project)
                project_paths[project_id] = path
        else:
            populate_workspace_sources(discovery)
            project_id = make_target_id("project", "", "workspace")
            projects.append(
                AgentProject(
                    project_id=project_id,
                    name="Workspace",
                    path=".",
                    kind="workspace",
                )
            )
            project_paths[project_id] = None

        workspace = cls(root_path, discovery, tuple(projects), project_paths)
        if len(projects) == 1:
            workspace.select_project(projects[0].project_id)
        return workspace

    @property
    def projects(self) -> tuple[AgentProject, ...]:
        return self._projects

    @property
    def root(self) -> Path:
        return self._root

    @property
    def search_paths(self) -> tuple[str, ...]:
        discovery = self._active_discovery or self._discovery
        return tuple(discovery.search_paths)

    @property
    def include_paths(self) -> tuple[str, ...]:
        discovery = self._active_discovery or self._discovery
        return tuple(discovery.include_paths)

    @property
    def defines(self) -> tuple[str, ...]:
        discovery = self._active_discovery or self._discovery
        return tuple(discovery.defines)

    @property
    def active_project(self) -> AgentProject | None:
        return next(
            (project for project in self._projects if project.project_id == self._active_project_id),
            None,
        )

    @property
    def active_project_id(self) -> str:
        return self._active_project_id

    @property
    def focus(self) -> Focus:
        return self._focus

    @property
    def units(self) -> tuple[AgentUnit, ...]:
        return self._units

    @property
    def include_files(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(item) for item in self._include_files)

    @property
    def search_path_entries(self) -> tuple[dict[str, object], ...]:
        discovery = self._active_discovery or self._discovery
        return self._path_entries(discovery.search_paths, discovery.search_path_origins)

    @property
    def include_path_entries(self) -> tuple[dict[str, object], ...]:
        discovery = self._active_discovery or self._discovery
        return self._path_entries(discovery.include_paths, discovery.include_path_origins)

    @property
    def define_entries(self) -> tuple[dict[str, object], ...]:
        discovery = self._active_discovery or self._discovery
        return tuple(
            {
                "define": define,
                "origins": [self._display_origin(origin) for origin in discovery.define_origins.get(define, [])],
            }
            for define in discovery.defines
        )

    @property
    def problems(self) -> tuple[dict[str, str], ...]:
        entries: list[dict[str, str]] = []
        seen_discovery: set[tuple[str, str, str]] = set()
        discovery = self._active_discovery or self._discovery
        for problem in discovery.problems:
            key = (problem.kind, problem.message, problem.origin)
            if key in seen_discovery:
                continue
            seen_discovery.add(key)
            entries.append(
                {
                    "kind": problem.kind,
                    "message": problem.message,
                    "origin": self._display_origin(problem.origin),
                }
            )

        active_project = self.active_project
        if active_project is not None and self._active_result is not None:
            seen_project: set[tuple[str, str, str]] = set()
            for problem in self._active_result.problems:
                key = (problem.problem_type.value, problem.file_name, problem.description)
                if key in seen_project:
                    continue
                seen_project.add(key)
                entries.append(
                    {
                        "kind": problem.problem_type.value,
                        "message": problem.description,
                        "origin": active_project.path,
                        "path": self._display_problem_path(problem.file_name),
                    }
                )
        return tuple(entries)

    @property
    def workspace_revision(self) -> str:
        discovery = self._active_discovery or self._discovery
        result = self._active_result
        if self._active_project_id:
            project_path = self._project_paths[self._active_project_id]
            if project_path is None:
                discovery = discover_workspace_sources(self._root)
                result = _catalog_workspace_sources(discovery)
            else:
                discovery = discover_delphi_project(
                    self._root,
                    project_file=project_path,
                    scan_workspace_sources=False,
                )
        fingerprint = _selection_fingerprint(discovery, result, root=self._root)
        return f"workspace_v2_{fingerprint}"

    def select_project(self, project_id: str) -> None:
        if project_id not in self._project_paths:
            raise AgentProtocolError("project_not_found", f"Project not found: {project_id}.")
        project_path = self._project_paths[project_id]
        cached = self._project_cache.get(project_id)
        if project_path is None:
            discovery = self._discovery if cached is None else discover_workspace_sources(self._root)
        else:
            discovery = discover_delphi_project(
                self._root,
                project_file=project_path,
                scan_workspace_sources=False,
            )
        if cached is not None:
            fingerprint = _selection_fingerprint(discovery, cached.result, root=self._root)
            if fingerprint == cached.fingerprint:
                self._activate_project(project_id, discovery, cached.result)
                return

        if project_path is None:
            result = _catalog_workspace_sources(discovery)
        else:
            indexer = ProjectIndexer(
                search_paths=discovery.search_paths,
                include_paths=discovery.include_paths,
                defines=discovery.defines,
                source_transform=_outline_agent_source,
            )
            result = indexer.index(str(project_path))
        self._project_cache[project_id] = _ProjectCache(
            result=result,
            fingerprint=_selection_fingerprint(discovery, result, root=self._root),
        )
        self._activate_project(project_id, discovery, result)

    def _activate_project(
        self,
        project_id: str,
        discovery: DelphiProjectDiscovery,
        result: ProjectIndexResult,
    ) -> None:
        units = []
        for item in result.parsed_units:
            relative_path = _display_path(Path(item.path), self._root)
            units.append(
                AgentUnit(
                    unit_id=make_target_id("unit", relative_path, item.name),
                    name=item.name,
                    path=relative_path,
                    has_error=item.has_error,
                )
            )
        units.sort(key=lambda item: (item.name.casefold(), item.path.casefold(), item.name, item.path))
        include_files = tuple(
            {
                "name": item.name,
                "path": _display_path(Path(item.path), self._root),
            }
            for item in sorted(
                result.include_files,
                key=lambda item: (item.name.casefold(), item.path.casefold(), item.name, item.path),
            )
        )

        self._active_discovery = discovery
        self._active_result = result
        if self._project_paths[project_id] is None:
            self._discovery = discovery
        self._units = tuple(units)
        self._include_files = include_files
        self._active_project_id = project_id
        self._focus = Focus(project_id=project_id, unit_id="", target_id="")

    def _path_entries(
        self,
        paths: list[str],
        origins: dict[str, list[str]],
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "path": _display_path(Path(path), self._root),
                "origins": [self._display_origin(origin) for origin in origins.get(path, [])],
            }
            for path in paths
        )

    def _display_origin(self, origin: str) -> str:
        path = Path(origin).expanduser()
        if path.is_absolute():
            return _display_path(path, self._root)
        return origin.replace("\\", "/")

    def _display_problem_path(self, value: str) -> str:
        path = Path(value).expanduser()
        if path.is_absolute():
            return _display_path(path, self._root)
        return value.replace("\\", "/")


def _display_path(path: Path, root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def unit_source_path(root: Path, unit: AgentUnit) -> Path:
    path = Path(unit.path)
    if not path.is_absolute():
        path = root / path
    return path.expanduser().resolve()


def unit_display_path(root: Path, unit: AgentUnit) -> str:
    source_path = unit_source_path(root, unit)
    try:
        return source_path.relative_to(root).as_posix()
    except ValueError:
        unit_component = _stable_path_component(unit.name or source_path.stem)
        file_component = _stable_path_component(source_path.name)
        return f"@external/{unit_component}/{file_component}"


def unit_target_id(root: Path, unit: AgentUnit) -> str:
    return make_target_id("unit", unit_display_path(root, unit), unit.name)


def _stable_path_component(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\\", "_").replace("/", "_")
    return normalized or "unknown"


def _outline_agent_source(text: str) -> str:
    return _compact_outline_whitespace(outline_source(text))


def _compact_outline_whitespace(text: str) -> str:
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            start = i
            while i < n and text[i].isspace():
                i += 1
            if parts and i < n and not parts[-1][-1].isspace():
                parts.append(_collapsed_whitespace(text, start, i))
            continue

        start = i
        if text[i] == "'":
            block_end = multiline_string_block_end(text, i)
            i = block_end if block_end is not None else _quoted_string_end(text, i)
        elif text.startswith("//", i):
            i = _line_comment_end(text, i)
        elif text[i] == "{":
            close = text.find("}", i + 1)
            i = n if close < 0 else close + 1
        elif text.startswith("(*", i):
            close = text.find("*)", i + 2)
            i = n if close < 0 else close + 2
        else:
            i += 1
            while i < n:
                if text[i].isspace() or text[i] in {"'", "{"}:
                    break
                if text.startswith("//", i) or text.startswith("(*", i):
                    break
                i += 1
        parts.append(text[start:i])
    return "".join(parts)


def _collapsed_whitespace(text: str, start: int, end: int) -> str:
    for index in range(start, end):
        if text[index] == "\r":
            if index + 1 < end and text[index + 1] == "\n":
                return "\r\n"
            return "\r"
        if text[index] == "\n":
            return "\n"
    return " "


def _quoted_string_end(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] != "'":
            i += 1
            continue
        if i + 1 < len(text) and text[i + 1] == "'":
            i += 2
            continue
        return i + 1
    return len(text)


def _line_comment_end(text: str, start: int) -> int:
    i = start + 2
    while i < len(text) and text[i] not in {"\r", "\n"}:
        i += 1
    if i < len(text) and text[i] == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
        return i + 2
    if i < len(text):
        return i + 1
    return i


def _project_kind(path: Path) -> str:
    if path.suffix.casefold() == ".dpr":
        return "program"
    if path.suffix.casefold() == ".dpk":
        return "package"
    return "project"


def _catalog_workspace_sources(discovery: DelphiProjectDiscovery) -> ProjectIndexResult:
    parsed_units: list[UnitInfo] = []
    include_files: list[IncludeFileInfo] = []
    for source in discovery.source_files:
        path = Path(source)
        if path.suffix.casefold() in {".pas", ".dpr", ".dpk"}:
            parsed_units.append(UnitInfo(name=path.stem, path=source, syntax_tree=None))
        elif path.suffix.casefold() == ".inc":
            include_files.append(IncludeFileInfo(name=path.name, path=source))

    return ProjectIndexResult(
        parsed_units=sorted(
            parsed_units,
            key=lambda item: (item.name.casefold(), item.path.casefold(), item.name, item.path),
        ),
        include_files=sorted(
            include_files,
            key=lambda item: (item.name.casefold(), item.path.casefold(), item.name, item.path),
        ),
        problems=[],
        not_found_units=[],
    )


def _selection_fingerprint(
    discovery: DelphiProjectDiscovery,
    result: ProjectIndexResult | None,
    *,
    root: Path,
) -> str:
    state = _selection_state(discovery, result, root=root)
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _selection_state(
    discovery: DelphiProjectDiscovery,
    result: ProjectIndexResult | None,
    *,
    root: Path,
) -> dict[str, object]:
    return {
        "files": _file_records(_selection_paths(discovery, result), root=root),
        "search_paths": [_display_path(Path(path), root) for path in discovery.search_paths],
        "include_paths": [_display_path(Path(path), root) for path in discovery.include_paths],
        "defines": sorted(discovery.defines, key=lambda item: (item.casefold(), item)),
        "directories": _directory_snapshots(
            _selection_snapshot_specs(discovery, result),
            root=root,
        ),
    }


def _selection_paths(
    discovery: DelphiProjectDiscovery,
    result: ProjectIndexResult | None,
) -> set[str]:
    paths = {
        *discovery.project_files,
        *discovery.config_files,
        *discovery.source_files,
    }
    if result is not None:
        paths.update(unit.path for unit in result.parsed_units)
        paths.update(include.path for include in result.include_files)
        paths.update(_explicit_dependency_paths(result))
    return paths


def _explicit_dependency_paths(result: ProjectIndexResult) -> set[str]:
    paths: dict[str, str] = {}
    for unit in sorted(
        result.parsed_units,
        key=lambda item: (item.path.casefold(), item.path, item.name.casefold(), item.name),
    ):
        if unit.syntax_tree is None:
            continue
        declaring_folder = Path(unit.path).expanduser().resolve().parent
        pending = [unit.syntax_tree]
        while pending:
            node = pending.pop()
            if node.typ == SyntaxNodeType.ntUnit:
                raw_path = node.get_attribute(AttributeName.anPath).strip()
                if raw_path:
                    candidate = Path(raw_path.replace("\\", "/"))
                    if not candidate.is_absolute():
                        candidate = declaring_folder / candidate
                    resolved = str(candidate.resolve())
                    paths.setdefault(resolved.casefold(), resolved)
            pending.extend(reversed(node.child_nodes))
    return set(paths.values())


def _selection_snapshot_specs(
    discovery: DelphiProjectDiscovery,
    result: ProjectIndexResult | None,
) -> list[_DirectorySnapshotSpec]:
    specs: dict[str, _DirectorySnapshotSpec] = {}

    def add(
        path: Path,
        *,
        immediate: tuple[str, ...] = (),
        recursive: tuple[str, ...] = (),
    ) -> None:
        resolved = path.expanduser().resolve()
        key = str(resolved).casefold()
        spec = specs.setdefault(
            key,
            _DirectorySnapshotSpec(
                path=resolved,
                immediate_extensions=set(),
                recursive_extensions=set(),
            ),
        )
        spec.immediate_extensions.update(immediate)
        spec.recursive_extensions.update(recursive)

    for project in discovery.project_files:
        add(
            Path(project).parent,
            immediate=_PROJECT_SNAPSHOT_EXTENSIONS,
            recursive=_RELATIVE_INCLUDE_EXTENSIONS,
        )
    for path in discovery.search_paths:
        add(
            Path(path),
            immediate=_UNIT_SNAPSHOT_EXTENSIONS,
            recursive=_RELATIVE_INCLUDE_EXTENSIONS,
        )
    for path in discovery.include_paths:
        add(Path(path), recursive=SOURCE_EXTENSIONS)
    if result is not None:
        for unit in result.parsed_units:
            add(
                Path(unit.path).parent,
                immediate=_UNIT_SNAPSHOT_EXTENSIONS,
                recursive=_RELATIVE_INCLUDE_EXTENSIONS,
            )
        for include in result.include_files:
            add(
                Path(include.path).parent,
                immediate=_UNIT_SNAPSHOT_EXTENSIONS,
                recursive=_RELATIVE_INCLUDE_EXTENSIONS,
            )
    return sorted(specs.values(), key=lambda item: (str(item.path).casefold(), str(item.path)))


def _directory_snapshots(
    specs: list[_DirectorySnapshotSpec],
    *,
    root: Path,
) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for spec in specs:
        directory = spec.path
        entries: set[str] = set()
        readable = True
        try:
            if spec.immediate_extensions:
                for candidate in directory.iterdir():
                    if candidate.suffix.casefold() in spec.immediate_extensions and candidate.is_file():
                        entries.add(str(candidate.resolve()))
            if spec.recursive_extensions:
                for candidate in directory.rglob("*"):
                    if any(part in SKIP_DIRS for part in candidate.parts):
                        continue
                    if candidate.suffix.casefold() in spec.recursive_extensions and candidate.is_file():
                        entries.add(str(candidate.resolve()))
        except OSError:
            readable = False
        snapshots.append(
            {
                "path": _display_path(directory, root),
                "is_directory": directory.is_dir(),
                "readable": readable,
                "entries": _file_records(entries, root=root),
            }
        )
    return snapshots


def _file_records(
    paths: set[str],
    *,
    root: Path,
) -> list[tuple[str, int | None, int | None]]:
    records: list[tuple[str, int | None, int | None]] = []
    for value in sorted(paths, key=lambda item: (item.casefold(), item)):
        path = Path(value).expanduser().resolve()
        exposed_path = _display_path(path, root)
        try:
            stat = path.stat()
        except OSError:
            records.append((exposed_path, None, None))
        else:
            records.append((exposed_path, stat.st_size, stat.st_mtime_ns))
    return records


__all__ = [
    "AgentProject",
    "AgentUnit",
    "AgentWorkspace",
    "unit_display_path",
    "unit_source_path",
    "unit_target_id",
]
