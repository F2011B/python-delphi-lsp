from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from .consts import AttributeName, SyntaxNodeType
from .nodes import SyntaxNode
from .parser import DelphiParser
from .preprocessor import IncludeLoader
from .source_reader import read_source_text


class ProjectProblemType(str, Enum):
    CANT_FIND_FILE = 'cant_find_file'
    CANT_OPEN_FILE = 'cant_open_file'
    CANT_PARSE_FILE = 'cant_parse_file'


@dataclass
class ProjectProblem:
    problem_type: ProjectProblemType
    file_name: str
    description: str


@dataclass
class UnitErrorInfo:
    line: int = 0
    col: int = 0
    error: str = ''


@dataclass
class UnitInfo:
    name: str
    path: str
    syntax_tree: Optional[SyntaxNode]
    has_error: bool = False
    error_info: UnitErrorInfo = field(default_factory=UnitErrorInfo)


@dataclass
class IncludeFileInfo:
    name: str
    path: str


@dataclass
class ProjectIndexResult:
    parsed_units: list[UnitInfo]
    include_files: list[IncludeFileInfo]
    problems: list[ProjectProblem]
    not_found_units: list[str]


GetUnitSyntaxHook = Callable[[str], tuple[Optional[SyntaxNode], bool, bool]]
UnitParsedHook = Callable[[str, str, SyntaxNode, bool], bool]
SourceTransform = Callable[[str], str]


