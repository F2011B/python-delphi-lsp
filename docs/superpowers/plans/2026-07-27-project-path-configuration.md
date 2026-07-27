# TOML Project Path Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repository-level TOML include/exclude selection for Delphi projects and use it consistently across discovery consumers.

**Architecture:** A focused `project_config` module loads and validates `.delphi-lsp.toml` and matches repository-relative project paths. `project_discovery` applies the selection before reading project metadata; all existing CLI and server consumers inherit the behavior through the shared discovery function.

**Tech Stack:** Python 3.10+, `tomllib`/`tomli`, pytest, pathlib

---

### Task 1: Configuration loader and matcher

**Files:**
- Create: `delphi_lsp/project_config.py`
- Create: `tests/test_project_config.py`
- Modify: `pyproject.toml`

- [ ] Write tests that load valid include/exclude arrays, match exact directories and `*`/`**` patterns case-insensitively, and reject invalid TOML, unknown keys, absolute paths, parent traversal, and non-string arrays.
- [ ] Run `pytest tests/test_project_config.py -q` and verify it fails because the module does not exist.
- [ ] Implement immutable configuration values, conditional TOML parsing, validation, and deterministic matching.
- [ ] Add conditional `tomli` support for Python 3.10.
- [ ] Run `pytest tests/test_project_config.py -q` and verify all loader tests pass.

### Task 2: Project discovery integration

**Files:**
- Modify: `delphi_lsp/project_discovery.py`
- Modify: `tests/test_project_discovery.py`

- [ ] Write tests for multiple included projects at different depths, `.dproj` matching, exclusion precedence, explicit project override, and a configured zero-match error.
- [ ] Run the new discovery tests and verify the old discovery ignores the TOML filters.
- [ ] Load selection once per discovery and apply it to every non-explicit candidate using both entry and companion `.dproj` paths.
- [ ] Record `.delphi-lsp.toml` in discovery configuration metadata.
- [ ] Run project configuration and discovery tests and verify they pass.

### Task 3: Wiki and workspace behavior

**Files:**
- Modify: `delphi_lsp/agent_layers.py`
- Modify: `delphi_lsp/agent_wiki.py`
- Modify: `delphi_lsp/agent_cli.py`
- Modify: `tests/test_agent_wiki.py`
- Modify: `tests/test_agent_workspace.py`

- [ ] Write a wiki regression test proving configured exclusions cannot trigger the all-source fallback and a workspace test proving the configured project list is exposed.
- [ ] Run both tests and verify they fail with existing behavior.
- [ ] Preserve existing fallback only when no configured project filter is active, and translate configuration failures into clear CLI/wiki errors.
- [ ] Run focused agent tests and verify they pass.

### Task 4: User documentation

**Files:**
- Modify: `README.md`
- Modify: `tests/test_package_metadata.py`

- [ ] Write a documentation assertion for `.delphi-lsp.toml`, `[projects]`, include/exclude precedence, glob semantics, explicit override, and no-match behavior.
- [ ] Run the assertion and verify it fails before README changes.
- [ ] Add a complete monorepo configuration section and command examples.
- [ ] Run package metadata tests and verify they pass.

### Task 5: Verification and delivery

**Files:**
- Verify all changed production, test, documentation, and packaging files.

- [ ] Run focused tests for configuration, discovery, wiki, workspace, and CLI errors.
- [ ] Run the complete pytest suite.
- [ ] Run Python 3.10 syntax compilation, `compileall`, `git diff --check`, package build, and `twine check`.
- [ ] Review the final diff for scope, memory regressions, unsafe path handling, and documentation consistency.
- [ ] Commit with the required author policy, rebase on origin, push, and verify clean synchronized status and green GitHub CI.

### Task 6: Complete workspace directory exclusions

**Files:**
- Modify: `delphi_lsp/project_config.py`
- Modify: `delphi_lsp/project_discovery.py`
- Modify: `delphi_lsp/project_indexer.py`
- Modify: `delphi_lsp/agent_layers.py`
- Modify: `delphi_lsp/agent_workspace.py`
- Modify: `delphi_lsp/agent_relations.py`
- Modify: `tests/test_project_config.py`
- Modify: `tests/test_project_discovery.py`
- Modify: `tests/test_project_indexer.py`
- Modify: `tests/test_agent_wiki.py`
- Modify: `README.md`

- [ ] Add failing loader tests for `[workspace].exclude` validation and matching.
- [ ] Add a failing traversal test proving an excluded directory's `.dproj`, `.dpr`, `.pas`, and `.inc` files are absent without being opened.
- [ ] Add a failing project-indexer test proving explicit unit and include dependencies inside an excluded directory are not read.
- [ ] Parse `workspace.exclude`, prune matching `os.walk` directories, and reject matching explicit project entries.
- [ ] Pass the loaded path configuration through every production `ProjectIndexer` construction.
- [ ] Add a wiki acceptance test proving excluded dependency files produce no pages.
- [ ] Document the distinction between project exclusion and complete workspace exclusion.
- [ ] Run focused tests, the complete suite, package build, and cross-platform CI before delivery.
