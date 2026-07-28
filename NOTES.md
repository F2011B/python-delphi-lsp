# Remediation Notes

## Baseline

- Baseline commit: `d79d61305bc5983dba2b0590110ef0e1f0fb4495`.
- The local test environment initially lacked the declared `watchfiles`
  dependency. Running `python -m pip install -e '.[test]'` repaired only the
  local virtual environment; it did not change repository files.
- Tests: 812 passed, 1 skipped, 11 warnings, and 60 subtests passed.
- Intentional platform skip:
  `tests/test_openrouter_github_e2e.py:1279` requires Windows Job Objects.
- Ruff baseline: 323 diagnostics (239 auto-fixable).
- mypy baseline: 114 errors in 17 files.
- The deterministic semantic/agent snapshot covers 45 Delphi fixture files
  and 152 implementation queries. A repeated baseline run was byte-identical.

## Finding status

### F1 — LSP document synchronization

- Added regressions for the advertised pygls synchronization mode and for
  indexing the document text already synchronized by pygls.
- Configured `LanguageServer` itself for full-document synchronization.
- Removed the dead initialize capability response, whose value pygls discards,
  while retaining initialization side effects.
- `didChange` now indexes the synchronized workspace document rather than the
  last content-change fragment.
- Focused result: 53 passed, 13 subtests passed.
- Full result: 814 passed, 1 skipped, 33 warnings, 60 subtests passed.
- Golden snapshot classification: identical (no semantic or agent-layer
  change).

### F6 — Rename and reference search ranges

- Added regressions for unit-level symbols and for cross-file reference
  attribution.
- Unit-scope symbols now use a whole-file fallback search instead of the
  synthetic one-line unit declaration range.
- Text fallback references receive the file being scanned explicitly, so
  ranges can never combine coordinates from one document with another
  document's name.

### F4 — Include source-map stop-gap

- Added a rename regression using a resolved include file.
- Rename now returns no edit when preprocessing maps any emitted line to a
  foreign file. This deliberately favors a safe refusal over potentially
  rewriting unrelated text until full source-map translation lands in
  Batch 8.
- Batch 2 full result: 817 passed, 1 skipped, 44 warnings, and 60 subtests
  passed.
- Batch 2 golden snapshot classification: identical.

### F7 / F11 — Incremental LSP outline rebuild

- Added a measured regression that records outline-builder calls across
  document update and close, plus a dispatch regression for all three
  document lifecycle notifications.
- Unchanged disk snapshots retain their semantic outline model. Rebuilding
  the shared workspace index re-registers those models and parses only the
  changed document.
- `didOpen`, `didChange`, and `didClose` are marked for pygls thread-pool
  dispatch so parsing does not block the protocol event loop.
- The implementation deliberately rebuilds the lightweight shared
  `SymbolIndex` from cached models rather than adding mutable unregister
  operations to `SymbolIndex`; this preserves deterministic registration
  order and avoids stale name-index entries.

### F14 / F36 — Bounded workspace-symbol query cache

- Added an eviction regression that distinguishes LRU behavior from FIFO.
- Query-specific workspace semantic results now use an eight-entry
  `OrderedDict`; hits refresh recency and insertion evicts the least recently
  used entry.

### F21 — Shared workspace directory pruning

- Added a regression containing Delphi sources beneath every shared
  `SKIP_DIRS` name and one canonical source outside them.
- The LSP walker now applies the same skip-directory set as project discovery
  and the agent workspace, preventing duplicate build/worktree sources from
  entering the semantic index.
- Batch 3 full result: 821 passed, 1 skipped, 70 warnings, and 60 subtests
  passed.
- Batch 3 golden snapshot classification: identical.

### F2 — Less-than statement recovery

- Added a hand-parser regression with a `while Index < 10` followed by a
  second routine declaration.
- Statement and condition collectors no longer track angle brackets.
  Declaration collectors retain generic-aware tracking, narrowed to an
  identifier immediately adjacent to `<`, with statement-boundary recovery.
