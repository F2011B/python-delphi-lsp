from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
from typing import Iterable

from .consts import AttributeName, SyntaxNodeType
from .lark_tokens import KEYWORDS
from .nodes import SyntaxNode
from .parser import DelphiParser
from .semantic import Scope, SymbolKind


_KEYWORDS = frozenset(keyword.casefold() for keyword in KEYWORDS)
_TWO_CHARACTER_OPERATORS = frozenset({":=", "<=", ">=", "<>", "..", "**"})
_DECISION_NODES = frozenset(
    {
        SyntaxNodeType.ntIf,
        SyntaxNodeType.ntFor,
        SyntaxNodeType.ntWhile,
        SyntaxNodeType.ntRepeat,
        SyntaxNodeType.ntCaseSelector,
        SyntaxNodeType.ntExceptionHandler,
    }
)
_DEPENDENCY_NODES = frozenset(
    {
        SyntaxNodeType.ntUses,
        SyntaxNodeType.ntContains,
        SyntaxNodeType.ntRequires,
    }
)


@dataclass(frozen=True)
class MetricProblem:
    kind: str
    path: str
    message: str

    def to_mapping(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class LineMetrics:
    total_lines: int = 0
    source_lines: int = 0
    blank_lines: int = 0
    comment_only_lines: int = 0
    comment_lines: int = 0
    directive_lines: int = 0

    def to_mapping(self) -> dict[str, int]:
        return {
            "total_lines": self.total_lines,
            "source_lines": self.source_lines,
            "blank_lines": self.blank_lines,
            "comment_only_lines": self.comment_only_lines,
            "comment_lines": self.comment_lines,
            "directive_lines": self.directive_lines,
        }


@dataclass(frozen=True)
class HalsteadMetrics:
    distinct_operators: int = 0
    distinct_operands: int = 0
    total_operators: int = 0
    total_operands: int = 0
    vocabulary: int = 0
    length: int = 0
    calculated_length: float = 0.0
    volume: float = 0.0
    difficulty: float = 0.0
    effort: float = 0.0
    estimated_time_seconds: float = 0.0
    estimated_defects: float = 0.0

    def to_mapping(self) -> dict[str, int | float]:
        return {
            "distinct_operators": self.distinct_operators,
            "distinct_operands": self.distinct_operands,
            "total_operators": self.total_operators,
            "total_operands": self.total_operands,
            "vocabulary": self.vocabulary,
            "length": self.length,
            "calculated_length": self.calculated_length,
            "volume": self.volume,
            "difficulty": self.difficulty,
            "effort": self.effort,
            "estimated_time_seconds": self.estimated_time_seconds,
            "estimated_defects": self.estimated_defects,
        }


@dataclass(frozen=True)
class RoutineComplexity:
    name: str
    value: int
    line: int

    def to_mapping(self) -> dict[str, str | int]:
        return {"name": self.name, "value": self.value, "line": self.line}


@dataclass(frozen=True)
class CyclomaticMetrics:
    routine_count: int = 0
    total: int = 0
    average: float = 0.0
    maximum: int = 0
    routines: tuple[RoutineComplexity, ...] = ()

    @classmethod
    def from_routines(cls, routines: Iterable[RoutineComplexity]) -> CyclomaticMetrics:
        ordered = tuple(
            sorted(
                routines,
                key=lambda item: (-item.value, item.name.casefold(), item.line, item.name),
            )
        )
        total = sum(item.value for item in ordered)
        count = len(ordered)
        return cls(
            routine_count=count,
            total=total,
            average=total / count if count else 0.0,
            maximum=max((item.value for item in ordered), default=0),
            routines=ordered,
        )

    def to_mapping(self, *, detail: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "routine_count": self.routine_count,
            "total": self.total,
            "average": self.average,
            "maximum": self.maximum,
        }
        if detail:
            result["routines"] = [item.to_mapping() for item in self.routines]
        return result


@dataclass(frozen=True)
class UnitMetrics:
    name: str
    path: str
    lines: LineMetrics
    cyclomatic: CyclomaticMetrics
    halstead: HalsteadMetrics
    maintainability_index: float
    symbol_counts: Mapping[str, int]
    dependencies: tuple[str, ...] = ()
    internal_dependencies: tuple[str, ...] = ()
    external_dependencies: tuple[str, ...] = ()
    afferent_coupling: int = 0
    efferent_coupling: int = 0
    instability: float = 0.0
    class_like_types: int = 0
    abstract_types: int = 0
    abstractness: float = 0.0
    distance: float = 1.0
    problems: tuple[MetricProblem, ...] = ()
    unit_id: str = ""
    _operator_vocabulary: frozenset[str] = field(default_factory=frozenset, repr=False)
    _operand_vocabulary: frozenset[str] = field(default_factory=frozenset, repr=False)

    def to_mapping(self, *, detail: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "path": self.path,
            "unit_id": self.unit_id,
            "lines": self.lines.to_mapping(),
            "cyclomatic": self.cyclomatic.to_mapping(detail=detail),
            "maintainability_index": self.maintainability_index,
            "afferent_coupling": self.afferent_coupling,
            "efferent_coupling": self.efferent_coupling,
            "instability": self.instability,
            "abstractness": self.abstractness,
            "distance": self.distance,
            "class_like_types": self.class_like_types,
            "abstract_types": self.abstract_types,
            "problems": [problem.to_mapping() for problem in self.problems],
        }
        if detail:
            result.update(
                {
                    "halstead": self.halstead.to_mapping(),
                    "symbol_counts": dict(sorted(self.symbol_counts.items())),
                    "dependencies": list(self.dependencies),
                    "internal_dependencies": list(self.internal_dependencies),
                    "external_dependencies": list(self.external_dependencies),
                }
            )
        else:
            result["halstead_volume"] = self.halstead.volume
        return result


@dataclass(frozen=True)
class ProjectMetrics:
    units: tuple[UnitMetrics, ...]
    lines: LineMetrics
    cyclomatic: CyclomaticMetrics
    halstead: HalsteadMetrics
    maintainability_index: float
    total_loc: int
    include_loc: int
    total_loc_with_includes: int
    dependency_edges: int
    problems: tuple[MetricProblem, ...] = ()
    project_id: str = ""
    project_name: str = ""

    @property
    def unit_count(self) -> int:
        return len(self.units)

    def unit_by_name(self, name: str) -> UnitMetrics:
        normalized = name.casefold()
        for unit in self.units:
            if unit.name.casefold() == normalized:
                return unit
        raise KeyError(name)

    def to_mapping(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "unit_count": self.unit_count,
            "total_loc": self.total_loc,
            "include_loc": self.include_loc,
            "total_loc_with_includes": self.total_loc_with_includes,
            "lines": self.lines.to_mapping(),
            "cyclomatic": self.cyclomatic.to_mapping(),
            "halstead": self.halstead.to_mapping(),
            "maintainability_index": self.maintainability_index,
            "dependency_edges": self.dependency_edges,
            "problems": [problem.to_mapping() for problem in self.problems],
        }


@dataclass(frozen=True)
class _ScanResult:
    lines: LineMetrics
    operators: tuple[str, ...]
    operands: tuple[str, ...]


def analyze_unit(
    source: str,
    path: str,
    *,
    defines: Iterable[str] = (),
    include_paths: Iterable[str] = (),
) -> UnitMetrics:
    scan = _scan_source(source)
    problems: list[MetricProblem] = []
    root: SyntaxNode | None = None
    semantic = None
    try:
        parsed = DelphiParser(
            defines=tuple(defines),
            include_paths=tuple(include_paths),
        ).parse(source, path, build_semantic=True)
        root = parsed.root
        semantic = parsed.semantic
    except Exception as error:
        problems.append(
            MetricProblem(
                kind="parse_error",
                path=path,
                message=f"Could not parse source ({type(error).__name__}).",
            )
        )

    name = Path(path).stem
    dependencies: tuple[str, ...] = ()
    cyclomatic = CyclomaticMetrics()
    symbol_counts: Counter[str] = Counter()
    class_like_types = 0
    abstract_types = 0
    if root is not None:
        name = root.get_attribute(AttributeName.anName) or name
        dependencies = _dependencies(root)
        cyclomatic = _cyclomatic_metrics(root)
        class_like_types, abstract_types = _abstract_type_counts(root)
    if semantic is not None:
        symbol_counts = _symbol_counts(semantic.unit_scope)

    halstead = _halstead_metrics(
        operator_vocabulary=frozenset(scan.operators),
        operand_vocabulary=frozenset(scan.operands),
        total_operators=len(scan.operators),
        total_operands=len(scan.operands),
    )
    abstractness = abstract_types / class_like_types if class_like_types else 0.0
    maintainability = _maintainability_index(
        volume=halstead.volume,
        complexity=cyclomatic.total,
        source_lines=scan.lines.source_lines,
    )
    return UnitMetrics(
        name=name,
        path=path,
        lines=scan.lines,
        cyclomatic=cyclomatic,
        halstead=halstead,
        maintainability_index=maintainability,
        symbol_counts=dict(sorted(symbol_counts.items())),
        dependencies=dependencies,
        class_like_types=class_like_types,
        abstract_types=abstract_types,
        abstractness=abstractness,
        distance=abs(abstractness - 1.0),
        problems=tuple(problems),
        _operator_vocabulary=frozenset(scan.operators),
        _operand_vocabulary=frozenset(scan.operands),
    )


def analyze_project(
    sources: Mapping[str, str],
    *,
    include_sources: Mapping[str, str] | None = None,
    defines: Iterable[str] = (),
    include_paths: Iterable[str] = (),
    project_id: str = "",
    project_name: str = "",
    unit_ids: Mapping[str, str] | None = None,
) -> ProjectMetrics:
    unique_sources = _unique_sources(sources)
    units = [
        analyze_unit(source, path, defines=defines, include_paths=include_paths)
        for path, source in unique_sources.items()
    ]
    units.sort(key=lambda item: (item.name.casefold(), item.path.casefold(), item.name, item.path))

    canonical_units: dict[str, UnitMetrics] = {}
    for unit in units:
        canonical_units.setdefault(unit.name.casefold(), unit)

    reverse_edges: dict[str, set[str]] = {name: set() for name in canonical_units}
    dependencies_by_unit: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    dependency_edges = 0
    for unit in units:
        internal: dict[str, str] = {}
        external: dict[str, str] = {}
        for dependency in unit.dependencies:
            normalized_dependency = dependency.casefold()
            if normalized_dependency == unit.name.casefold():
                continue
            if normalized_dependency in canonical_units:
                canonical_name = canonical_units[normalized_dependency].name
                internal.setdefault(normalized_dependency, canonical_name)
                reverse_edges[normalized_dependency].add(unit.name.casefold())
            else:
                external.setdefault(normalized_dependency, dependency)
        internal_values = tuple(sorted(internal.values(), key=lambda value: (value.casefold(), value)))
        external_values = tuple(sorted(external.values(), key=lambda value: (value.casefold(), value)))
        dependencies_by_unit[unit.path] = (internal_values, external_values)
        dependency_edges += len(internal_values) + len(external_values)

    enriched: list[UnitMetrics] = []
    id_by_path = {path.casefold(): value for path, value in (unit_ids or {}).items()}
    for unit in units:
        internal, external = dependencies_by_unit[unit.path]
        afferent = len(reverse_edges.get(unit.name.casefold(), ()))
        efferent = len(internal) + len(external)
        instability = efferent / (afferent + efferent) if afferent + efferent else 0.0
        enriched.append(
            replace(
                unit,
                internal_dependencies=internal,
                external_dependencies=external,
                afferent_coupling=afferent,
                efferent_coupling=efferent,
                instability=instability,
                distance=abs(unit.abstractness + instability - 1.0),
                unit_id=id_by_path.get(unit.path.casefold(), unit.unit_id),
            )
        )

    line_metrics = _sum_line_metrics(unit.lines for unit in enriched)
    routines = [routine for unit in enriched for routine in unit.cyclomatic.routines]
    cyclomatic = CyclomaticMetrics.from_routines(routines)
    operator_vocabulary = frozenset().union(*(unit._operator_vocabulary for unit in enriched))
    operand_vocabulary = frozenset().union(*(unit._operand_vocabulary for unit in enriched))
    halstead = _halstead_metrics(
        operator_vocabulary=operator_vocabulary,
        operand_vocabulary=operand_vocabulary,
        total_operators=sum(unit.halstead.total_operators for unit in enriched),
        total_operands=sum(unit.halstead.total_operands for unit in enriched),
    )
    maintainability = _maintainability_index(
        volume=halstead.volume,
        complexity=cyclomatic.total,
        source_lines=line_metrics.source_lines,
    )
    unique_includes = _unique_sources(include_sources or {})
    include_loc = sum(_scan_source(source).lines.total_lines for source in unique_includes.values())
    problems = tuple(problem for unit in enriched for problem in unit.problems)
    return ProjectMetrics(
        units=tuple(enriched),
        lines=line_metrics,
        cyclomatic=cyclomatic,
        halstead=halstead,
        maintainability_index=maintainability,
        total_loc=line_metrics.total_lines,
        include_loc=include_loc,
        total_loc_with_includes=line_metrics.total_lines + include_loc,
        dependency_edges=dependency_edges,
        problems=problems,
        project_id=project_id,
        project_name=project_name,
    )


def _unique_sources(sources: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    for path, source in sorted(sources.items(), key=lambda item: (item[0].casefold(), item[0])):
        normalized = str(Path(path)).replace("\\", "/").casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        result[path] = source
    return result


def _scan_source(source: str) -> _ScanResult:
    line_count = len(source.splitlines()) if source else 0
    line_starts = [0]
    line_starts.extend(index + 1 for index, character in enumerate(source) if character == "\n")
    has_code = [False] * line_count
    has_comment = [False] * line_count
    has_directive = [False] * line_count
    operators: list[str] = []
    operands: list[str] = []

    def mark(flags: list[bool], start: int, end: int) -> None:
        if not flags or end <= start:
            return
        start_line = min(bisect_right(line_starts, start) - 1, len(flags) - 1)
        end_line = min(bisect_right(line_starts, end - 1) - 1, len(flags) - 1)
        for line in range(max(start_line, 0), end_line + 1):
            flags[line] = True

    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            end = length if newline < 0 else newline
            mark(has_comment, index, end)
            index = end
            continue
        if character == "{":
            close = source.find("}", index + 1)
            end = length if close < 0 else close + 1
            directive = index + 1 < length and source[index + 1] == "$"
            mark(has_directive if directive else has_comment, index, end)
            index = end
            continue
        if source.startswith("(*", index):
            close = source.find("*)", index + 2)
            end = length if close < 0 else close + 2
            directive = index + 2 < length and source[index + 2] == "$"
            mark(has_directive if directive else has_comment, index, end)
            index = end
            continue
        if character == "'":
            end = _quoted_end(source, index)
            mark(has_code, index, end)
            operands.append(source[index:end])
            index = end
            continue
        if character == "&" and index + 1 < length and _identifier_start(source[index + 1]):
            end = index + 2
            while end < length and _identifier_part(source[end]):
                end += 1
            mark(has_code, index, end)
            operands.append(source[index + 1 : end].casefold())
            index = end
            continue
        if _identifier_start(character):
            end = index + 1
            while end < length and _identifier_part(source[end]):
                end += 1
            value = source[index:end].casefold()
            mark(has_code, index, end)
            if value in _KEYWORDS:
                operators.append(value)
            else:
                operands.append(value)
            index = end
            continue
        if character.isdigit() or (
            character in {"$", "%", "#"}
            and index + 1 < length
            and source[index + 1].isalnum()
        ):
            end = _number_end(source, index)
            mark(has_code, index, end)
            operands.append(source[index:end].casefold())
            index = end
            continue
        pair = source[index : index + 2]
        end = index + 2 if pair in _TWO_CHARACTER_OPERATORS else index + 1
        value = source[index:end]
        mark(has_code, index, end)
        operators.append(value.casefold())
        index = end

    source_lines = 0
    blank_lines = 0
    comment_only_lines = 0
    for line in range(line_count):
        if has_code[line]:
            source_lines += 1
        elif has_directive[line]:
            continue
        elif has_comment[line]:
            comment_only_lines += 1
        else:
            blank_lines += 1
    lines = LineMetrics(
        total_lines=line_count,
        source_lines=source_lines,
        blank_lines=blank_lines,
        comment_only_lines=comment_only_lines,
        comment_lines=sum(has_comment),
        directive_lines=sum(has_directive),
    )
    return _ScanResult(lines=lines, operators=tuple(operators), operands=tuple(operands))


def _quoted_end(source: str, start: int) -> int:
    index = start + 1
    while index < len(source):
        if source[index] != "'":
            index += 1
            continue
        if index + 1 < len(source) and source[index + 1] == "'":
            index += 2
            continue
        return index + 1
    return len(source)


def _number_end(source: str, start: int) -> int:
    index = start + 1
    while index < len(source):
        character = source[index]
        if character.isalnum() or character in {"_", "."}:
            if source.startswith("..", index):
                break
            index += 1
            continue
        if character in {"+", "-"} and index > start and source[index - 1].casefold() == "e":
            index += 1
            continue
        break
    return index


def _identifier_start(character: str) -> bool:
    return character == "_" or character.isalpha()


def _identifier_part(character: str) -> bool:
    return character == "_" or character.isalnum()


def _walk(node: SyntaxNode) -> Iterable[SyntaxNode]:
    yield node
    for child in node.child_nodes:
        yield from _walk(child)


def _dependencies(root: SyntaxNode) -> tuple[str, ...]:
    dependencies: dict[str, str] = {}
    for node in _walk(root):
        if node.typ not in _DEPENDENCY_NODES:
            continue
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntUnit:
                continue
            name = child.get_attribute(AttributeName.anName).strip()
            if name:
                dependencies.setdefault(name.casefold(), name)
    return tuple(sorted(dependencies.values(), key=lambda value: (value.casefold(), value)))


def _cyclomatic_metrics(root: SyntaxNode) -> CyclomaticMetrics:
    routines: list[RoutineComplexity] = []
    for node in _walk(root):
        if node.typ != SyntaxNodeType.ntMethod:
            continue
        if not any(child.typ == SyntaxNodeType.ntStatements for child in node.child_nodes):
            continue
        name = node.get_attribute(AttributeName.anName) or f"anonymous@{node.line}"
        routines.append(
            RoutineComplexity(
                name=name,
                value=1 + _decision_count(node),
                line=node.line,
            )
        )
    return CyclomaticMetrics.from_routines(routines)


def _decision_count(node: SyntaxNode) -> int:
    count = 0
    for child in node.child_nodes:
        if child.typ == SyntaxNodeType.ntMethod:
            continue
        if child.typ in _DECISION_NODES:
            count += 1
        if child.typ == SyntaxNodeType.ntExcept and not any(
            descendant.typ == SyntaxNodeType.ntExceptionHandler
            for descendant in child.child_nodes
        ):
            count += 1
        count += _decision_count(child)
    return count


def _symbol_counts(scope: Scope) -> Counter[str]:
    counts: Counter[str] = Counter()
    seen_scopes: set[int] = set()

    def visit(current: Scope) -> None:
        if id(current) in seen_scopes:
            return
        seen_scopes.add(id(current))
        for symbols in current.symbols.values():
            for symbol in symbols:
                if symbol.kind != SymbolKind.UNIT:
                    counts[symbol.kind.value] += 1
                if symbol.member_scope is not None:
                    visit(symbol.member_scope)

    visit(scope)
    return counts


def _abstract_type_counts(root: SyntaxNode) -> tuple[int, int]:
    class_like = 0
    abstract = 0
    for node in _walk(root):
        if node.typ != SyntaxNodeType.ntTypeDecl:
            continue
        type_node = next(
            (child for child in node.child_nodes if child.typ == SyntaxNodeType.ntType),
            None,
        )
        if type_node is None:
            continue
        kind = type_node.get_attribute(AttributeName.anType).casefold()
        if kind not in {"class", "interface", "dispinterface"}:
            continue
        class_like += 1
        has_abstract_method = any(
            child.typ == SyntaxNodeType.ntMethod
            and child.get_attribute(AttributeName.anAbstract) == "true"
            for child in _walk(type_node)
        )
        if kind in {"interface", "dispinterface"} or (
            type_node.get_attribute(AttributeName.anAbstract) == "true"
            or has_abstract_method
        ):
            abstract += 1
    return class_like, abstract


def _halstead_metrics(
    *,
    operator_vocabulary: frozenset[str],
    operand_vocabulary: frozenset[str],
    total_operators: int,
    total_operands: int,
) -> HalsteadMetrics:
    distinct_operators = len(operator_vocabulary)
    distinct_operands = len(operand_vocabulary)
    vocabulary = distinct_operators + distinct_operands
    length = total_operators + total_operands
    calculated_length = _halstead_term(distinct_operators) + _halstead_term(distinct_operands)
    volume = length * math.log2(vocabulary) if vocabulary > 1 else 0.0
    difficulty = (
        (distinct_operators / 2.0) * (total_operands / distinct_operands)
        if distinct_operands
        else 0.0
    )
    effort = difficulty * volume
    return HalsteadMetrics(
        distinct_operators=distinct_operators,
        distinct_operands=distinct_operands,
        total_operators=total_operators,
        total_operands=total_operands,
        vocabulary=vocabulary,
        length=length,
        calculated_length=calculated_length,
        volume=volume,
        difficulty=difficulty,
        effort=effort,
        estimated_time_seconds=effort / 18.0,
        estimated_defects=volume / 3000.0,
    )


def _halstead_term(value: int) -> float:
    return value * math.log2(value) if value > 1 else 0.0


def _maintainability_index(*, volume: float, complexity: int, source_lines: int) -> float:
    if source_lines == 0:
        return 100.0
    raw = (
        171.0
        - 5.2 * math.log(max(volume, 1.0))
        - 0.23 * complexity
        - 16.2 * math.log(max(source_lines, 1))
    ) * 100.0 / 171.0
    return min(100.0, max(0.0, raw))


def _sum_line_metrics(metrics: Iterable[LineMetrics]) -> LineMetrics:
    items = tuple(metrics)
    return LineMetrics(
        total_lines=sum(item.total_lines for item in items),
        source_lines=sum(item.source_lines for item in items),
        blank_lines=sum(item.blank_lines for item in items),
        comment_only_lines=sum(item.comment_only_lines for item in items),
        comment_lines=sum(item.comment_lines for item in items),
        directive_lines=sum(item.directive_lines for item in items),
    )


__all__ = [
    "MetricProblem",
    "LineMetrics",
    "HalsteadMetrics",
    "RoutineComplexity",
    "CyclomaticMetrics",
    "UnitMetrics",
    "ProjectMetrics",
    "analyze_unit",
    "analyze_project",
]
