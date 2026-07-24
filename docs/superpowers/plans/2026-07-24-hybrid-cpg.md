# Hybrid Code Property Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lazy, RAM-bounded Delphi Code Property Graph to Protocol v3 while preserving 2.3.3 navigation performance on four-million-line repositories.

**Architecture:** Keep the compact navigation registry as the repository-wide index. Build immutable AST, CFG, DFG, and call subgraphs only for the requested target, cache them in a byte-bounded LRU, and expose them through one paginated `cpg` action.

**Tech Stack:** Python 3.10+, DelphiAST-compatible Python parser, existing semantic/navigation registry, argparse CLI, NDJSON worker/cache protocol, pytest.

---

## File Structure

- Create `delphi_lsp/agent_cpg.py`: immutable graph records, stable IDs, retained-byte accounting, selection, and filtering.
- Create `delphi_lsp/agent_cpg_builder.py`: target syntax selection plus AST, CFG, DFG, and explicit-call builders.
- Create `tests/test_agent_cpg.py`: focused graph and performance-invariant tests.
- Modify `delphi_lsp/agent_protocol.py`: Protocol v3 request fields and validation.
- Modify `delphi_lsp/agent_context.py`: lazy construction, LRU ownership, revision invalidation, memory accounting, and response handling.
- Modify `delphi_lsp/agent_cli.py`: ergonomic `cpg` query arguments and Protocol v3 help.
- Modify `delphi_lsp/agent_templates.py`: OpenCode schema, validation, and usage instructions.
- Modify cache, metadata, README, and package tests only where Protocol v3/version reporting requires it.

### Task 1: Protocol v3 request contract

**Files:**
- Modify: `delphi_lsp/agent_protocol.py`
- Modify: `tests/test_agent_protocol.py`

- [ ] **Step 1: Write failing Protocol v3 tests**

Add tests that assert:

```python
request = AgentRequest.from_mapping({
    "action": "cpg",
    "target_id": "target_v2_example",
    "graph": "cfg",
    "direction": "both",
    "depth": 8,
})
assert request.graph == "cfg"
assert request.direction == "both"
assert request.depth == 8
assert AgentResponse("", Focus(), [], Page(), ContextBudget(0)).schema == 3
```

Parametrize invalid `graph`, `direction`, boolean depth, depth zero, depth 17,
and unknown fields. Assert their stable error codes.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_agent_protocol.py -q
```

Expected: failures because `cpg`, the fields, and schema 3 do not exist.

- [ ] **Step 3: Implement the request contract**

Set `SCHEMA_VERSION = 3`, add:

```python
SUPPORTED_ACTIONS = (..., "cpg")
SUPPORTED_GRAPHS = ("ast", "cfg", "dfg", "call", "full")
SUPPORTED_DIRECTIONS = ("out", "in", "both")

@dataclass(frozen=True)
class AgentRequest:
    ...
    graph: str = "full"
    direction: str = "out"
    depth: int = 4
```

Validate strings and `1 <= depth <= 16`, include the fields in mappings and
cursor fingerprints, and preserve the existing target identifier format.

- [ ] **Step 4: Verify GREEN**

Run the same test command and require zero failures.

- [ ] **Step 5: Commit**

Stage only protocol and protocol-test files. Commit title:
`feat(protocol): add Protocol v3 CPG request fields`.

### Task 2: Immutable CPG graph model

**Files:**
- Create: `delphi_lsp/agent_cpg.py`
- Create: `tests/test_agent_cpg.py`

- [ ] **Step 1: Write failing graph-model tests**

Test deterministic IDs, JSON mappings, edge deduplication, stable ordering,
depth filtering, graph-kind filtering, and retained-byte estimates:

```python
node = CpgNode.create(
    label="ROUTINE",
    identity=("src/U.pas", "TThing.Run", 10, 3),
    properties={"path": "src/U.pas", "line": 10, "column": 3},
)
assert node.node_id == CpgNode.create(...).node_id
assert node.to_mapping()["item_type"] == "cpg_node"
assert CpgSubgraph(...).retained_bytes > 0
```

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_agent_cpg.py -q`.
Expected: import failure for `delphi_lsp.agent_cpg`.

- [ ] **Step 3: Implement the graph model**

Implement slot-based immutable records:

```python
@dataclass(frozen=True, slots=True)
class CpgNode:
    node_id: str
    label: str
    properties: Mapping[str, object]

@dataclass(frozen=True, slots=True)
class CpgEdge:
    edge_id: str
    label: str
    source: str
    target: str
    properties: Mapping[str, object]

@dataclass(frozen=True, slots=True)
class CpgSubgraph:
    target_id: str
    graph: str
    nodes: tuple[CpgNode, ...]
    edges: tuple[CpgEdge, ...]
    problems: tuple[Mapping[str, object], ...]
    truncated: bool
    unresolved: int
```

Use SHA-256 stable IDs, mapping proxies or copied primitive dictionaries,
deterministic sort keys, a hard 50,000-record builder limit, and compact
retained-byte accounting without recursive graph walks.