- The first Batch 4 snapshot exposed an adjacent recovery boundary: once F2
  revealed later statements, inline `asm ... end` was mistaken for the
  enclosing routine end and reduced resolved references from 22 to 21 in
  `unit_statements.pas`. A dedicated regression now keeps the post-assembly
  assignment attached; the reference count is restored to 22.

### F3 — Metaclass and class-helper parsing

- Added one regression covering `class of`, `class helper for`, helper
  members, and a following class field.
- Metaclasses now produce a `classref` type and stop at their semicolon.
  Helpers consume their target type before entering the normal class body.
- A defensive type-body guard prevents stray `of` or `helper` tokens from
  consuming subsequent declarations during tolerant recovery.

### F5 — Incomplete enum values

- Added a regression for `(alNone = 0, alTop =)`.
- Empty enum-value token lists now add a precise parser problem while
  preserving both enum items, instead of indexing an empty list and raising
  `IndexError`.

### F13 — Nested routines

- Added a structural regression for an outer routine with a local variable,
  nested routine, and its own body.
- Local routine declarations are now parsed recursively during the enclosing
  routine's declaration phase. The nested routine and both statement blocks
  remain attached to their correct owners.
- The Batch 4 snapshot also caught forward declarations being mistaken for
  enclosing routines after recursion was enabled. A second regression keeps
  bodyless `forward`, `external`, and `abstract` routines as siblings of the
  next declaration.
- Batch 4 full result: 827 passed, 1 skipped, 70 warnings, and 60 subtests
  passed.
- Batch 4 snapshot classification:
  - F3 changes `unit_types.pas`: the bogus `helper` field becomes the real
    `Help` procedure, syntax problems decrease 4→3, and resolved references
    increase 3→4.
  - F2 changes `unit_statements.pas`: recovered control flow raises
    cyclomatic complexity 5→8 and changes maintainability
    41.670635083303196→41.26712631137338. One recovery problem at the
    pre-existing `case ... else` boundary is now visible; symbols remain 4
    and resolved references remain 22.
  - Agent-layer payloads are unchanged. No clean fixture raises, and no
    fixture loses symbols or resolved references.

### F12 — Malformed BOM source handling

- Added an indexer regression using a UTF-8 BOM followed by invalid UTF-8.
- `_read_file` now routes `UnicodeError` through the existing per-file
  `CANT_OPEN_FILE` problem path instead of aborting the entire index.

### F17 — UTF-8 CLI streams

- Added regressions for stream reconfiguration and the structured fallback
  when an encoding error is still unavoidable.
- CLI stdout and stderr are reconfigured to UTF-8 when the stream supports
  it. Remaining `UnicodeEncodeError` failures now return
  `cli_error:encoding_error` instead of a traceback.

### F20 — Invalid LSP project configuration

- Added regressions for state fallback and user-visible initialize warnings.
- Project-config load or discovery errors now fall back to the unfiltered
  workspace configuration, retain the warning, and surface it through
  `window/showMessage` without aborting LSP initialization.

### F22 — Out-of-root project candidates

- Added a cross-platform regression that simulates a walked project symlink
  resolving outside the repository.
- Resolved candidates must now be relative to the repository root before
  auxiliary-project and minimum-depth calculations.
- Batch 5 full result: 833 passed, 1 skipped, 84 warnings, and 60 subtests
  passed.
- Batch 5 golden snapshot classification: identical to the classified Batch 4
  snapshot.

### F8 — Revision invalidation race

- Added a deterministic race regression that invalidates during a revision
  scan and verifies the immediately following request rescans.
- A monotonic revision epoch now guards timestamp write-back, so an in-flight
  refresh cannot erase a newer watcher invalidation.

### F18 — Watcher health

- Added a regression that simulates a watcher returning unexpectedly.
- The service records watcher failure, exposes `watcher_active: false` in
  status, and invalidates the revision cache before every subsequent request
  so results degrade to per-request validation rather than silent staleness.

