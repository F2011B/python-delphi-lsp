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
