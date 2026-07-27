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

The resulting Markdown content, stable names, link targets, and concept counts
remain unchanged. The acceptance gate is at least a 30 percent reduction from
the 378.8 MiB one-worker mORMot2 peak, with all internal links still resolving.

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

