# Conditional Outline Range Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore complete Delphi routine ranges in 2.x without replacing the linear outline path with full parsing.

**Architecture:** Extend the existing scanner in `delphi_lsp/lsp_server.py` with a small conditional-branch state machine. It merges compatible branch-local block stacks and retains the original-source fallback for ambiguous structures, so every existing caller benefits without API changes.

**Tech Stack:** Python 3.10+, unittest/pytest, existing Delphi lexical outline scanner.

---

### Task 1: Reproduce the truncated range

**Files:**
- Modify: `tests/test_lsp_support.py`
- Modify: `tests/test_agent_codebase.py`

- [ ] Add a minimal `IFDEF` fixture that selects `try` versus `begin`, contains a nested `begin/end`, and has statements after that nested block.
- [ ] Assert that `outline_source` removes all body statements while preserving source size and line count.
- [ ] Assert through `build_codebase_index` and the symbols JSON layer that the routine range ends at the outer routine `end`.
- [ ] Run the two new tests and verify that they fail because the current transformer returns the original source and reports the first nested `end`.

### Task 2: Implement compatible conditional-stack merging

**Files:**
- Modify: `delphi_lsp/lsp_server.py`
- Test: `tests/test_lsp_support.py`
- Test: `tests/test_agent_codebase.py`

- [ ] Add a bounded compiler-directive reader supporting brace and parenthesis-star directive syntax.
- [ ] Track opening, alternative, and closing conditional directives only while an outline block is active.
- [ ] Reject branches that close pre-existing blocks or produce incompatible normalized stack shapes.
- [ ] Merge compatible statement-block alternatives and continue the existing linear scan.
- [ ] Run the focused regression tests and verify they pass.
- [ ] Run all outline, LSP, agent-layer, and Protocol v2 tests.

### Task 3: Verify real-code correctness and regression safety

**Files:**
- No production changes expected.

- [ ] Run the complete test suite.
- [ ] Run the project lint/type checks configured by the repository.
- [ ] Query `TSynLog` against the pinned local mORMot2 checkout and verify that 2.x now matches 1.1.1 for `TSynLog._Release` and `DefaultSynLogExceptionToStr` end lines.
- [ ] Run the 117,511-line generated LSP query and confirm it remains inside the existing cold-start budget.
- [ ] Build wheel and sdist, inspect their metadata, and smoke-install the wheel in Python 3.11.