- [ ] **Step 4: Verify GREEN**

Run the focused file and require zero failures.

- [ ] **Step 5: Commit**

Commit title: `feat(cpg): add immutable property graph model`.

### Task 3: AST and target selection

**Files:**
- Create: `delphi_lsp/agent_cpg_builder.py`
- Modify: `tests/test_agent_cpg.py`

- [ ] **Step 1: Write failing AST tests**

Parse a unit containing a class, method, assignment, conditional, and call.
Build an AST graph for the method target. Assert:

```python
assert labels >= {"ROUTINE", "STATEMENT", "CONTROL", "CALL", "IDENTIFIER"}
assert all(edge.label == "AST" for edge in ast_edges)
assert all(node.properties["path"] == "UnitA.pas" for node in source_nodes)
assert source_text not in json.dumps(graph.to_items())
```

Also test a unit target, parser-partial input, deterministic repeated builds,
and a 50,000-record truncation boundary.

- [ ] **Step 2: Verify RED**

Run the AST test selection and confirm missing builder failures.

- [ ] **Step 3: Implement target-local AST building**

Expose:

```python
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
    ...
```

Select the smallest declaration/implementation subtree matching normalized
name and source line. Traverse it iteratively in preorder, classify syntax
types into public labels, emit `AST` edges, normalize paths, and sanitize parse
problems. Never retain source text or parser roots in `CpgSubgraph`.

- [ ] **Step 4: Verify GREEN**

Run AST-focused tests and require zero failures.

- [ ] **Step 5: Commit**

Commit title: `feat(cpg): build lazy target AST graphs`.

### Task 4: Control-flow graph

**Files:**
- Modify: `delphi_lsp/agent_cpg_builder.py`
- Modify: `tests/test_agent_cpg.py`

- [ ] **Step 1: Write failing CFG tests**

Cover sequential statements, if/else merge, while/for back edges, repeat-until,
case branches, try/except, try/finally, raise, goto/label, and exit. Assert that
every routine has one `ENTRY`, one `EXIT`, deterministic `CFG` edges, no
dangling endpoints, and reachable ordinary statements.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_agent_cpg.py -q -k cfg`.
Expected: no CFG edges.

- [ ] **Step 3: Implement structured CFG fragments**

Use a fragment result rather than global mutable recursion:

```python
@dataclass(frozen=True, slots=True)
class _CfgFragment:
    entries: tuple[str, ...]
    normal_exits: tuple[str, ...]
    breaks: tuple[str, ...] = ()
    continues: tuple[str, ...] = ()
    terminal: tuple[str, ...] = ()
```

Compose sequences, branch fan-out/merge, loop back edges, and terminal edges.
Resolve labels after traversal. Unknown statements conservatively remain in
sequence and add a `cpg_problem` only when control flow cannot be represented.

- [ ] **Step 4: Verify GREEN**

Run CFG tests and the complete CPG file.

- [ ] **Step 5: Commit**

Commit title: `feat(cpg): add Delphi control-flow edges`.

### Task 5: Local data flow and explicit calls

**Files:**
- Modify: `delphi_lsp/agent_cpg_builder.py`
- Modify: `tests/test_agent_cpg.py`

- [ ] **Step 1: Write failing DFG and call tests**

Use routines with assignments, parameters, field reads, branch joins,
loop-carried values, explicit calls, overloaded names, and unresolved calls.
Assert `DEF`, `USE`, and `REACHING_DEF` for unique local names. Assert `CALL`
only when compact-registry resolution has exactly one candidate; ambiguity
increments metadata rather than selecting a target.

- [ ] **Step 2: Verify RED**

Run `python -m pytest tests/test_agent_cpg.py -q -k 'dfg or call'`.

- [ ] **Step 3: Implement conservative DFG and call resolution**

Classify assignment left sides and declaration/parameter nodes as definitions;
classify right sides, conditions, and call arguments as uses. Run a forward
worklist over CFG predecessor sets:

```python
incoming[node] = union(outgoing[pred] for pred in predecessors[node])
for name in uses[node]:
    emit REACHING_DEF from each incoming[node][name]
outgoing[node] = transfer(incoming[node], defs[node])
```

Bound worklist iterations by graph size, case-fold Delphi identifiers, and
record aliasing/indirect dispatch as `sound_partial` limitations.

- [ ] **Step 4: Verify GREEN**

Run focused and complete CPG tests.

- [ ] **Step 5: Commit**

Commit title: `feat(cpg): add conservative data and call flow`.

### Task 6: AgentContext LRU and CPG action

**Files:**
- Modify: `delphi_lsp/agent_context.py`
- Modify: `tests/test_agent_context.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing context/cache tests**

Assert:

- non-CPG requests do not parse CPG syntax;
- first `cpg` query builds exactly one target source;
- an identical query is an LRU hit;
- workspace revision and project focus invalidate subgraphs;
- CPG bytes contribute to `estimated_cache_bytes`;
- auxiliary eviction clears CPG bytes first;
- CPG pressure triggers existing 80-percent warnings;
- prewarm leaves CPG count and bytes at zero.