### F33 — Watcher exclusions

- Added a watcher-filter regression covering a normal Delphi unit, a TOML
  `workspace.exclude`, a shared skip directory, and an unrelated suffix.
- The daemon watcher now uses a workspace-specific filter combining watched
  suffixes, `SKIP_DIRS`, and the active project configuration.
- Batch 6 full result: 836 passed, 1 skipped, 84 warnings, and 60 subtests
  passed.
- Batch 6 golden snapshot classification: identical to Batch 5.

### F9 — Wiki semantic references

- Added an export regression with a resolved global-variable reference.
- Wiki export now performs a streaming tolerant semantic pass, maps resolved
  targets back to retained outline symbols, and stores only lightweight
  reference records. Temporary full semantic scopes are removed from the
  shared reference index after each file to preserve bounded residency.
- Existing assertion updated:
  `assert "BlockedUnit" not in unit_text` became
  `assert "\n# BlockedUnit\n" not in unit_text`. The old assertion rejected
  even an honest unresolved reference in an allowed source; the corrected
  assertion still proves that no page is generated for the excluded unit,
  while the existing read-spy assertions prove excluded files are not read.

### F10 — Project maintainability aggregation

- Added a multi-unit regression with deliberately different unit sizes and
  maintainability scores.
- Project maintainability is now the source-line-weighted mean of the unit
  indices, with the existing full-maintainability value of `100.0` retained
  when a project has no source lines.

### F26 — Streaming layer metrics

- Added a delegation regression that fails if the metrics layer reads source
  files itself.
- The layer now passes the discovered file inventory and active project
  filters to `build_path_metrics`, avoiding a complete in-memory source map
  before analysis.

### F25 — Bounded implementation queries

- Added regressions proving broad implementation queries prepare at most 50
  matches, traverse the semantic symbol inventory once, and use a four-file
  LRU source cache with real least-recently-used eviction.
- Matches are deduplicated, deterministically sorted, and truncated before
  source extraction. A per-request routine lookup now replaces repeated
  whole-index scans for type and member implementation fragments.

### F16 — Paged response preparation

- Added regressions proving cursor pages reuse one prepared sequence, the
  response cache is capped at eight entries and cleared by auxiliary
  eviction, and a 256-character find budget materializes only the selected
  symbol card.
- Prepared responses are cached by revision and cursor-independent request
  fingerprint. Symbol entries retain their card JSON size and conservative
  upper bound, allowing the lazy card sequence to calculate pagination
  without serializing the full result set and to chunk only accessed cards.
- Batch 7 golden snapshot classification:
  - F10 changes the project maintainability index from the saturated `0.0`
    to the source-line-weighted `51.91223967859085`.
  - F26 changes metrics unit paths from fixture-root absolute paths to stable
    repository-relative paths.
  - No symbols, references, parser problems, or implementation payloads
    regress.

### F4 — Include source-map plumbing

- Added semantic regressions for declarations inside an include and after an
  include, plus a rename regression that verifies both edits target the
  original unit lines.
- Preprocessed source maps now flow through direct parser semantics, workspace
  semantics, project index results, and relation-graph semantics.
  `SemanticBuilder` maps every node range back to the originating file and
  line before constructing symbols or references.
- Existing assertion updated:
  `assert result is None` for include-bearing rename became exact assertions
  for the two safe mapped edits. The temporary Batch 2 stop-gap is no longer
  needed because ranges are now mapped instead of discarded.

### F15 — Canonical LSP paths

- Added a platform-independent Windows-path regression covering drive paths
  and UNC file URIs.
- URI-derived paths, workspace scan results, cache keys, source aggregation,
  model lookup, and open-document URI lookup now share native `Path`
  spelling. File URI hosts are retained as UNC paths.

### F19 — Persistent navigation-cache bounds

