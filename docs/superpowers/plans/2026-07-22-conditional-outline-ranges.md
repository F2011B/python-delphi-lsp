# Conditional Outline Range Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore complete Delphi routine ranges in 2.x without replacing the linear outline path with full parsing.

**Architecture:** Centralize define-aware conditional selection in `build_outline_semantic_model`, retain exact source positions, and extend the raw outline scanner with a branch state machine for callers that do not yet have an effective define set.

**Tech Stack:** Python 3.10+, unittest/pytest, existing Delphi preprocessor and lexical outline scanner.

---

### Task 1: Reproduce the truncated range

**Files:**
- Modify: `tests/test_lsp_support.py`
- Modify: `tests/test_agent_codebase.py`

- [x] Add minimal fixtures for balanced and split `IFDEF` statement blocks.
- [x] Assert that safe outlining preserves source size and line count.
- [x] Assert through `build_codebase_index` that the routine range ends at the outer routine `end`.
- [x] Verify that the tests fail on the old 2.x transformer.

### Task 2: Implement conditional-safe outlining

**Files:**
- Modify: `delphi_lsp/lsp_server.py`
- Modify: `delphi_lsp/preprocessor.py`
- Modify: `delphi_lsp/agent_layers.py`
- Modify: `delphi_lsp/agent_context.py`

- [x] Add a bounded compiler-directive reader for brace and parenthesis-star syntax.
- [x] Merge compatible raw branch stacks and isolate incompatible routines.
- [x] Select the active branch from effective project defines before semantic outlining.
- [x] Preserve exact offsets, line endings, active includes, and multiline strings.
- [x] Centralize transformation in `build_outline_semantic_model` and avoid double transforms.
- [x] Run the focused LSP, agent-layer, workspace, and Protocol v2 tests.

### Task 3: Verify real-code correctness and regression safety

**Files:**
- No production changes expected.

- [x] Run the complete test suite.
- [x] Run project syntax and diff checks (no repository lint/type command is configured).
- [x] Query `TSynLog` on local mORMot2 and compare the affected ranges with 1.1.1.
- [x] Run the generated 100k-line LSP performance checks in the full suite.
- [x] Build wheel and sdist, inspect metadata, and smoke-install the wheel in Python 3.11.
