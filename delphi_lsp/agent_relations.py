from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import re
import unicodedata

from .agent_protocol import AgentProtocolError
from .agent_workspace import AgentWorkspace
from .consts import AttributeName, SyntaxNodeType
from .nodes import SyntaxNode
from .parser import DelphiParser
from .project_indexer import ProjectIndexer, ProjectProblem
from .semantic import (
    GenericInstanceTypeRef,
    NamedTypeRef,
    ProcTypeRef,
    ReferenceKind,
    Scope,
    ScopeKind,
    SourceRange,
    Symbol,
    SymbolIndex,
    SymbolKind,
    SymbolReference,
    TypeRef,
)
from .source_reader import read_source_text
from .workspace import WorkspaceSemanticResult, build_workspace_semantics_from_roots


_ROUTINE_KINDS = frozenset(
    {
        SymbolKind.METHOD.value,
        SymbolKind.FUNCTION.value,
        SymbolKind.PROCEDURE.value,
        SymbolKind.CONSTRUCTOR.value,
        SymbolKind.DESTRUCTOR.value,
    }
)
_TYPE_KINDS = frozenset(
    {
        SymbolKind.CLASS.value,
        SymbolKind.RECORD.value,
        SymbolKind.INTERFACE.value,
    }
)
_GroupKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class RelationTarget:
    target_id: str
    source_path: str
    path: str
    unit_id: str
    unit_name: str
    name: str
    qualified_name: str
    kind: str
    signature: str
    line: int
    column: int
    card: Mapping[str, object]


@dataclass(frozen=True)
class _ReferenceRecord:
    target_group: _GroupKey
    owner_group: _GroupKey | None
    kind: ReferenceKind
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class _TypeEdge:
    source_group: _GroupKey
    target_group: _GroupKey
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class _UnitEdge:
    source_group: _GroupKey
    target_group: _GroupKey
    evidence: Mapping[str, object]


