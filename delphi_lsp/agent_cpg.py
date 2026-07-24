from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import posixpath
import sys
import unicodedata


_GRAPH_EDGE_LABELS = {
    "ast": frozenset({"AST", "CONTAINS"}),
    "cfg": frozenset({"CFG"}),
    "dfg": frozenset({"DEF", "USE", "REACHING_DEF"}),
    "call": frozenset({"CALL"}),
    "full": None,
}


@dataclass(frozen=True, slots=True)
class CpgTarget:
    target_id: str
    source_path: str
    path: str
    unit_id: str
    name: str
    qualified_name: str
    kind: str
    line: int
    column: int
    end_line: int = 0
    end_column: int = 0
    visibility: str = "unknown"
    type_name: str = ""
    owner: str = ""
    parent_target_id: str = ""


def _normalized_identity(value: object) -> object:
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
        if "/" in normalized:
            normalized = posixpath.normpath(normalized)
        return normalized.casefold()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_normalized_identity(item) for item in value]
    return value


def _stable_id(prefix: str, *identity: object) -> str:
    payload = json.dumps(
        [_normalized_identity(value) for value in identity],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()}"


def _frozen_properties(
    properties: Mapping[str, object] | None,
) -> tuple[tuple[str, object], ...]:
    if properties is None:
        return ()
    return tuple(sorted(((str(key), value) for key, value in properties.items())))


@dataclass(frozen=True, slots=True)
class CpgNode:
    node_id: str
    label: str
    properties: tuple[tuple[str, object], ...] = ()

    @classmethod
    def create(
        cls,
        *,
        label: str,
        identity: Sequence[object],
        properties: Mapping[str, object] | None = None,
    ) -> CpgNode:
        normalized_label = label.upper()
        return cls(
            _stable_id("cpg_node", normalized_label, tuple(identity)),
            normalized_label,
            _frozen_properties(properties),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "item_type": "cpg_node",
            "node_id": self.node_id,
            "label": self.label,
            "properties": dict(self.properties),
        }


@dataclass(frozen=True, slots=True)
class CpgEdge:
    edge_id: str
    label: str
    source: str
    target: str
    properties: tuple[tuple[str, object], ...] = ()

    @classmethod
    def create(
        cls,
        *,
        label: str,
        source: str,
        target: str,
        identity: Sequence[object] = (),
        properties: Mapping[str, object] | None = None,
    ) -> CpgEdge:
        normalized_label = label.upper()
        frozen = _frozen_properties(properties)
        return cls(
            _stable_id(
                "cpg_edge",
                normalized_label,
                source,
                target,
                tuple(identity),
                frozen,
            ),
            normalized_label,
            source,
            target,
            frozen,
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "item_type": "cpg_edge",
            "edge_id": self.edge_id,
            "label": self.label,
            "source": self.source,
            "target": self.target,
        }
        if self.properties:
            result["properties"] = dict(self.properties)
        return result


@dataclass(frozen=True, slots=True)
class CpgProblem:
    kind: str
    message: str
    path: str = ""
    line: int = 0
    column: int = 0

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "item_type": "cpg_problem",
            "kind": self.kind,
            "message": self.message,
        }
        if self.path:
            result["path"] = self.path
        if self.line > 0:
            result["line"] = self.line
        if self.column > 0:
            result["column"] = self.column
        return result


