from __future__ import annotations

from collections.abc import Mapping, Sequence

from .agent_cpg import CpgEdge, CpgNode, CpgProblem, CpgSubgraph, CpgTarget
from .consts import AttributeName, SyntaxNodeType
from .nodes import CompoundSyntaxNode, SyntaxNode


_ROUTINE_KINDS = frozenset(
    {"method", "function", "procedure", "constructor", "destructor"}
)
_TYPE_KINDS = frozenset({"type", "class", "record", "interface", "enum"})
_CONTROL_TYPES = frozenset(
    {
        SyntaxNodeType.ntCase,
        SyntaxNodeType.ntCaseElse,
        SyntaxNodeType.ntExcept,
        SyntaxNodeType.ntExceptionHandler,
        SyntaxNodeType.ntFinally,
        SyntaxNodeType.ntFor,
        SyntaxNodeType.ntGoto,
        SyntaxNodeType.ntIf,
        SyntaxNodeType.ntRaise,
        SyntaxNodeType.ntRepeat,
        SyntaxNodeType.ntTry,
        SyntaxNodeType.ntWhile,
        SyntaxNodeType.ntWith,
    }
)
_STATEMENT_TYPES = frozenset(
    {
        SyntaxNodeType.ntAssign,
        SyntaxNodeType.ntEmptyStatement,
        SyntaxNodeType.ntStatement,
        SyntaxNodeType.ntStatements,
    }
)
_SYMBOL_TYPES = frozenset(
    {
        SyntaxNodeType.ntConstant,
        SyntaxNodeType.ntField,
        SyntaxNodeType.ntParameter,
        SyntaxNodeType.ntProperty,
        SyntaxNodeType.ntTypeDecl,
        SyntaxNodeType.ntVariable,
    }
)


def build_cpg_subgraph(
    *,
    target: CpgTarget,
    syntax_root: SyntaxNode,
    candidates: Mapping[str, Sequence[CpgTarget]],
    graph: str,
    direction: str,
    depth: int,
    record_limit: int = 50_000,
) -> CpgSubgraph:
    del candidates  # Call resolution is added by the call-graph builder.
    selected = _select_target_syntax(target, syntax_root)
    root_node = _target_node(target)
    nodes = [root_node]
    edges: list[CpgEdge] = []
    problems: list[CpgProblem] = []
    if selected is None:
        problems.append(
            CpgProblem(
                kind="target_syntax_missing",
                message="Target syntax could not be located.",
                path=target.path,
                line=target.line,
                column=target.column,
            )
        )
    else:
        stack: list[tuple[SyntaxNode, tuple[int, ...], str]] = [
            (child, (index,), root_node.node_id)
            for index, child in reversed(tuple(enumerate(selected.child_nodes)))
        ]
        while stack and len(nodes) + len(edges) < record_limit:
            current, syntax_path, parent_id = stack.pop()
            node = _syntax_node(target, current, syntax_path)
            nodes.append(node)
            edges.append(
                CpgEdge.create(
                    label="AST",
                    source=parent_id,
                    target=node.node_id,
                    identity=syntax_path,
                    properties={"order": syntax_path[-1]},
                )
            )
            for index, child in reversed(tuple(enumerate(current.child_nodes))):
                stack.append((child, (*syntax_path, index), node.node_id))
        if stack:
            problems.append(
                CpgProblem(
                    kind="record_limit",
                    message="CPG record limit was reached.",
                    path=target.path,
                    line=target.line,
                    column=target.column,
                )
            )
    complete = CpgSubgraph.create(
        target_id=target.target_id,
        graph="full",
        root_node_id=root_node.node_id,
        nodes=nodes,
        edges=edges,
        problems=problems,
        truncated=bool(stack) if selected is not None else False,
        record_limit=record_limit,
    )
    return complete.select(
        graph=graph,
        direction=direction,
        depth=depth,
    )


def _target_node(target: CpgTarget) -> CpgNode:
    label = (
        "UNIT"
        if target.kind == "unit"
        else "ROUTINE"
        if target.kind in _ROUTINE_KINDS
        else "TYPE"
        if target.kind in _TYPE_KINDS
        else "SYMBOL"
    )
    properties: dict[str, object] = {
        "target_id": target.target_id,
        "unit_id": target.unit_id,
        "name": target.name,
        "qualified_name": target.qualified_name,
        "kind": target.kind,
        "path": target.path,
        "line": target.line,
        "column": target.column,
        "visibility": target.visibility,
    }
    if target.end_line:
        properties["end_line"] = target.end_line
        properties["end_column"] = target.end_column
    if target.type_name:
        properties["type"] = target.type_name
    if target.owner:
        properties["owner"] = target.owner
    return CpgNode.create(
        label=label,
        identity=("target", target.target_id),
        properties=properties,
    )


def _select_target_syntax(
    target: CpgTarget,
    root: SyntaxNode,
) -> SyntaxNode | None:
    if target.kind == "unit":
        return root
    candidates: list[tuple[int, int, SyntaxNode]] = []
    stack = [root]
    normalized_name = target.name.casefold()
    normalized_qualified = target.qualified_name.casefold()
    while stack:
        current = stack.pop()
        stack.extend(reversed(current.child_nodes))
        if target.kind in _ROUTINE_KINDS and current.typ != SyntaxNodeType.ntMethod:
            continue
        if target.kind in _TYPE_KINDS and current.typ != SyntaxNodeType.ntTypeDecl:
            continue
        name = current.get_attribute(AttributeName.anName).casefold()
        if not name:
            continue
        name_matches = (
            name == normalized_name
            or name == normalized_qualified
            or name.rsplit(".", 1)[-1] == normalized_name
        )
        if not name_matches:
            continue
        candidates.append(
            (
                abs(max(1, current.line) - max(1, target.line)),
                -len(current.child_nodes),
                current,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2].line, item[2].col))
    return candidates[0][2]


def _syntax_node(
    target: CpgTarget,
    syntax: SyntaxNode,
    syntax_path: tuple[int, ...],
) -> CpgNode:
    properties: dict[str, object] = {
        "path": target.path,
        "line": max(1, syntax.line),
        "column": max(1, syntax.col),
        "syntax_type": syntax.typ.name,
    }
    if isinstance(syntax, CompoundSyntaxNode) and syntax.end_line > 0:
        properties["end_line"] = syntax.end_line
        properties["end_column"] = syntax.end_col
    name = syntax.get_attribute(AttributeName.anName)
    if name:
        properties["name"] = name
    return CpgNode.create(
        label=_syntax_label(syntax.typ),
        identity=(
            target.path,
            target.target_id,
            syntax_path,
            syntax.typ.name,
            syntax.line,
            syntax.col,
        ),
        properties=properties,
    )


def _syntax_label(typ: SyntaxNodeType) -> str:
    if typ in _CONTROL_TYPES:
        return "CONTROL"
    if typ == SyntaxNodeType.ntCall:
        return "CALL"
    if typ in {SyntaxNodeType.ntIdentifier, SyntaxNodeType.ntName}:
        return "IDENTIFIER"
    if typ == SyntaxNodeType.ntLiteral:
        return "LITERAL"
    if typ in _STATEMENT_TYPES:
        return "STATEMENT"
    if typ in _SYMBOL_TYPES:
        return "SYMBOL"
    return "EXPRESSION"


__all__ = ["build_cpg_subgraph"]