class ProjectRelationIndex:
    """Lazy deep semantic graph for one selected project revision."""

    def __init__(
        self,
        workspace: AgentWorkspace,
        project_id: str,
        revision: str,
        targets: Sequence[RelationTarget],
    ) -> None:
        self.project_id = project_id
        self.revision = revision
        self._root = workspace.root
        self._targets = {target.target_id: target for target in targets}
        self._target_groups = {
            target.target_id: _target_group(target)
            for target in targets
        }
        self._groups: dict[_GroupKey, list[RelationTarget]] = {}
        for target in targets:
            self._groups.setdefault(_target_group(target), []).append(target)
        for group in self._groups.values():
            group.sort(key=_target_sort_key)

        self._path_display: dict[str, str] = {}
        self._exact_targets: dict[tuple[str, int, int, str, str], list[RelationTarget]] = {}
        self._position_targets: dict[tuple[str, int, int, str], list[RelationTarget]] = {}
        self._line_targets: dict[tuple[str, int, str, str], list[RelationTarget]] = {}
        self._identity_targets: dict[tuple[str, str, str], list[RelationTarget]] = {}
        for target in targets:
            path_key = _path_key(target.source_path)
            self._path_display.setdefault(path_key, target.path)
            exact = (path_key, target.line, target.column, target.kind, _normalized(target.name))
            self._exact_targets.setdefault(exact, []).append(target)
            position = (path_key, target.line, target.column, target.kind)
            self._position_targets.setdefault(position, []).append(target)
            line = (path_key, target.line, target.kind, _normalized(target.name))
            self._line_targets.setdefault(line, []).append(target)
            identity = (
                _normalized(target.qualified_name),
                target.kind,
                _target_signature_identity(target),
            )
            self._identity_targets.setdefault(identity, []).append(target)

        roots, load_problems = _load_project_roots(workspace)
        self._roots = roots
        self._problems = list(load_problems)
        self._semantics = build_workspace_semantics_from_roots(roots) if roots else None
        self._symbol_targets: dict[int, RelationTarget | None] = {}
        self._references: list[_ReferenceRecord] = []
        self._unit_edges: list[_UnitEdge] = []
        self._inherits: list[_TypeEdge] = []
        self._implements: list[_TypeEdge] = []
        self._unresolved_references = 0
        self._ambiguous_references = 0
        if self._semantics is not None:
            self._build_graph(self._semantics)
            self._add_contains_edges(self._semantics)
            self._add_semantic_problems(self._semantics)
        self._problems = _dedupe_mappings(self._problems)

    def trace(self, target_id: str, relation: str) -> list[dict[str, object]]:
        target = self._targets.get(target_id)
        if target is None:
            raise AgentProtocolError("target_not_found", f"Target not found: {target_id}.")
        self._validate_applicability(target, relation)
        group = self._target_groups[target_id]

        if relation == "references":
            items = [
                self._relation_item(relation, record.owner_group or record.target_group, record.evidence)
                for record in self._references
                if record.target_group == group
            ]
        elif relation == "callers":
            items = [
                self._relation_item(relation, record.owner_group, record.evidence)
                for record in self._references
                if record.kind == ReferenceKind.CALL
                and record.target_group == group
                and record.owner_group is not None
            ]
        elif relation == "callees":
            items = [
                self._relation_item(relation, record.target_group, record.evidence)
                for record in self._references
                if record.kind == ReferenceKind.CALL and record.owner_group == group
            ]
        elif relation == "uses":
            items = [
                self._relation_item(relation, edge.target_group, edge.evidence)
                for edge in self._unit_edges
                if edge.source_group == group
            ]
        elif relation == "used_by":
            items = [
                self._relation_item(relation, edge.source_group, edge.evidence)
                for edge in self._unit_edges
                if edge.target_group == group
            ]
        elif relation == "inherits":
            items = [
                self._relation_item(relation, edge.target_group, edge.evidence)
                for edge in self._inherits
                if edge.source_group == group
            ]
        elif relation == "implements":
            items = [
                self._relation_item(relation, edge.target_group, edge.evidence)
                for edge in self._implements
                if edge.source_group == group
            ]
        else:  # AgentRequest validation normally rejects this first.
            raise AgentProtocolError("invalid_relation", f"Unsupported relation value: {relation!r}.")

        compact_items = [item for item in items if item is not None]
        relations = _dedupe_mappings(compact_items)
        relations.sort(key=_relation_sort_key)
        result = [self._metadata(relation)]
        result.extend(relations)
        result.extend(self._problems)
        return result

    def _build_graph(self, semantics: WorkspaceSemanticResult) -> None:
        for model in semantics.models.values():
            for reference in model.references:
                if reference.resolved is None:
                    self._unresolved_references += 1
                    continue
                candidate_groups = self._resolved_candidate_groups(reference.resolved)
                if len(candidate_groups) != 1:
                    if len(candidate_groups) > 1:
                        self._ambiguous_references += 1
                    else:
                        self._unresolved_references += 1
                    continue
                target_group = next(iter(candidate_groups))
                owner = self._scope_target(
                    reference.scope,
                    call_owner=reference.kind == ReferenceKind.CALL,
                )
                owner_group = _target_group(owner) if owner is not None else None
                evidence = self._evidence(reference.ref_range, reference.kind.value)
                record = _ReferenceRecord(
                    target_group=target_group,
                    owner_group=owner_group,
                    kind=reference.kind,
                    evidence=evidence,
                )
                self._references.append(record)
                if reference.kind == ReferenceKind.UNIT and owner_group is not None:
                    self._unit_edges.append(
                        _UnitEdge(
                            source_group=owner_group,
                            target_group=target_group,
                            evidence=evidence,
                        )
                    )

        seen_symbols: set[int] = set()
        for model in semantics.models.values():
            for symbol in _symbols_in_scope(model.unit_scope):
                if id(symbol) in seen_symbols or not symbol.base_types:
                    continue
                seen_symbols.add(id(symbol))
                source_target = self._target_for_symbol(symbol)
                if source_target is None:
                    continue
                source_group = _target_group(source_target)
                for base_ref in symbol.base_types:
                    base_symbol = _resolve_type_symbol(base_ref, symbol.scope, semantics.index)
                    base_target = self._target_for_symbol(base_symbol) if base_symbol is not None else None
                    if base_target is None:
                        self._unresolved_references += 1
                        continue
                    target_group = _target_group(base_target)
                    evidence = self._evidence(symbol.decl_range, "base_type")
                    relation = _classify_type_relation(symbol.kind, base_symbol.kind)
                    if relation == "inherits":
                        self._inherits.append(_TypeEdge(source_group, target_group, evidence))
                    elif relation == "implements":
                        self._implements.append(_TypeEdge(source_group, target_group, evidence))

    def _add_contains_edges(self, semantics: WorkspaceSemanticResult) -> None:
        unit_targets = {
            _normalized(target.unit_name or target.name): target
            for target in self._targets.values()
            if target.kind == SymbolKind.UNIT.value
        }
        for file_name, root in self._roots.items():
            contains = root.find_node(SyntaxNodeType.ntContains)
            if contains is None:
                continue
            model = semantics.models.get(file_name)
            source_target = self._target_for_symbol(model.unit_scope.owner) if model is not None else None
            if source_target is None:
                continue
            source_group = _target_group(source_target)
            for child in contains.child_nodes:
                if child.typ != SyntaxNodeType.ntUnit:
                    continue
                name = child.get_attribute(AttributeName.anName)
                target = unit_targets.get(_normalized(name))
                if target is None:
                    self._unresolved_references += 1
                    continue
                evidence = self._evidence(
                    SourceRange(
                        file_name=child.file_name or file_name,
                        start_line=child.line,
                        start_col=child.col,
                        end_line=child.line,
                        end_col=child.col,
                    ),
                    "contains",
                )
                self._unit_edges.append(_UnitEdge(source_group, _target_group(target), evidence))

    def _add_semantic_problems(self, semantics: WorkspaceSemanticResult) -> None:
        for model in semantics.models.values():
            for problem in model.problems:
                self._problems.append(
                    {
                        "item_type": "relation_problem",
                        "kind": "semantic",
                        "message": problem.message,
                        "path": self._display_path(problem.range.file_name),
                        "line": problem.range.start_line,
                        "column": problem.range.start_col,
                    }
                )

    def _resolved_candidate_groups(self, symbol: Symbol) -> set[_GroupKey]:
        groups: set[_GroupKey] = set()
        for candidate in (symbol, *symbol.overloads):
            target = self._target_for_symbol(candidate)
            if target is not None:
                groups.add(_target_group(target))
        return groups

    def _target_for_symbol(self, symbol: Symbol | None) -> RelationTarget | None:
        if symbol is None:
            return None
        cached = self._symbol_targets.get(id(symbol), ...)
        if cached is not ...:
            return cached
        path_key = _path_key(symbol.decl_range.file_name)
        exact = (
            path_key,
            symbol.decl_range.start_line,
            symbol.decl_range.start_col,
            symbol.kind.value,
            _normalized(symbol.name),
        )
        candidates = list(self._exact_targets.get(exact, ()))
        if not candidates:
            position = (
                path_key,
                symbol.decl_range.start_line,
                symbol.decl_range.start_col,
                symbol.kind.value,
            )
            candidates = list(self._position_targets.get(position, ()))
        if not candidates:
            line = (
                path_key,
                symbol.decl_range.start_line,
                symbol.kind.value,
                _normalized(symbol.name),
            )
            candidates = list(self._line_targets.get(line, ()))
        if not candidates:
            qualified_name = _semantic_qualified_name(symbol)
            candidates = list(
                self._identity_targets.get(
                    (
                        _normalized(qualified_name),
                        symbol.kind.value,
                        _symbol_signature_identity(symbol),
                    ),
                    (),
                )
            )
        target = min(candidates, key=_target_sort_key) if candidates else None
        self._symbol_targets[id(symbol)] = target
        return target

    def _scope_target(self, scope: Scope, *, call_owner: bool = False) -> RelationTarget | None:
        current: Scope | None = scope
        while current is not None:
            if current.kind == ScopeKind.ROUTINE:
                target = self._target_for_symbol(current.owner)
                if target is not None:
                    return target
                if call_owner:
                    return None
            elif current.kind == ScopeKind.UNIT:
                if call_owner:
                    return None
                target = self._target_for_symbol(current.owner)
                if target is not None:
                    return target
            current = current.parent
        return None

    def _relation_item(
        self,
        relation: str,
        group: _GroupKey | None,
        evidence: Mapping[str, object],
    ) -> dict[str, object] | None:
        if group is None:
            return None
        candidates = self._groups.get(group)
        if not candidates:
            return None
        target = candidates[0]
        return {
            "item_type": "relation",
            "relation": relation,
            **dict(target.card),
            "evidence": dict(evidence),
        }

    def _evidence(self, source_range: SourceRange, kind: str) -> dict[str, object]:
        return {
            "path": self._display_path(source_range.file_name),
            "line": source_range.start_line,
            "column": source_range.start_col,
            "kind": kind,
        }

    def _display_path(self, file_name: str) -> str:
        key = _path_key(file_name)
        known = self._path_display.get(key)
        if known is not None:
            return known
        path = Path(file_name).expanduser()
        if path.is_absolute():
            try:
                return path.resolve().relative_to(self._root).as_posix()
            except ValueError:
                return f"@external/relation/{_stable_component(path.name)}"
        return file_name.replace("\\", "/")

    def _metadata(self, relation: str) -> dict[str, object]:
        limitations = {
            "references": ["Only resolved semantic references are reported; text matches are excluded."],
            "callers": [
                "CALL edges cover explicit parsed calls only.",
                "Bare parameterless, overload, virtual, and procedural dispatch can be incomplete.",
            ],
            "callees": [
                "CALL edges cover explicit parsed calls only.",
                "Bare parameterless, overload, virtual, and procedural dispatch can be incomplete.",
            ],
            "uses": ["Includes resolved uses clauses and project/package contains clauses."],
            "used_by": ["Includes resolved uses clauses and project/package contains clauses."],
            "inherits": ["Reports resolved class-to-class and interface-to-interface base types."],
            "implements": [
                "Reports class/record base types resolved to interfaces; property delegation is not modeled."
            ],
        }
        return {
            "item_type": "relation_metadata",
            "relation": relation,
            "completeness": "sound_partial",
            "unresolved_references": self._unresolved_references,
            "ambiguous_references": self._ambiguous_references,
            "deep_problem_count": len(self._problems),
            "limitations": limitations[relation],
        }

    @staticmethod
    def _validate_applicability(target: RelationTarget, relation: str) -> None:
        applicable = True
        if relation in {"callers", "callees"}:
            applicable = target.kind in _ROUTINE_KINDS
        elif relation in {"uses", "used_by"}:
            applicable = target.kind == SymbolKind.UNIT.value
        elif relation == "inherits":
            applicable = target.kind in {SymbolKind.CLASS.value, SymbolKind.INTERFACE.value}
        elif relation == "implements":
            applicable = target.kind in {SymbolKind.CLASS.value, SymbolKind.RECORD.value}
        if not applicable:
            raise AgentProtocolError(
                "relation_not_applicable",
                f"Relation {relation!r} does not apply to {target.kind} target {target.target_id}.",
            )


