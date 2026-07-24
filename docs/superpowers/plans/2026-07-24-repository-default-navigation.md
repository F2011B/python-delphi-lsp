# Repository-Default Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repository-root navigation work immediately across many Delphi projects, with optional `.dproj` selection.

**Architecture:** `AgentWorkspace` adds and activates its existing synthetic workspace representation only when discovery finds multiple concrete project entries. Project discovery resolves an explicit `.dproj` to its `MainSource`; all downstream indexing continues to receive a Pascal project entry rather than XML.

**Tech Stack:** Python 3.10+, pytest, existing Protocol v2 cache and Delphi discovery/indexing modules.

---

### Task 1: Default multi-project repositories to a workspace index

**Files:**
- Modify: `tests/test_agent_workspace.py`
- Modify: `tests/test_agent_context.py`
- Modify: `delphi_lsp/agent_workspace.py`

- [ ] Add a failing workspace test asserting that two project entries expose an active `Workspace` project plus both concrete projects and inventory every source once.
- [ ] Add a failing context test asserting that `find` works immediately and no `project_required` error occurs.
- [ ] Run the two tests and verify that the current unselected workspace fails.
- [ ] Add the synthetic project in `AgentWorkspace.open` only for implicit multi-project discovery, select it by default, and preserve concrete project mappings.
- [ ] Run the focused tests and verify they pass.

### Task 2: Resolve an optional `.dproj`

**Files:**
- Modify: `tests/test_project_discovery.py`
- Modify: `delphi_lsp/project_discovery.py`
- Modify: `delphi_lsp/agent_cli.py`

- [ ] Add a failing test passing `project_file=Main.dproj` and asserting `Main.dpr` is the sole project entry while `.dproj` settings are retained.
- [ ] Add failing cases for missing, malformed, non-Pascal, and nonexistent `MainSource` values.
- [ ] Run the discovery tests and verify that XML is currently exposed as a project entry.
- [ ] Resolve explicit `.dproj` files before candidate collection and record deterministic discovery problems for invalid entries.
- [ ] Update CLI help to describe `.dproj`, `.dpr`, and `.dpk` inputs.
- [ ] Run the focused discovery and CLI tests.

### Task 3: Verify cache and OpenCode behavior

**Files:**
- Modify: `tests/test_agent_cache.py`
- Modify: `tests/test_agent_worker.py`
- Modify: `README.md`

- [ ] Replace the obsolete selection-required cache test with a failing test asserting startup prewarms a multi-project repository.
- [ ] Add a worker-level query test showing OpenCode's root-scoped context can `find` symbols from different projects without a `project_id`.
- [ ] Run the focused tests and verify the current behavior fails.
- [ ] Update documentation to state that root-only repository navigation is the default and `.dproj` is optional.
- [ ] Run cache, context, discovery, workspace, worker, and CLI suites.
- [ ] Run `git diff --check` and the complete test suite; classify only independently reproducible timing-threshold failures as performance-environment failures.

