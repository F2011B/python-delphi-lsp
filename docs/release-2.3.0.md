# Python Delphi LSP 2.3.0

Version 2.3.0 accelerates repeated analysis of very large Delphi workspaces
without changing the command protocol.

## Performance work

- Workspace revision discovery is reused instead of rescanning the same tree.
- Preprocessor output is emitted in spans rather than one character at a time.
- Symbol search, pagination, and relation traversal use dedicated indexes.
- Compact, content-addressed navigation shards persist under
  `.delphi-lsp/agent-cache/navigation-v1` and are shared by CLI and OpenCode
  daemon invocations.
- Metrics are calculated in worker processes without returning complete source
  maps to the parent process.
- Workspace `uses` relations are staged so unit-level navigation does not force
  construction of the full semantic graph.

## Large-corpus validation

The release candidate was exercised against an 8,549-file corpus containing
4,029,668 lines of Pascal source and 4,421 units:

| Workload | Result |
| --- | ---: |
| Cold navigation index | 19.44 s |
| Warm persistent-cache load | 7.64 s |
| Metrics, 8 workers | 53.21 s |
| Metrics, 1 worker | 248.27 s |
| Largest-unit staged `uses` query | 4.99 s |

The parallel metrics path was 4.67 times faster than the single-worker path.
The staged `uses` query was 6.36 times faster than the earlier eager relation
path on the tested 6.87 MB unit.

The retained navigation estimate was 393,253,967 bytes for 390,925 entries,
about 73% of the default 512 MiB budget and therefore below the 80% warning
threshold. The retained budget is deliberately separate from transient parser
peaks and process RSS.

## Cache safety and compatibility

Persistent shards are versioned JSON rather than executable serialization.
Loads validate the schema, cache key, file size, ownership, permissions, file
type, and symlink state. Invalid or incompatible shards are ignored and rebuilt.
All existing agent commands continue to use the same navigation context; the
cache changes how that context is produced, not which commands can consume it.