def _load_project_roots(
    workspace: AgentWorkspace,
) -> tuple[dict[str, SyntaxNode], list[dict[str, object]]]:
    active_project = workspace.active_project
    if active_project is None:
        raise AgentProtocolError("project_required", "Select a project before tracing relations.")
    if active_project.kind != "workspace":
        project_path = Path(active_project.path)
        if not project_path.is_absolute():
            project_path = workspace.root / project_path
        indexer = ProjectIndexer(
            search_paths=workspace.search_paths,
            include_paths=workspace.include_paths,
            defines=workspace.defines,
        )
        result = indexer.index(str(project_path.resolve()))
        roots = {
            str(Path(unit.path).expanduser().resolve()): unit.syntax_tree
            for unit in result.parsed_units
            if unit.syntax_tree is not None
        }
        problems = [_project_problem_item(problem, workspace.root) for problem in result.problems]
        return roots, problems

    parser = DelphiParser(include_paths=workspace.include_paths, defines=workspace.defines)
    roots: dict[str, SyntaxNode] = {}
    problems: list[dict[str, object]] = []
    for unit in workspace.units:
        source_path = Path(unit.path)
        if not source_path.is_absolute():
            source_path = workspace.root / source_path
        source_path = source_path.expanduser().resolve()
        try:
            text = read_source_text(source_path)
            roots[str(source_path)] = parser.parse(
                text,
                str(source_path),
                build_semantic=False,
            ).root
        except Exception as exc:
            problems.append(
                {
                    "item_type": "relation_problem",
                    "kind": "cant_parse_file",
                    "message": _safe_problem_message(
                        f"{type(exc).__name__}: {exc}",
                        workspace.root,
                        (
                            source_path,
                            getattr(exc, "filename", None),
                            getattr(exc, "filename2", None),
                        ),
                    ),
                    "path": _display_project_path(source_path, workspace.root),
                }
            )
    return roots, problems


