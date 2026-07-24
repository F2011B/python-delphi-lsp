from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

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
    syntax_nodes: dict[int, CpgNode] = {}
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
        syntax_nodes[id(selected)] = root_node
        stack: list[tuple[SyntaxNode, tuple[int, ...], str]] = [
            (child, (index,), root_node.node_id)
            for index, child in reversed(tuple(enumerate(selected.child_nodes)))
        ]
        while stack and len(nodes) + len(edges) < record_limit:
            current, syntax_path, parent_id = stack.pop()
            node = _syntax_node(target, current, syntax_path)
            syntax_nodes[id(current)] = node
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
        if target.kind in _ROUTINE_KINDS:
            cfg_nodes, cfg_edges = _build_cfg(
                target,
                selected,
                syntax_nodes,
            )
            nodes.extend(cfg_nodes)
            edges.extend(cfg_edges)
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


@dataclass(frozen=True, slots=True)
class _CfgExit:
    node_id: str
    kind: str = "next"


@dataclass(frozen=True, slots=True)
class _CfgFragment:
    entries: tuple[str, ...]
    exits: tuple[_CfgExit, ...]


def _build_cfg(
    target: CpgTarget,
    selected: SyntaxNode,
    syntax_nodes: Mapping[int, CpgNode],
) -> tuple[list[CpgNode], list[CpgEdge]]:
    entry = CpgNode.create(
        label="ENTRY",
        identity=(target.target_id, "entry"),
        properties={
            "path": target.path,
            "line": target.line,
            "column": target.column,
        },
    )
    exit_node = CpgNode.create(
        label="EXIT",
        identity=(target.target_id, "exit"),
        properties={
            "path": target.path,
            "line": target.end_line or target.line,
            "column": target.end_column or target.column,
        },
    )
    edges: list[CpgEdge] = []
    root_node = syntax_nodes[id(selected)]
    _cfg_edge(edges, root_node.node_id, entry.node_id, "entry")
    statements = _first_descendant(selected, SyntaxNodeType.ntStatements)
    if statements is None:
        _cfg_edge(edges, entry.node_id, exit_node.node_id, "empty")
        return [entry, exit_node], edges
    fragment = _cfg_fragment(statements, syntax_nodes, edges)
    if not fragment.entries:
        _cfg_edge(edges, entry.node_id, exit_node.node_id, "empty")
    else:
        for fragment_entry in fragment.entries:
            _cfg_edge(edges, entry.node_id, fragment_entry, "next")
        for pending in fragment.exits:
            _cfg_edge(edges, pending.node_id, exit_node.node_id, pending.kind)
    return [entry, exit_node], edges


