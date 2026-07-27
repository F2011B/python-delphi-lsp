# OKF Export Memory and Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce OKF export peak RSS by at least 30 percent and add a default CLI progress display without changing exported knowledge.

**Architecture:** Compact deep-project results after dependency extraction, then generate symbol knowledge directly from semantic models instead of retaining a duplicate global catalog. Route immutable export progress events to a throttled stderr renderer while keeping stdout JSON-only.

**Tech Stack:** Python 3.10+, dataclasses, pathlib, argparse, pytest, tracemalloc, macOS `resource`/`ps`.

---

### Task 1: Lock project compaction behavior

**Files:**
- Modify: `tests/test_agent_codebase.py`
- Modify: `delphi_lsp/agent_layers.py`

- [ ] Add a test that calls `build_codebase_index(..., index_projects=True, retain_project_syntax=False)` and asserts that parsed-unit names, paths, error flags, and dependency membership remain present while every `syntax_tree` is `None`.
- [ ] Run the focused test and confirm it fails because `retain_project_syntax` is not accepted.
- [ ] Add the keyword-only flag with default `True`, and replace each stored deep result with a metadata-equivalent `ProjectIndexResult` containing `dataclasses.replace(unit, syntax_tree=None)` when the flag is false.
- [ ] Run the focused test and the existing codebase/progress tests.

### Task 2: Lock bounded writer initialization

**Files:**
- Modify: `tests/test_agent_wiki.py`
- Modify: `delphi_lsp/agent_wiki.py`

- [ ] Add a structural regression test that initializes `_WikiWriter` from a synthetic multi-symbol index under `tracemalloc`, asserts that no global `symbols` collection or tuple-key symbol map is retained, and enforces a conservative per-symbol allocation ceiling.
- [ ] Run the test and confirm it fails against the 3.1.0 global `_SymbolRecord` catalog.
- [ ] Replace global records with deterministic unique-symbol iterators, string page derivation, integer identity reverse-reference buckets, direct member-scope traversal, and streamed directory indexes.
- [ ] Run the focused wiki tests and validate every internal Markdown link.

### Task 3: Add export progress API and CLI renderer

**Files:**
- Modify: `tests/test_agent_wiki.py`
- Modify: `delphi_lsp/agent_wiki.py`
- Modify: `delphi_lsp/agent_cli.py`

- [ ] Add failing API tests for immutable `WikiProgressEvent` values and a final `complete` event.
- [ ] Add failing CLI tests proving progress is written to stderr, stdout remains one JSON object, redirected output is throttled, and `--quiet` suppresses progress.
- [ ] Add `on_progress` to `export_okf_wiki`, emit progress from indexing and each writer loop, and implement a CLI renderer that uses carriage returns for TTYs and bounded milestone lines otherwise.
- [ ] Run focused API and CLI tests.

### Task 4: Document and benchmark

**Files:**
- Modify: `README.md`
- Modify: `docs/release-3.1.0.md`

- [ ] Document the default progress display, `--quiet`, stdout/stderr contract, and bounded-memory generation.
- [ ] Run `pytest -q`.
- [ ] Run `git diff --check` and package build checks.
- [ ] Run the same one-worker mORMot2 stage benchmark used by the design evidence and require peak RSS no higher than 265 MiB.
- [ ] Commit with the repository commit-policy helper, pull with rebase, push, and verify `main` is synchronized with `origin/main`.
