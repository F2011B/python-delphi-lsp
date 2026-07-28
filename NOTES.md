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