- Added shard-store regressions for stale-key pruning and oldest-first byte
  budget eviction, plus an integration assertion that a changed unit leaves
  only its current content-addressed shard.
- Completed navigation builds prune against the live content hashes and a
  configurable 512 MiB default disk budget. `cache start
  --max-disk-cache SIZE` propagates that budget to the daemon and status.
- Added `cache clear --root PATH`, which stops the daemon before safely
  removing the validated navigation-cache directory, and documented both CLI
  controls.

### F23 — Fail-closed workspace exclusions

- Added an escaping-directory-symlink regression covering direct config
  checks, the project indexer boundary, and the include loader.
- Workspace exclusion checks now require both lexical and resolved
  containment. A path outside the configured root, including an in-root
  symlink that escapes it, is excluded instead of silently re-admitted.

### F24 — Flat global symbol indexing

- Added a three-unit transitive-uses regression that compares the global
  lookup exactly with the declaring unit's symbols.
- `SymbolIndex` no longer recursively re-indexes imported scopes when a unit
  is registered. Each unit already registers its own symbols, while semantic
  import resolution remains unchanged.
- Existing assertion setup updated: the cyclic-import test now registers both
  unit scopes before checking both global lookups. Its former single
  registration encoded the duplicate-producing transitive indexing behavior;
  it still verifies cyclic imports terminate and each symbol appears once.

### F27 — Layered test timeouts

- Added 20-minute limits to both CI jobs and the `pytest-timeout` test-only
  dependency with a 120-second thread-based per-test limit.
- LSP response reads now run through a five-second reader-thread deadline.
  Worker NDJSON reads use the same bounded queue pattern, including watcher,
  focus, recovery, and EOF checks.
- Added deterministic blocked-reader regressions for both helpers.

### F28 — Complete source distributions

- Added the missing recursive Markdown docs rule to `MANIFEST.in` and its
  packaged-test guard.
- The package CI job now extracts the freshly built sdist, installs its test
  extra, and runs the shipped tests from that extracted tree.
- A local clean-room Python 3.14 run passed with 850 tests, 2 platform skips,
  84 warnings, and 60 subtests; both release-note tests found their packaged
  docs.

### F29 — PEP 561 typing marker

- Added `delphi_lsp/py.typed`, declared it as setuptools package data, and
  included it explicitly in source archives.
- Package metadata tests and the wheel smoke step now require the marker.
  A locally built wheel was inspected and contains `delphi_lsp/py.typed`.

### F30 — UTF-16 LSP positions

- Added an astral-character regression covering inbound identifier and symbol
  lookup, member-completion parsing, and outbound semantic ranges.
- LSP request characters are converted from UTF-16 code units before indexing
  Python strings. All emitted diagnostics, document/workspace symbols,
  definitions, references, and rename edits convert code-point columns back
  to UTF-16.
- Outbound conversion uses an eight-file source-line cache with an ASCII fast
  path, preserving the 100,000-line document-symbol latency regression.

### F31 — Metrics target IDs

- Added CLI regressions for both positional `target_v2_...` values and the new
  explicit `query metrics --target-id` form.
- Metrics unit target IDs now populate the protocol `target_id` field instead
  of being treated as name/path queries that return an empty page.
- Supplying both a positional value and `--target-id` is rejected with the
  existing structured `cache_error:invalid_request` contract.

### F32 — Workspace-relative index output

- Added a subprocess regression that runs the index command from outside the
  selected repository and verifies the output is created only below the
  workspace.
- `index` and `wiki export` now share one output-path resolver. Relative
  destinations are anchored to the canonical `--root`, and `index` prints the
  resolved path.

### F34 — Empty workspace-exclude fast path

- Added a regression that makes repository path resolution fail if it is
  reached for a configuration without workspace exclusions.
- `excludes_workspace_path` now returns immediately when its pattern tuple is
  empty, avoiding lexical normalization and filesystem-backed resolution in
  every discovery walk entry.
