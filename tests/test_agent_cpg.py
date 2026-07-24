from __future__ import annotations

import json

from delphi_lsp.parser import DelphiParser
from delphi_lsp.consts import AttributeName, SyntaxNodeType
from delphi_lsp.nodes import SyntaxNode


CPG_SOURCE = """unit UnitA;
interface
type
  TThing = class
    procedure Run;
  end;
implementation
procedure TThing.Run;
var
  Value: Integer;
begin
  Value := 1;
  if Value > 0 then
    Notify(Value)
  else
    Value := 2;
end;
end.
"""


def test_cpg_nodes_and_edges_have_stable_sanitized_identifiers() -> None:
    from delphi_lsp.agent_cpg import CpgEdge, CpgNode

    properties = {"path": "src/UnitA.pas", "line": 10, "column": 3}
    first = CpgNode.create(
        label="ROUTINE",
        identity=("src/UnitA.pas", "TThing.Run", 10, 3),
        properties=properties,
    )
    second = CpgNode.create(
        label="ROUTINE",
        identity=("SRC\\unita.PAS", "tthing.run", 10, 3),
        properties=properties,
    )
    edge = CpgEdge.create(
        label="CONTAINS",
        source=first.node_id,
        target="cpg_node_child",
    )

    assert first.node_id == second.node_id
    assert first.node_id.startswith("cpg_node_")
    assert edge.edge_id.startswith("cpg_edge_")
    assert "UnitA" not in first.node_id
    assert first.to_mapping() == {
        "item_type": "cpg_node",
        "node_id": first.node_id,
        "label": "ROUTINE",
        "properties": properties,
    }
    assert edge.to_mapping()["item_type"] == "cpg_edge"


def test_cpg_subgraph_deduplicates_orders_and_accounts_without_source_text() -> None:
    from delphi_lsp.agent_cpg import CpgEdge, CpgNode, CpgProblem, CpgSubgraph

    root = CpgNode.create(
        label="ROUTINE",
        identity=("UnitA.pas", "Run"),
        properties={"path": "UnitA.pas", "line": 4, "column": 1},
    )
    child = CpgNode.create(
        label="STATEMENT",
        identity=("UnitA.pas", "Run", 0),
        properties={"path": "UnitA.pas", "line": 6, "column": 3},
    )
    edge = CpgEdge.create(label="AST", source=root.node_id, target=child.node_id)
    problem = CpgProblem(
        kind="parse_partial",
        message="Partial syntax was retained.",
        path="UnitA.pas",
        line=8,
        column=2,
    )

    graph = CpgSubgraph.create(
        target_id="target_v2_run",
        graph="full",
        root_node_id=root.node_id,
        nodes=(child, root, child),
        edges=(edge, edge),
        problems=(problem, problem),
        unresolved=2,
    )

    assert graph.nodes == tuple(sorted((root, child), key=lambda item: item.node_id))
    assert graph.edges == (edge,)
    assert graph.problems == (problem,)
    assert graph.retained_bytes > 0
    items = graph.to_items()
    assert items[0]["item_type"] == "cpg_metadata"
    assert items[0]["completeness"] == "sound_partial"
    assert items[0]["unresolved"] == 2
    assert [item["item_type"] for item in items[1:]] == [
        "cpg_node",
        "cpg_node",
        "cpg_edge",
        "cpg_problem",
    ]
    assert "routine source must not be retained" not in json.dumps(items)


def test_cpg_subgraph_filters_by_graph_direction_and_depth() -> None:
    from delphi_lsp.agent_cpg import CpgEdge, CpgNode, CpgSubgraph

    nodes = tuple(
        CpgNode.create(
            label="STATEMENT" if index else "ROUTINE",
            identity=("UnitA.pas", index),
            properties={"path": "UnitA.pas", "line": index + 1, "column": 1},
        )
        for index in range(4)
    )
    graph = CpgSubgraph.create(
        target_id="target_v2_run",
        graph="full",
        root_node_id=nodes[0].node_id,
        nodes=nodes,
        edges=(
            CpgEdge.create(label="AST", source=nodes[0].node_id, target=nodes[1].node_id),
            CpgEdge.create(label="CFG", source=nodes[1].node_id, target=nodes[2].node_id),
            CpgEdge.create(label="CALL", source=nodes[2].node_id, target=nodes[3].node_id),
        ),
    )

    ast = graph.select(graph="ast", direction="out", depth=1)
    cfg = graph.select(
        graph="cfg",
        direction="out",
        depth=2,
        root_node_id=nodes[1].node_id,
    )
    incoming = graph.select(graph="call", direction="in", depth=1, root_node_id=nodes[3].node_id)

    assert {edge.label for edge in ast.edges} == {"AST"}
    assert {node.node_id for node in ast.nodes} == {nodes[0].node_id, nodes[1].node_id}
    assert {edge.label for edge in cfg.edges} == {"CFG"}
    assert {node.node_id for node in cfg.nodes} == {nodes[1].node_id, nodes[2].node_id}
    assert {edge.label for edge in incoming.edges} == {"CALL"}
    assert {node.node_id for node in incoming.nodes} == {nodes[2].node_id, nodes[3].node_id}


def test_cpg_subgraph_enforces_deterministic_record_limit() -> None:
    from delphi_lsp.agent_cpg import CpgNode, CpgSubgraph

    nodes = tuple(
        CpgNode.create(
            label="STATEMENT",
            identity=("UnitA.pas", index),
            properties={"path": "UnitA.pas", "line": index + 1, "column": 1},
        )
        for index in range(8)
    )

    graph = CpgSubgraph.create(
        target_id="target_v2_run",
        graph="ast",
        root_node_id=nodes[0].node_id,
        nodes=nodes,
        record_limit=3,
    )

    assert len(graph.nodes) == 3
    assert graph.truncated is True


