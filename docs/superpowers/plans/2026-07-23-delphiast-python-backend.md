# DelphiAST Python Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a pure-Python DelphiAST-compatible parser as the default backend and keep all commands bounded on the verified 4M-LOC FPC corpus.

**Architecture:** Keep preprocessing and public AST classes stable, add a linear lexer and tolerant recursive-descent backend, and retain Lark behind an explicit compatibility selector. Bulk deep operations use the tolerant backend without unbounded fallback; the cache avoids per-request full revision scans and uses a bounded long-response timeout.

**Tech Stack:** Python 3.10+, existing `SyntaxNode`/semantic APIs, pytest, multiprocessing cache workers, setuptools/build/twine.

---

### Task 1: Backend contract and selection

**Files:**
- Create: `delphi_lsp/parser_backend.py`
- Modify: `delphi_lsp/parser.py`
- Test: `tests/test_parser_backends.py`

- [ ] Write tests proving that `delphiast` is the default, `lark` remains selectable, invalid backend names fail, and tolerant parsing never calls the Lark fallback.
- [ ] Run `pytest tests/test_parser_backends.py -q` and verify the new tests fail because the backend API does not exist.
- [ ] Add `ParserBackend`, `ParserMode`, backend normalization, and dependency injection into `DelphiParser`.
- [ ] Run the focused tests and verify they pass.

### Task 2: DelphiAST token model and lexer

**Files:**
- Create: `delphi_lsp/delphiast_tokens.py`
- Create: `delphi_lsp/delphiast_lexer.py`
- Test: `tests/test_delphiast_lexer.py`

- [ ] Write lexer tests for Delphi/FPC identifiers, `&escaped` identifiers, numbers, adjacent strings and character codes, comments, directives, multi-character operators, positions, and bounded invalid input.
- [ ] Run `pytest tests/test_delphiast_lexer.py -q` and verify failures for missing modules.
- [ ] Implement an O(n) scanner with immutable tokens and no regular expression capable of super-linear backtracking.
- [ ] Run the lexer tests and the isolated `aasmcpu.pas` lexer benchmark.

### Task 3: Structural recursive-descent parser

**Files:**
- Create: `delphi_lsp/delphiast_parser.py`
- Test: `tests/test_delphiast_parser.py`

- [ ] Write tests for unit sections, dependencies, declarations, type bodies, routines, blocks, and recovery from FPC-only syntax.
- [ ] Verify the tests fail before implementation.
- [ ] Implement DelphiAST-compatible node construction, token synchronization, partial-tree problems, and a forward-progress invariant.
- [ ] Verify focused tests pass and `aasmcpu.pas` completes below one second.

### Task 4: Expressions, statements, and semantic compatibility

**Files:**
- Modify: `delphi_lsp/delphiast_parser.py`
- Modify: `delphi_lsp/parser_backend.py`
- Test: `tests/test_delphiast_semantic_compatibility.py`

- [ ] Write differential tests for assignments, calls, member access, operators, control flow, routine parameters, generics, inheritance, and semantic symbol lookup.
- [ ] Verify the differential tests fail for missing expression/statement nodes.
- [ ] Add Pratt expression parsing and statement parsing using existing `SyntaxNodeType` and `AttributeName` values.
- [ ] Run semantic, metrics, relations, diagnostics, and parser compatibility tests.

### Task 5: Default integration and bounded fallback

**Files:**
- Modify: `delphi_lsp/parser.py`
- Modify: `delphi_lsp/project_indexer.py`
- Modify: `delphi_lsp/agent_relations.py`
- Modify: `delphi_lsp/metrics.py`
- Test: `tests/test_parser_backends.py`
- Test: `tests/test_agent_relations.py`
- Test: `tests/test_metrics.py`

- [ ] Write failing tests that deep bulk actions use tolerant DelphiAST parsing and never invoke Lark.
- [ ] Add backend/mode propagation and structured parser problems.
- [ ] Run all affected suites and verify no regression in public response schemas.

### Task 6: Warm request latency and transport deadlines

**Files:**
- Modify: `delphi_lsp/agent_workspace.py`
- Modify: `delphi_lsp/agent_context.py`
- Modify: `delphi_lsp/agent_cache.py`
- Test: `tests/test_agent_cache.py`
- Test: `tests/test_agent_context.py`

- [ ] Write failing tests for revision-scan coalescing, immediate explicit invalidation, separate connect/response timeouts, and the 1 GiB default cache budget.
- [ ] Implement a monotonic revision-check deadline in cached contexts and retain immediate scans for standalone use.
- [ ] Separate socket connection and response deadlines and surface timeout-specific errors.
- [ ] Run cache and context suites.

### Task 7: Documentation, attribution, and release benchmark

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `scripts/benchmark_github_corpus.py`
- Test: `tests/test_package_metadata.py`
- Test: `tests/test_agent_cache.py`

- [ ] Add tests for backend documentation, attribution, status fields, and 4M benchmark budgets.
- [ ] Update user documentation and remove Lark from mandatory runtime dependencies only after the explicit Lark backend is covered by an optional dependency.
- [ ] Run `pytest -q`, Ruff checks, build wheel/sdist, `twine check`, and inspect archive contents.
- [ ] Run the verified 4M corpus benchmark for prewarm and every protocol action.
- [ ] Commit the validated release candidate and ask the user for explicit PyPI/GitHub publication approval.