def _project_problem_item(problem: ProjectProblem, root: Path) -> dict[str, object]:
    return {
        "item_type": "relation_problem",
        "kind": problem.problem_type.value,
        "message": _safe_problem_message(problem.description, root, (problem.file_name,)),
        "path": _display_project_path(problem.file_name, root),
    }


def _safe_problem_message(
    message: str,
    root: Path,
    known_paths: Iterable[str | PurePath | None],
) -> str:
    replacements: dict[str, str] = {}
    for value in known_paths:
        if value is None:
            continue
        original = str(value)
        pattern = _separator_agnostic_absolute_path_pattern(original)
        if pattern is None:
            continue
        safe_path = _display_project_path(original, root)
        replacements[pattern] = safe_path
    for pattern in sorted(replacements, key=len, reverse=True):
        replacement = replacements[pattern]
        message = re.sub(pattern, lambda _match: replacement, message)
    return message


def _display_project_path(path: str | PurePath, root: Path) -> str:
    path_text = str(path)
    if not _is_cross_platform_absolute(path_text):
        return Path(path_text).as_posix()
    host_path = Path(path_text)
    if host_path.is_absolute():
        resolved = host_path.expanduser().resolve()
        relative = _portable_relative_to_root(resolved, root)
        if relative is not None:
            return relative
        return f"@external/relation/{_stable_component(resolved.name)}"
    relative = _portable_relative_to_root(path_text, root)
    if relative is not None:
        return relative
    return f"@external/relation/{_stable_component(_portable_name(path_text))}"