@dataclass(frozen=True, slots=True)
class CpgSubgraph:
    target_id: str
    graph: str
    root_node_id: str
    nodes: tuple[CpgNode, ...]
    edges: tuple[CpgEdge, ...]
    problems: tuple[CpgProblem, ...] = ()
    truncated: bool = False
    unresolved: int = 0

    @classmethod
    def create(
        cls,
        *,
        target_id: str,
        graph: str,
        root_node_id: str,
        nodes: Iterable[CpgNode] = (),
        edges: Iterable[CpgEdge] = (),
        problems: Iterable[CpgProblem] = (),
        truncated: bool = False,
        unresolved: int = 0,
        record_limit: int = 50_000,
    ) -> CpgSubgraph:
        unique_nodes = {node.node_id: node for node in nodes}
        ordered_nodes = sorted(unique_nodes.values(), key=lambda item: item.node_id)
        unique_edges = {edge.edge_id: edge for edge in edges}
        ordered_edges = sorted(unique_edges.values(), key=lambda item: item.edge_id)
        unique_problems = {
            (problem.kind, problem.message, problem.path, problem.line, problem.column): problem
            for problem in problems
        }
        ordered_problems = sorted(
            unique_problems.values(),
            key=lambda item: (
                item.path.casefold(),
                item.line,
                item.column,
                item.kind,
                item.message,
            ),
        )

        limit = max(1, record_limit)
        overflow = len(ordered_nodes) + len(ordered_edges) > limit
        if overflow:
            if len(ordered_nodes) > limit:
                selected_nodes = ordered_nodes[:limit]
                if root_node_id in unique_nodes and all(
                    node.node_id != root_node_id for node in selected_nodes
                ):
                    selected_nodes[-1] = unique_nodes[root_node_id]
                    selected_nodes.sort(key=lambda item: item.node_id)
                ordered_nodes = selected_nodes
                ordered_edges = []
            else:
                remaining = limit - len(ordered_nodes)
                ordered_edges = ordered_edges[:remaining]
        retained_ids = {node.node_id for node in ordered_nodes}
        ordered_edges = [
            edge
            for edge in ordered_edges
            if edge.source in retained_ids and edge.target in retained_ids
        ]
        return cls(
            target_id=target_id,
            graph=graph,
            root_node_id=root_node_id,
            nodes=tuple(ordered_nodes),
            edges=tuple(ordered_edges),
            problems=tuple(ordered_problems),
            truncated=truncated or overflow,
            unresolved=max(0, unresolved),
        )

    @property
    def retained_bytes(self) -> int:
        property_slots = sum(len(node.properties) for node in self.nodes) + sum(
            len(edge.properties) for edge in self.edges
        )
        text_bytes = sum(
            len(node.node_id) + len(node.label)
            for node in self.nodes
        ) + sum(
            len(edge.edge_id)
            + len(edge.label)
            + len(edge.source)
            + len(edge.target)
            for edge in self.edges
        )
        problem_bytes = sum(
            len(problem.kind) + len(problem.message) + len(problem.path)
            for problem in self.problems
        )
        return (
            sys.getsizeof(self)
            + len(self.nodes) * 256
            + len(self.edges) * 320
            + len(self.problems) * 256
            + property_slots * 96
            + text_bytes
            + problem_bytes
        )

    def to_items(self) -> list[dict[str, object]]:
        metadata: dict[str, object] = {
            "item_type": "cpg_metadata",
            "target_id": self.target_id,
            "graph": self.graph,
            "root_node_id": self.root_node_id,
            "completeness": "sound_partial",
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "unresolved": self.unresolved,
            "problem_count": len(self.problems),
            "truncated": self.truncated,
            "retained_bytes": self.retained_bytes,
        }
        return [
            metadata,
            *(node.to_mapping() for node in self.nodes),
            *(edge.to_mapping() for edge in self.edges),
            *(problem.to_mapping() for problem in self.problems),
        ]

    def select(
        self,
        *,
        graph: str,
        direction: str,
        depth: int,
        root_node_id: str | None = None,
    ) -> CpgSubgraph:
        allowed = _GRAPH_EDGE_LABELS[graph]
        candidate_edges = tuple(
            edge for edge in self.edges if allowed is None or edge.label in allowed
        )
        root = root_node_id or self.root_node_id
        reached = {root}
        frontier = deque([(root, 0)])
        selected_edges: dict[str, CpgEdge] = {}
        outgoing: dict[str, list[CpgEdge]] = {}
        incoming: dict[str, list[CpgEdge]] = {}
        for edge in candidate_edges:
            outgoing.setdefault(edge.source, []).append(edge)
            incoming.setdefault(edge.target, []).append(edge)
        while frontier:
            current, distance = frontier.popleft()
            if distance >= depth:
                continue
            adjacent: list[tuple[CpgEdge, str]] = []
            if direction in {"out", "both"}:
                adjacent.extend((edge, edge.target) for edge in outgoing.get(current, ()))
            if direction in {"in", "both"}:
                adjacent.extend((edge, edge.source) for edge in incoming.get(current, ()))
            for edge, neighbor in adjacent:
                selected_edges[edge.edge_id] = edge
                if neighbor not in reached:
                    reached.add(neighbor)
                    frontier.append((neighbor, distance + 1))
        selected_nodes = tuple(node for node in self.nodes if node.node_id in reached)
        return CpgSubgraph.create(
            target_id=self.target_id,
            graph=graph,
            root_node_id=root,
            nodes=selected_nodes,
            edges=selected_edges.values(),
            problems=self.problems,
            truncated=self.truncated,
            unresolved=self.unresolved,
        )


__all__ = [
    "CpgEdge",
    "CpgNode",
    "CpgProblem",
    "CpgSubgraph",
    "CpgTarget",
]