class ProjectIndexer:
    def __init__(
        self,
        *,
        search_paths: Iterable[str] = (),
        include_paths: Iterable[str] = (),
        defines: Iterable[str] = (),
        include_loader: IncludeLoader | None = None,
        on_get_unit_syntax: GetUnitSyntaxHook | None = None,
        on_unit_parsed: UnitParsedHook | None = None,
        source_transform: SourceTransform | None = None,
    ) -> None:
        self.search_paths = [Path(path) for path in search_paths]
        self.include_paths = [Path(path) for path in include_paths]
        self.defines = list(defines)
        self.include_loader = include_loader
        self.on_get_unit_syntax = on_get_unit_syntax
        self.on_unit_parsed = on_unit_parsed
        self.source_transform = source_transform

        self._parsed_units: dict[str, UnitInfo] = {}
        self._problems: list[ProjectProblem] = []
        self._not_found_units: set[str] = set()
        self._include_files: dict[str, IncludeFileInfo] = {}
        self._project_folder: Path = Path('.')
        self._aborting: bool = False

    def index(self, file_name: str) -> ProjectIndexResult:
        self._parsed_units = {}
        self._problems = []
        self._not_found_units = set()
        self._include_files = {}
        self._aborting = False

        entry = Path(file_name).expanduser().resolve()
        self._project_folder = entry.parent

        is_project = entry.suffix.casefold() in {'.dpr', '.dpk'}
        self._parse_unit(entry.stem, entry, is_project=is_project)

        parsed_units = sorted(self._parsed_units.values(), key=lambda item: item.name.casefold())
        include_files = sorted(self._include_files.values(), key=lambda item: (item.name.casefold(), item.path.casefold()))
        problems = list(self._problems)
        not_found = sorted(self._not_found_units, key=str.casefold)

        return ProjectIndexResult(
            parsed_units=parsed_units,
            include_files=include_files,
            problems=problems,
            not_found_units=not_found,
        )

    def _parse_unit(self, unit_name: str, file_path: Path, *, is_project: bool) -> None:
        if self._aborting:
            return

        normalized_name = unit_name.casefold()
        if normalized_name in self._parsed_units:
            return

        hook_tree: Optional[SyntaxNode] = None
        do_parse_unit = True
        if self.on_get_unit_syntax is not None:
            hook_tree, do_parse_unit, do_abort = self.on_get_unit_syntax(str(file_path))
            if do_abort:
                self._aborting = True
                return

        syntax_tree: Optional[SyntaxNode] = hook_tree
        from_parser = False

        if syntax_tree is None and do_parse_unit:
            source = self._read_file(file_path)
            if source is None:
                return
            parser = DelphiParser(
                include_paths=[str(path) for path in self.include_paths],
                defines=self.defines,
                include_loader=self._build_include_loader(),
            )
            try:
                if self.source_transform is not None:
                    source = self.source_transform(source)
                result = parser.parse(source, str(file_path), build_semantic=False)
                syntax_tree = result.root
                from_parser = True
            except Exception as exc:  # pragma: no cover - parser error branch
                self._problems.append(
                    ProjectProblem(
                        problem_type=ProjectProblemType.CANT_PARSE_FILE,
                        file_name=str(file_path),
                        description=str(exc),
                    )
                )
                self._parsed_units[normalized_name] = UnitInfo(
                    name=unit_name,
                    path=str(file_path),
                    syntax_tree=None,
                    has_error=True,
                    error_info=UnitErrorInfo(error=str(exc)),
                )
                return

        if syntax_tree is None:
            return

        actual_name = syntax_tree.get_attribute(AttributeName.anName) or unit_name
        normalized_name = actual_name.casefold()
        unit_info = UnitInfo(name=actual_name, path=str(file_path), syntax_tree=syntax_tree)
        self._parsed_units[normalized_name] = unit_info

        if self.on_unit_parsed is not None:
            do_abort = self.on_unit_parsed(actual_name, str(file_path), syntax_tree, from_parser)
            if do_abort:
                self._aborting = True
                return

        for dep_name, dep_path in self._collect_dependencies(syntax_tree, file_path, is_project=is_project):
            if self._aborting:
                return
            resolved = self._resolve_unit_path(dep_name, dep_path, relative_to=file_path.parent)
            if resolved is None:
                self._not_found_units.add(dep_name)
                self._problems.append(
                    ProjectProblem(
                        problem_type=ProjectProblemType.CANT_FIND_FILE,
                        file_name=dep_name,
                        description=f'Unit not found: {dep_name}',
                    )
                )
                continue
            self._parse_unit(dep_name, resolved, is_project=False)

    def _read_file(self, file_path: Path) -> Optional[str]:
        try:
            return read_source_text(file_path)
        except OSError as exc:
            self._problems.append(
                ProjectProblem(
                    problem_type=ProjectProblemType.CANT_OPEN_FILE,
                    file_name=str(file_path),
                    description=str(exc),
                )
            )
            return None

    def _collect_dependencies(
        self,
        syntax_tree: SyntaxNode,
        file_path: Path,
        *,
        is_project: bool,
    ) -> list[tuple[str, Optional[str]]]:
        deps: list[tuple[str, Optional[str]]] = []
        if is_project:
            self._append_units_from_node(syntax_tree, SyntaxNodeType.ntUses, deps)
            self._append_units_from_node(syntax_tree, SyntaxNodeType.ntContains, deps)
            return deps

        intf = syntax_tree.find_node(SyntaxNodeType.ntInterface)
        impl = syntax_tree.find_node(SyntaxNodeType.ntImplementation)
        if intf is not None:
            self._append_units_from_node(intf, SyntaxNodeType.ntUses, deps)
        if impl is not None:
            self._append_units_from_node(impl, SyntaxNodeType.ntUses, deps)
        return deps

    def _append_units_from_node(
        self,
        root: SyntaxNode,
        node_type: SyntaxNodeType,
        deps: list[tuple[str, Optional[str]]],
    ) -> None:
        target = root.find_node(node_type)
        if target is None:
            return
        for child in target.child_nodes:
            if child.typ != SyntaxNodeType.ntUnit:
                continue
            name = child.get_attribute(AttributeName.anName)
            if not name:
                continue
            dep_path = child.get_attribute(AttributeName.anPath) or None
            deps.append((name, dep_path))

    def _resolve_unit_path(self, unit_name: str, unit_path: Optional[str], *, relative_to: Path) -> Optional[Path]:
        candidates: list[Path] = []

        if unit_path:
            hint = Path(unit_path)
            if hint.is_absolute():
                candidates.append(hint)
            else:
                candidates.append((relative_to / hint).resolve())

        unit_file_names: list[str]
        if Path(unit_name).suffix:
            unit_file_names = [unit_name]
        else:
            unit_file_names = [f'{unit_name}.pas', f'{unit_name}.dpr', f'{unit_name}.dpk']

        search_roots = [relative_to, self._project_folder, *self.search_paths, *self.include_paths]
        for root in search_roots:
            for file_name in unit_file_names:
                candidates.append((root / file_name).resolve())

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _build_include_loader(self) -> IncludeLoader:
        base_loader = self.include_loader or self._default_include_loader

        def wrapped(parent_file: str, include_name: str) -> Optional[tuple[str, str]]:
            resolved = base_loader(parent_file, include_name)
            if resolved is None:
                return None
            content, resolved_path = resolved
            cache_key = f'{include_name.casefold()}::{resolved_path.casefold()}'
            self._include_files[cache_key] = IncludeFileInfo(name=include_name, path=resolved_path)
            return content, resolved_path

        return wrapped

    def _default_include_loader(self, parent_file: str, include_name: str) -> Optional[tuple[str, str]]:
        parent = Path(parent_file).resolve().parent
        include_path = Path(include_name.replace('\\', '/'))
        search_roots = [parent, self._project_folder, *self.include_paths, *self.search_paths]
        for root in search_roots:
            candidate = (root / include_path).resolve()
            if candidate.exists() and candidate.is_file():
                try:
                    content = read_source_text(candidate)
                except OSError:
                    continue
                return content, str(candidate)
        return None


__all__ = [
    'ProjectIndexer',
    'ProjectIndexResult',
    'ProjectProblemType',
    'ProjectProblem',
    'UnitInfo',
    'UnitErrorInfo',
    'IncludeFileInfo',
    'GetUnitSyntaxHook',
    'UnitParsedHook',
    'SourceTransform',
]