def _is_cross_platform_absolute(path: str | PurePath) -> bool:
    text = str(path)
    return PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()


def _separator_agnostic_absolute_path_pattern(path: str | PurePath) -> str | None:
    text = str(path)
    windows_path = PureWindowsPath(text)
    if windows_path.is_absolute():
        canonical = windows_path.as_posix()
        case_insensitive = True
    else:
        posix_path = PurePosixPath(text)
        if not posix_path.is_absolute():
            return None
        canonical = posix_path.as_posix()
        case_insensitive = False
    pattern = r"[\\/]+".join(re.escape(component) for component in canonical.split("/"))
    return f"(?i:{pattern})" if case_insensitive else pattern


def _portable_relative_to_root(path: str | PurePath, root: Path) -> str | None:
    text = str(path)
    posix_path = PurePosixPath(text)
    posix_root = PurePosixPath(str(root))
    if posix_path.is_absolute() and posix_root.is_absolute():
        try:
            return posix_path.relative_to(posix_root).as_posix()
        except ValueError:
            pass

    windows_path = PureWindowsPath(text)
    windows_root = PureWindowsPath(str(root))
    if windows_path.is_absolute() and windows_root.is_absolute():
        try:
            return windows_path.relative_to(windows_root).as_posix()
        except ValueError:
            pass
    return None


