from __future__ import annotations

import json


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