def _cfg_fragment(
    syntax: SyntaxNode,
    syntax_nodes: Mapping[int, CpgNode],
    edges: list[CpgEdge],
) -> _CfgFragment:
    if syntax.typ == SyntaxNodeType.ntStatements:
        fragments = [
            _cfg_fragment(child, syntax_nodes, edges)
            for child in syntax.child_nodes
            if _is_executable(child)
        ]
        fragments = [fragment for fragment in fragments if fragment.entries]
        if not fragments:
            return _CfgFragment((), ())
        for left, right in zip(fragments, fragments[1:]):
            for pending in left.exits:
                for entry in right.entries:
                    _cfg_edge(edges, pending.node_id, entry, pending.kind)
        return _CfgFragment(fragments[0].entries, fragments[-1].exits)

    node = syntax_nodes.get(id(syntax))
    if node is None:
        return _CfgFragment((), ())

    if syntax.typ == SyntaxNodeType.ntIf:
        branches = _branch_nodes(syntax)
        if not branches:
            return _CfgFragment((node.node_id,), (_CfgExit(node.node_id, "false"),))
        then_fragment = _cfg_fragment(branches[0], syntax_nodes, edges)
        if then_fragment.entries:
            for entry in then_fragment.entries:
                _cfg_edge(edges, node.node_id, entry, "true")
        exits = list(then_fragment.exits)
        if len(branches) > 1:
            else_fragment = _cfg_fragment(branches[1], syntax_nodes, edges)
            for entry in else_fragment.entries:
                _cfg_edge(edges, node.node_id, entry, "false")
            exits.extend(else_fragment.exits)
        else:
            exits.append(_CfgExit(node.node_id, "false"))
        return _CfgFragment((node.node_id,), tuple(exits))

    if syntax.typ in {SyntaxNodeType.ntWhile, SyntaxNodeType.ntFor}:
        branches = _branch_nodes(syntax)
        body = branches[-1] if branches else None
        if body is not None:
            body_fragment = _cfg_fragment(body, syntax_nodes, edges)
            for entry in body_fragment.entries:
                _cfg_edge(edges, node.node_id, entry, "true")
            for pending in body_fragment.exits:
                _cfg_edge(edges, pending.node_id, node.node_id, "loop_back")
        return _CfgFragment(
            (node.node_id,),
            (_CfgExit(node.node_id, "false"),),
        )

    if syntax.typ == SyntaxNodeType.ntRepeat:
        branches = _branch_nodes(syntax)
        if not branches:
            return _CfgFragment((node.node_id,), (_CfgExit(node.node_id, "true"),))
        body_fragments = [
            _cfg_fragment(branch, syntax_nodes, edges)
            for branch in branches
        ]
        body_fragments = [fragment for fragment in body_fragments if fragment.entries]
        if not body_fragments:
            return _CfgFragment((node.node_id,), (_CfgExit(node.node_id, "true"),))
        for left, right in zip(body_fragments, body_fragments[1:]):
            for pending in left.exits:
                for entry in right.entries:
                    _cfg_edge(edges, pending.node_id, entry, pending.kind)
        for pending in body_fragments[-1].exits:
            _cfg_edge(edges, pending.node_id, node.node_id, "loop_test")
        for entry in body_fragments[0].entries:
            _cfg_edge(edges, node.node_id, entry, "loop_back")
        return _CfgFragment(
            body_fragments[0].entries,
            (_CfgExit(node.node_id, "true"),),
        )

    if syntax.typ in {
        SyntaxNodeType.ntCase,
        SyntaxNodeType.ntTry,
        SyntaxNodeType.ntExcept,
        SyntaxNodeType.ntFinally,
    }:
        branches = _branch_nodes(syntax)
        exits: list[_CfgExit] = []
        for branch in branches:
            fragment = _cfg_fragment(branch, syntax_nodes, edges)
            for entry in fragment.entries:
                _cfg_edge(edges, node.node_id, entry, "branch")
            exits.extend(fragment.exits)
        if not exits:
            exits.append(_CfgExit(node.node_id))
        return _CfgFragment((node.node_id,), tuple(exits))

    if syntax.typ in {SyntaxNodeType.ntRaise, SyntaxNodeType.ntGoto}:
        return _CfgFragment((node.node_id,), (_CfgExit(node.node_id, "terminal"),))

    return _CfgFragment((node.node_id,), (_CfgExit(node.node_id),))


def _branch_nodes(syntax: SyntaxNode) -> list[SyntaxNode]:
    result: list[SyntaxNode] = []
    for child in syntax.child_nodes:
        if child.typ == SyntaxNodeType.ntElse:
            result.extend(candidate for candidate in child.child_nodes if _is_executable(candidate))
        elif _is_executable(child):
            result.append(child)
    return result


def _is_executable(syntax: SyntaxNode) -> bool:
    return (
        syntax.typ in _STATEMENT_TYPES
        or syntax.typ in _CONTROL_TYPES
        or syntax.typ == SyntaxNodeType.ntCall
    )


def _first_descendant(
    root: SyntaxNode,
    typ: SyntaxNodeType,
) -> SyntaxNode | None:
    stack = list(reversed(root.child_nodes))
    while stack:
        current = stack.pop()
        if current.typ == typ:
            return current
        stack.extend(reversed(current.child_nodes))
    return None


def _cfg_edge(
    edges: list[CpgEdge],
    source: str,
    target: str,
    kind: str,
) -> None:
    edges.append(
        CpgEdge.create(
            label="CFG",
            source=source,
            target=target,
            properties={"kind": kind},
        )
    )


__all__ = ["build_cpg_subgraph"]