def _portable_name(path: str | PurePath) -> str:
    return PurePosixPath(str(path).replace("\\", "/")).name or "unknown"


def _resolve_type_symbol(type_ref: TypeRef, scope: Scope, index: SymbolIndex) -> Symbol | None:
    if isinstance(type_ref, GenericInstanceTypeRef):
        return _resolve_type_symbol(type_ref.base, scope, index)
    if not isinstance(type_ref, NamedTypeRef):
        return None
    if type_ref.unit_name:
        unit_scope = index.lookup_unit(type_ref.unit_name)
        if unit_scope is not None:
            symbols = unit_scope.lookup_local(type_ref.name)
            return symbols[0] if symbols else None
    if "." in type_ref.name:
        symbols = index.resolve_qualified(type_ref.name)
        if symbols:
            return symbols[0]
    symbols = scope.resolve(type_ref.name)
    return symbols[0] if symbols else None


def _classify_type_relation(source_kind: SymbolKind, target_kind: SymbolKind) -> str | None:
    if (
        source_kind == SymbolKind.CLASS
        and target_kind == SymbolKind.CLASS
    ) or (
        source_kind == SymbolKind.INTERFACE
        and target_kind == SymbolKind.INTERFACE
    ):
        return "inherits"
    if source_kind in {SymbolKind.CLASS, SymbolKind.RECORD} and target_kind == SymbolKind.INTERFACE:
        return "implements"
    return None


def _symbols_in_scope(scope: Scope) -> Iterable[Symbol]:
    seen_scopes: set[int] = set()

    def visit(current: Scope) -> Iterable[Symbol]:
        if id(current) in seen_scopes:
            return
        seen_scopes.add(id(current))
        for symbols in current.symbols.values():
            for symbol in symbols:
                yield symbol
                if symbol.member_scope is not None:
                    yield from visit(symbol.member_scope)

    yield from visit(scope)


def _semantic_qualified_name(symbol: Symbol) -> str:
    scope = symbol.scope
    unit_scope = scope
    while unit_scope.parent is not None:
        unit_scope = unit_scope.parent
    unit_name = unit_scope.name
    if symbol.kind == SymbolKind.UNIT:
        return unit_name
    if scope.kind == ScopeKind.TYPE and scope.owner is not None:
        return f"{unit_name}.{scope.owner.name}.{symbol.name}"
    return f"{unit_name}.{symbol.name}"


def _target_group(target: RelationTarget) -> _GroupKey:
    return (
        _path_key(target.source_path),
        _normalized(target.qualified_name),
        target.kind,
        _target_signature_identity(target),
    )


def _target_signature_identity(target: RelationTarget) -> str:
    if target.kind not in _ROUTINE_KINDS:
        return ""
    return _canonical_signature_identity(target.signature)


