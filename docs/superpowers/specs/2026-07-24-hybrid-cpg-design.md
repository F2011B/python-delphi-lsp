# Hybrid Code Property Graph Design

## Objective

Version 3.0.0 adds a real, queryable Code Property Graph (CPG) to
`python-delphi-lsp` without regressing repository startup or navigation
performance on workspaces with four million or more lines of Delphi code.

The CPG unifies syntax, semantic, call, control-flow, and local data-flow
information behind Protocol v3 and the existing cache daemon.

## Existing Baseline

The package already provides:

- a compact repository navigation registry;
- stable target identifiers for units and symbols;
- lazy semantic relations for references, calls, uses, inheritance, and
  implementation;
- bounded source-document and cache-daemon memory;
- repository-root operation with optional project selection;
- parallel and disk-cached navigation prewarming.

It is not yet a CPG because syntax nodes are not public graph nodes and the
public model has no AST, control-flow, definition, use, or reaching-definition
edges.

## Chosen Architecture

The implementation is a hybrid, lazy CPG.

The navigation registry remains the compact repository-wide index. CPG
subgraphs are built only for the focused or explicitly requested unit,
type, or routine. Constructed subgraphs are stored in a byte-bounded LRU cache
owned by `AgentContext`. They are invalidated with the workspace revision and
evicted before the compact navigation registry.

This avoids the prohibitive memory and startup costs of materializing millions
of syntax nodes for an entire repository. It also avoids presenting the
existing relations API as a CPG without adding control and data flow.

## Public Protocol v3

Protocol v3 retains all Protocol v2 actions and adds the `cpg` action.

New request fields:

- `graph`: `ast`, `cfg`, `dfg`, `call`, or `full`; default `full`;
- `direction`: `out`, `in`, or `both`; default `out`;
- `depth`: integer from 1 through 16; default 4.

`cpg` accepts `target_id` or the current focused target. It uses the existing
`cursor`, `max_items`, and `max_chars` fields. An absent target produces
`target_required`; a non-unit/type/routine target produces
`cpg_not_applicable`.

Responses continue to use the bounded `AgentResponse` envelope with schema 3.
The result contains:

- one `cpg_metadata` item;
- zero or more `cpg_node` items;
- zero or more `cpg_edge` items;
- zero or more `cpg_problem` items.

CPG node identifiers and edge identifiers are deterministic hashes of
normalized source identity and graph position. Existing `target_v2_*`
identifiers remain unchanged so cached focus and integrations do not lose
their targets during the major-version upgrade.

## Graph Model

Node labels:

- `PROJECT`, `UNIT`, `TYPE`, `ROUTINE`, and `SYMBOL`;
- `ENTRY` and `EXIT`;
- `STATEMENT`, `CONTROL`, `CALL`, `IDENTIFIER`, `LITERAL`, and `EXPRESSION`.

All source-backed nodes carry normalized repository-relative `path`, `line`,
`column`, `end_line`, and `end_column` properties when known. Semantic nodes
also carry name, qualified name, symbol kind, visibility, and type. Source
text is not retained in graph records.

Edge labels:

- structure: `CONTAINS`, `AST`;
- semantics: `REF`, `CALL`, `USES`, `INHERITS`, `IMPLEMENTS`;
- control: `CFG`;
- local data flow: `DEF`, `USE`, `REACHING_DEF`.

All relationships are conservative. Ambiguous targets are not fabricated.
Metadata reports `sound_partial`, unresolved counts, truncation, parser
problems, and limitations for indirect calls, aliasing, exception propagation,
and conditional compilation.

## Lazy Builders

`delphi_lsp.agent_cpg` owns immutable graph records, deterministic IDs, graph
selection, depth limiting, and retained-byte accounting.

`delphi_lsp.agent_cpg_builder` converts the already selected Delphi syntax
subtree into graph records:

- preorder syntax traversal builds `AST` edges;
- structured statement traversal builds `ENTRY`, `EXIT`, and `CFG` edges for
  sequences, conditionals, loops, cases, try/except/finally, raise, goto, and
  routine exits;
- assignment and expression traversal classifies local identifier definitions
  and uses;
- a forward worklist computes conservative reaching definitions;
- explicit call syntax resolves only unique compact-registry candidates.

Unit and type requests expose bounded structural graphs. Routine requests add
control and data flow. Unsupported or partially parsed syntax yields problems
rather than failing the entire query.

## Cache and Memory

`AgentContext` owns a CPG LRU keyed by workspace revision, target, graph kind,
direction, and depth.

- The CPG cache receives at most 20 percent of the daemon memory budget and is
  capped at 128 MiB.
- A single subgraph is capped at 50,000 nodes plus edges.
- Oversized subgraphs are returned as deterministic truncated graphs and are
  not retained if they exceed the CPG cache budget.
- CPG retained-byte estimates are included in
  `AgentContext.estimated_cache_bytes`.
- Auxiliary eviction clears CPG subgraphs before navigation eviction.
- Existing 80-percent cache warnings and compaction apply unchanged.

No CPG parsing occurs during `cache start` prewarming. Therefore the baseline
startup path remains compact navigation only.

## Performance Requirements

For a repository of at least four million Delphi LOC:

- cache-start prewarming must not construct a CPG subgraph;
- `open`, `find`, `focus`, `inspect`, and existing `trace` behavior must not
  require CPG construction;
- idle CPG memory must be zero;
- a focused routine CPG must parse only its source unit;
- a repeated identical CPG query must be served from memory;
- CPG eviction must restore the pre-CPG auxiliary memory state;
- benchmarked non-CPG navigation throughput must remain within 5 percent of
  the 2.3.3 baseline, accounting for measurement noise.

The release gate combines deterministic instrumentation tests with the
existing large-corpus benchmark. If the available corpus is smaller than four
million LOC, it is replicated by manifest or generated source without copying
CPG state, and the measured LOC is reported.

## CLI and OpenCode

Example:

```text
delphi-lsp-agent query --root PATH cpg TARGET_ID --graph full \
  --direction out --depth 8 --max-items 50 --max-chars 40000
```

The generated OpenCode plugin and skill expose the same fields and teach the
agent to use `find`, `focus`, then `cpg`. Repository-root and optional-project
behavior remain unchanged.

## Error Handling

CPG requests use stable errors:

- `target_required`;
- `target_not_found`;
- `cpg_not_applicable`;
- `source_unavailable`;
- `cpg_build_failed`.

Raw parser exceptions, absolute external paths, tokens, and source bodies are
not exposed. Partial parser and resolver results appear as sanitized
`cpg_problem` items.

## Verification and Release

Implementation follows red-green-refactor tests for protocol validation, graph
identity, AST, CFG, DFG, calls, pagination, revision invalidation, memory
accounting, cache eviction, CLI, worker, and OpenCode templates.

The release gate is:

- focused CPG tests;
- complete package suite;
- `git diff --check`;
- sdist and wheel build;
- `twine check`;
- fresh installation smoke test;
- large-corpus navigation and focused-CPG benchmark.

Version 3.0.0 is prepared but not published until the user explicitly approves
publication.