- [ ] **Step 2: Verify RED**

Run context and cache CPG selections; expect unsupported action and missing
memory statistics.

- [ ] **Step 3: Implement lazy context integration**

Add an ordered cache keyed by:

```python
_CpgCacheKey = tuple[str, str, str, str, int]
# revision, target_id, graph, direction, depth
```

Set its byte limit to `min(128 * 1024**2, max(1, budget // 5))`, expose
`cpg_cache_entries` and `cpg_cache_bytes`, clear it during revision/project
changes and auxiliary eviction, and handle `cpg` after resolving the existing
registry target. Convert graph items through the existing pagination response
path.

- [ ] **Step 4: Verify GREEN**

Run context, cache, and CPG files; require zero failures.

- [ ] **Step 5: Commit**

Commit title: `feat(agent): cache lazy CPG subgraphs`.

### Task 7: CLI, worker, and OpenCode interface

**Files:**
- Modify: `delphi_lsp/agent_cli.py`
- Modify: `delphi_lsp/agent_templates.py`
- Modify: `tests/test_agent_worker.py`
- Modify: `tests/test_agent_codebase.py`
- Modify: `tests/test_agent_cache.py`

- [ ] **Step 1: Write failing integration tests**

Assert the CLI accepts:

```text
query --root ROOT cpg TARGET --graph full --direction out --depth 8
```

Assert worker and daemon responses have schema 3. Assert generated TypeScript
includes `cpg`, graph/direction enums, depth validation, and usage guidance.
Assert every existing action remains accepted.

- [ ] **Step 2: Verify RED**

Run the four integration test files with `-k cpg`; expect parser/template
failures.

- [ ] **Step 3: Implement the public integration**

Add CLI arguments from protocol constants, map the positional value to
`target_id` for `cpg`, update Protocol help strings, update generated request
types and runtime validators, and teach the skill to call `find`, `focus`,
then targeted `cpg`. Preserve root auto-discovery and optional project fields.

- [ ] **Step 4: Verify GREEN**

Run all four files and require zero failures.

- [ ] **Step 5: Commit**

Commit title: `feat(cli): expose Protocol v3 CPG queries`.

### Task 8: Four-million-LOC performance gate

**Files:**
- Create: `scripts/benchmark_cpg.py`
- Modify: `tests/test_agent_cpg.py`
- Modify: `tests/test_github_performance_corpus.py`

- [ ] **Step 1: Write failing instrumentation tests**

Assert a synthetic large workspace does not invoke the CPG builder during
prewarm or legacy actions. Assert target-local CPG reads one source path and a
repeated request invokes no parse.

- [ ] **Step 2: Verify RED**

Run the instrumentation tests and confirm missing counters/benchmark entry
points.

- [ ] **Step 3: Implement benchmark harness**

The script must report JSON containing:

```json
{
  "loc": 4000000,
  "prewarm_seconds": 0.0,
  "legacy_queries_per_second": 0.0,
  "cpg_first_seconds": 0.0,
  "cpg_cached_seconds": 0.0,
  "cpg_cache_bytes": 0,
  "sources_parsed_for_cpg": 1
}
```

Accept an existing corpus root and a deterministic generated-corpus mode.
Measure 2.3.3-compatible legacy actions before CPG, then first and repeated
focused CPG requests. Fail if legacy throughput regresses more than 5 percent
against the recorded in-run control or if CPG touches more than one unit.

- [ ] **Step 4: Run the performance gate**

Use the existing messy corpus when its manifest is valid; otherwise generate
at least four million LOC in a temporary directory. Record command, machine,
LOC, timings, RSS, and pass/fail in release evidence.

- [ ] **Step 5: Commit**

Commit title: `perf(cpg): verify lazy four-million-LOC behavior`.

### Task 9: Prepare version 3.0.0

**Files:**
- Modify: `pyproject.toml`
- Modify: `delphi_lsp/_version.py`
- Modify: `README.md`
- Modify: `tests/test_package_metadata.py`
- Create: `docs/release-3.0.0.md`

- [ ] **Step 1: Write failing metadata/documentation tests**

Require version 3.0.0, Protocol v3, CPG action/fields, graph labels, lazy memory
behavior, and the measured four-million-LOC result.

- [ ] **Step 2: Verify RED**

Run metadata and documentation tests; expect 2.3.3/version text failures.

- [ ] **Step 3: Update release material**

Set all public versions to `3.0.0`. Document migration, unchanged target IDs,
examples, graph completeness, cache sizing, and benchmark evidence.

- [ ] **Step 4: Run final verification**

Run:

```bash
python -m pytest -q
git diff --check
python -m build --outdir TEMP_DIR
python -m twine check TEMP_DIR/*
```

Install the wheel in a clean temporary environment and run a cache-start plus
focused CPG smoke test.

- [ ] **Step 5: Commit and stop before publication**

Commit title: `release: prepare python-delphi-lsp 3.0.0`.
Push the branch, verify it is clean and up to date, then ask the user for
explicit PyPI/GitHub publication approval.
