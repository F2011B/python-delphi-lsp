# Large Codebase Performance Design

## Goal

Make Protocol v2 semantic queries complete predictably on a real, pinned GitHub Object Pascal corpus of at least two million physical lines while preserving response schemas, target IDs, source ranges, and body extraction behavior.

## Evidence and root cause

On the pinned mORMot2 core corpus, `AgentContext.open` completed quickly but `find TSynLog` did not finish after 152 seconds. The same query on v1.1.1 completed in milliseconds. Profiling attributed most time to `_exclude_routine_locals`, `_raw_routine_span`, and `_find_routine_token_span`.

The pathological input is a sequence of body-less class routine declarations after `implementation`. The outline can expose these as top-level raw routine symbols. The current recursive routine scanner re-evaluates the remaining suffix for every declaration and only caches the outer public request. That creates exponential work. The fixed scanner must cache every nested positive and negative result and must not depend on Python recursion depth.

## Runtime design

`_SourceDocument` owns a token-index routine-span cache for the lifetime of the document. `_find_routine_token_span` becomes an explicit-stack state machine. Each routine heading is evaluated at most once; child routine results are reused by parent frames. Positive and negative results are both cached.

`_exclude_routine_locals` first computes routine containers, sorts them by start offset, and filters symbols with a sweep over active container ends. This removes the current symbols-by-containers nested scan. Identical ranges and object identity continue to distinguish the container symbol itself from enclosed locals.

The implementation keeps the existing full-parser fallback for directives and ambiguous spans. Protocol v2 output, target IDs, pagination, source chunks, and package plugin contracts do not change.

## Regression tests

The focused regression constructs consecutive implementation-side class routine declarations. It instruments declaration scanning and asserts a linear upper bound, then scales beyond Python's normal recursion limit. Existing nested local routine, forward declaration, structured type, directive, and body extraction tests remain required.

The performance test is deterministic and does not depend on wall-clock timing for its unit-level assertion. Release benchmarking separately records wall-clock time and peak RSS.

## GitHub corpus

The repository stores metadata only in `tests/corpora.performance.lock.json`. The corpus builder fetches or validates exact Git revisions in an external cache and assembles only Object Pascal source files into an external workspace. It preserves repository-relative paths below repository-named roots and writes a manifest containing repository URL, revision, selected files, per-project LOC, total LOC, and SHA-256.

The locked sources include mORMot2, DUnitX, DelphiAST, python4delphi, FPCSource, Castle Game Engine, and Spring4D. Deterministic path ordering selects whole files until the assembled workspace contains at least 2,000,000 physical lines. No corpus source is committed to the package.

## Performance and E2E gates

The release benchmark must prove on the assembled corpus:

- manifest total is at least 2,000,000 lines and every checkout matches its locked revision;
- cold `AgentContext.open` plus a known broad `find` completes below the 120-second plugin timeout, with a release target of less than 60 seconds;
- the second identical `find` completes in less than one second;
- `focus` and `inspect` return source-backed evidence for a locked symbol;
- peak RSS and every phase duration are recorded in JSON.

The OpenCode E2E uses `openrouter/google/gemma-4-31b-it` and the installed `python-delphi-lsp` agent. Only `delphi_codebase` is permitted for code navigation. The transcript must show `open`, `find`, `focus`, and `inspect`, reject `bash`, `read`, `grep`, and `glob`, and end with an exact path and line citation present in the corpus manifest.

## Release contract

Version 2.0.3 is built from the committed source. A clean virtual environment installs the wheel, runs package smoke tests, installs the OpenCode integration into an isolated home, and reruns the real OpenRouter E2E against the two-million-line corpus. PyPI upload is allowed only if unit, integration, performance, package-content, fresh-install, and model E2E gates all pass. A failed or unavailable external gate leaves the release unpublished.