def _symbol_signature_identity(symbol: Symbol) -> str:
    if symbol.kind.value not in _ROUTINE_KINDS or not isinstance(symbol.type_ref, ProcTypeRef):
        return ""
    parameter_symbols = []
    if symbol.member_scope is not None:
        parameter_symbols = sorted(
            (
                candidate
                for symbols in symbol.member_scope.symbols.values()
                for candidate in symbols
                if candidate.kind == SymbolKind.PARAMETER
            ),
            key=lambda candidate: (
                candidate.decl_range.start_line,
                candidate.decl_range.start_col,
                candidate.name.casefold(),
            ),
        )
    normalized_params: list[str] = []
    for index, param in enumerate(symbol.type_ref.params):
        modifier = ""
        if index < len(parameter_symbols):
            modifier = _normalized(parameter_symbols[index].attributes.get("modifier", ""))
        type_name = _normalized(param.display_name()).replace(" ", "")
        normalized_params.append(f"{modifier}:{type_name}")
    params = ",".join(normalized_params)
    signature = f"({params})"
    if symbol.type_ref.return_type is not None:
        result = _normalized(symbol.type_ref.return_type.display_name()).replace(" ", "")
        signature = f"{signature}:{result}"
    calling_convention = _normalized(symbol.attributes.get("callingconvention", ""))
    if calling_convention:
        signature = f"{signature}|cc:{calling_convention}"
    return _canonical_signature_identity(signature)


def _canonical_signature_identity(signature: str) -> str:
    def split_parameters(content: str) -> list[str]:
        parameters: list[str] = []
        start = 0
        depth = 0
        for index, char in enumerate(content):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char in ";," and depth == 0:
                parameters.append(content[start:index])
                start = index + 1
        parameters.append(content[start:])
        return parameters

    def canonicalize(content: str) -> str:
        parts: list[str] = []
        index = 0
        while index < len(content):
            if content[index] != "(":
                parts.append(content[index])
                index += 1
                continue
            depth = 1
            end = index + 1
            while end < len(content) and depth:
                if content[end] == "(":
                    depth += 1
                elif content[end] == ")":
                    depth -= 1
                end += 1
            if depth:
                parts.append(content[index:])
                break
            parameters = split_parameters(content[index + 1 : end - 1])
            expanded: list[str] = []
            for parameter in parameters:
                match = re.fullmatch(r"\s*((?:var|const|out)\s*)?#(\d+):(.*)", parameter, re.DOTALL)
                if match is None:
                    expanded.append(canonicalize(parameter))
                    continue
                modifier, count, parameter_type = match.groups()
                expanded.extend(
                    f"{modifier or ''}:{canonicalize(parameter_type)}"
                    for _ in range(int(count))
                )
            parts.append(f"({','.join(expanded)})")
            index = end
        return "".join(parts)

    return _normalized(canonicalize(signature).replace(" ", ""))


def _target_sort_key(target: RelationTarget) -> tuple[object, ...]:
    return (
        target.path.casefold(),
        target.line,
        target.column,
        target.qualified_name.casefold(),
        target.signature.casefold(),
        target.target_id,
    )


def _relation_sort_key(item: Mapping[str, object]) -> tuple[object, ...]:
    evidence = item.get("evidence")
    evidence_mapping = evidence if isinstance(evidence, Mapping) else {}
    return (
        str(item.get("qualified_name", "")).casefold(),
        str(item.get("path", "")).casefold(),
        int(item.get("line", 0)),
        int(item.get("column", 0)),
        str(evidence_mapping.get("path", "")).casefold(),
        int(evidence_mapping.get("line", 0)),
        int(evidence_mapping.get("column", 0)),
        str(evidence_mapping.get("kind", "")),
        str(item.get("target_id", "")),
    )


def _dedupe_mappings(items: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in items:
        mapping = dict(item)
        key = repr(_freeze(mapping))
        if key in seen:
            continue
        seen.add(key)
        result.append(mapping)
    return result


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _path_key(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _stable_component(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\\", "_").replace("/", "_") or "unknown"


__all__ = ["ProjectRelationIndex", "RelationTarget"]