def test_builds_target_local_ast_without_retaining_source_text() -> None:
    from delphi_lsp.agent_cpg import CpgTarget
    from delphi_lsp.agent_cpg_builder import build_cpg_subgraph

    root = DelphiParser().parse(
        CPG_SOURCE,
        "UnitA.pas",
        build_semantic=False,
    ).root
    target = CpgTarget(
        target_id="target_v2_run",
        source_path="UnitA.pas",
        path="UnitA.pas",
        unit_id="target_v2_unit",
        name="Run",
        qualified_name="TThing.Run",
        kind="method",
        line=8,
        column=1,
    )

    graph = build_cpg_subgraph(
        target=target,
        syntax_root=root,
        candidates={},
        graph="ast",
        direction="out",
        depth=16,
    )

    labels = {node.label for node in graph.nodes}
    assert {"ROUTINE", "STATEMENT", "CONTROL", "CALL", "IDENTIFIER"} <= labels
    assert {edge.label for edge in graph.edges} == {"AST"}
    assert graph.root_node_id in {node.node_id for node in graph.nodes}
    assert all(
        dict(node.properties).get("path") == "UnitA.pas"
        for node in graph.nodes
    )
    serialized = json.dumps(graph.to_items())
    assert "Notify(Value)" not in serialized
    assert "Value := 1" not in serialized


def test_target_local_ast_is_deterministic_and_unit_scoped() -> None:
    from delphi_lsp.agent_cpg import CpgTarget
    from delphi_lsp.agent_cpg_builder import build_cpg_subgraph

    root = DelphiParser().parse(
        CPG_SOURCE,
        "UnitA.pas",
        build_semantic=False,
    ).root
    unit = CpgTarget(
        target_id="target_v2_unit",
        source_path="UnitA.pas",
        path="UnitA.pas",
        unit_id="target_v2_unit",
        name="UnitA",
        qualified_name="UnitA",
        kind="unit",
        line=1,
        column=1,
    )

    first = build_cpg_subgraph(
        target=unit,
        syntax_root=root,
        candidates={},
        graph="ast",
        direction="out",
        depth=16,
    )
    second = build_cpg_subgraph(
        target=unit,
        syntax_root=root,
        candidates={},
        graph="ast",
        direction="out",
        depth=16,
    )

    assert first == second
    assert len(first.nodes) > 10


def test_routine_cfg_contains_entry_exit_and_conditional_branches() -> None:
    from delphi_lsp.agent_cpg import CpgTarget
    from delphi_lsp.agent_cpg_builder import build_cpg_subgraph

    root = DelphiParser().parse(
        CPG_SOURCE,
        "UnitA.pas",
        build_semantic=False,
    ).root
    target = CpgTarget(
        target_id="target_v2_run",
        source_path="UnitA.pas",
        path="UnitA.pas",
        unit_id="target_v2_unit",
        name="Run",
        qualified_name="TThing.Run",
        kind="method",
        line=8,
        column=1,
    )

    graph = build_cpg_subgraph(
        target=target,
        syntax_root=root,
        candidates={},
        graph="cfg",
        direction="out",
        depth=16,
    )

    labels = {node.label for node in graph.nodes}
    assert {"ROUTINE", "ENTRY", "EXIT", "CONTROL", "STATEMENT", "CALL"} <= labels
    assert graph.edges
    assert {edge.label for edge in graph.edges} == {"CFG"}
    node_ids = {node.node_id for node in graph.nodes}
    assert all(
        edge.source in node_ids and edge.target in node_ids
        for edge in graph.edges
    )
    control_ids = {
        node.node_id
        for node in graph.nodes
        if node.label == "CONTROL"
        and dict(node.properties).get("syntax_type") == "ntIf"
    }
    assert control_ids
    outgoing = [
        edge
        for edge in graph.edges
        if edge.source in control_ids
    ]
    assert len(outgoing) >= 2


def test_loop_cfg_has_a_back_edge_and_a_fallthrough() -> None:
    from delphi_lsp.agent_cpg import CpgTarget
    from delphi_lsp.agent_cpg_builder import build_cpg_subgraph

    root = SyntaxNode(SyntaxNodeType.ntUnit)
    root.set_attribute(AttributeName.anName, "Flow")
    method = root.add_child(SyntaxNodeType.ntMethod)
    method.set_attribute(AttributeName.anName, "Run")
    method.line = 2
    statements = method.add_child(SyntaxNodeType.ntStatements)
    loop = statements.add_child(SyntaxNodeType.ntWhile)
    loop.line = 4
    condition = loop.add_child(SyntaxNodeType.ntIdentifier)
    condition.set_attribute(AttributeName.anName, "Ready")
    body = loop.add_child(SyntaxNodeType.ntAssign)
    body.line = 5
    after = statements.add_child(SyntaxNodeType.ntAssign)
    after.line = 6
    target = CpgTarget(
        target_id="target_v2_run",
        source_path="Flow.pas",
        path="Flow.pas",
        unit_id="target_v2_unit",
        name="Run",
        qualified_name="Run",
        kind="procedure",
        line=2,
        column=1,
    )

    graph = build_cpg_subgraph(
        target=target,
        syntax_root=root,
        candidates={},
        graph="cfg",
        direction="out",
        depth=16,
    )

    edge_kinds = {
        dict(edge.properties).get("kind")
        for edge in graph.edges
    }
    assert "loop_back" in edge_kinds
    assert "false" in edge_kinds
