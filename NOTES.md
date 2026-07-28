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
