# OKF Export Memory and Progress Design

## Goal

Reduce peak memory during `delphi-lsp-agent wiki export` without dropping any
exported knowledge, and show useful progress while the command is running.

## Evidence

The released 3.1.0 exporter was measured on the bundled mORMot2 corpus with
541 source files, 106 projects, and 78,324 symbols using one worker:

| Stage | Resident memory |
| --- | ---: |
| Process baseline | 28.9 MiB |
| Semantic index plus retained project syntax trees | 248.6 MiB |
| Wiki writer initialized | 300.3 MiB |
| Metrics completed | 349.1 MiB |
| Peak | 378.8 MiB |

An outline-only index used 167.1 MiB. `tracemalloc` attributed 52.2 MiB of
additional live allocations in `_WikiWriter` primarily to one
`PurePosixPath`, a normalized seven-field tuple key, and a second dictionary
entry for every symbol.

## Memory design

The wiki's project scope is a main-project dependency closure, not every
recursively discovered `.dpr`. An explicit `--project-file` is authoritative.
Otherwise, non-example root entry files are selected; when none exist, the
shallowest configured non-example `.dproj` entries are used. Example, sample,
test, benchmark, and vendor projects are excluded. Dependency and include
resolution cannot leave the canonical repository root. If no main entry
exists, the exporter inventories repository sources but creates no synthetic
project pages.

`build_codebase_index` gains an opt-in project-result compaction mode. A deep
project is still fully parsed so dependency discovery, include files, missing
units, parse flags, and diagnostics remain identical. Immediately after each
project completes, its `UnitInfo.syntax_tree` fields are cleared in the stored
result. Other callers retain the current default and can still request syntax
trees; the wiki exporter opts into compaction.

The wiki writer no longer materializes a global `_SymbolRecord` list. It walks
the already resident semantic models in deterministic source order and derives
the stable Markdown filename when needed. Reverse-reference buckets use the
resolved symbol object's integer identity rather than repeating normalized
paths and range tuples. Unit and symbol pages build only per-unit or per-symbol
temporary lists. Symbol directory index entries are streamed after a
deterministic name-order pass.

Within the selected scope, Markdown content, stable names, and link targets
remain unchanged; manifest counts reflect that scope. The acceptance gate is
at least a 30 percent reduction from the 378.8 MiB one-worker mORMot2 peak,
with all internal links still resolving.

The final equivalent full-source fallback measurement on mORMot2 retained all
541 repository sources and 78,324 symbols while reducing peak RSS to 245.4 MiB
and elapsed time to 57.2 seconds. This is a 35.2 percent peak-memory reduction
and a 33.5 percent runtime reduction. Repositories with identifiable main
projects additionally avoid unrelated project/source closures.

## Progress design

The Python API accepts an optional `on_progress` callback receiving immutable
`WikiProgressEvent` values with `phase`, `completed`, `total`, `path`, and
`detail`. Events cover discovery/indexing, deep project parsing, metrics,
project pages, unit pages, symbol pages, reference/problem/reference-document
pages, installation, and completion.

The CLI renders these events to `stderr`, preserving the final JSON object as
the only `stdout` record. On a terminal it rewrites one compact line; when
redirected it emits bounded milestone lines rather than one line per symbol.
Every phase shows a percentage when a total is known. `--quiet` disables the
renderer for automation.

## Testing

- Verify compact project results retain metadata but not syntax trees.
- Verify the writer does not retain a global symbol-record collection.
- Use `tracemalloc` on a synthetic large semantic model to enforce a bounded
  writer-initialization allocation.
- Verify progress callback phase ordering and terminal completion.
- Verify default CLI progress is on `stderr`, JSON remains on `stdout`, and
  `--quiet` suppresses progress.
- Run the focused wiki/progress tests, the full suite, link validation, and the
  mORMot2 peak-RSS benchmark.
