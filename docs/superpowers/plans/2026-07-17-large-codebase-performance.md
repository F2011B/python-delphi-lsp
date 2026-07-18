# Large Codebase Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Protocol v2 queries complete on a pinned GitHub Object Pascal workspace with at least two million lines and publish 2.0.3 only after real OpenCode/OpenRouter E2E passes.

**Architecture:** Replace recursive suffix rescanning with a document-owned iterative memoized routine scanner, then filter local symbols with sorted intervals. Add metadata-only corpus acquisition and benchmark/E2E harnesses outside the package payload.

**Tech Stack:** Python 3.10-3.14, pytest, Protocol v2 JSON worker, Git, OpenCode, OpenRouter Gemma 4 31B.

---

### Task 1: Lock the routine-scanner regression

**Files:**
- Modify: `tests/test_agent_context.py`

- [ ] **Step 1: Write the failing operation-count test**

Add a source containing consecutive `class procedure TRegistry.StepNNN;` declarations after `implementation`. Monkeypatch `_heading_semicolon_index`, call `_routine_span` for the first declaration, and assert the heading scanner is called no more than four times per declaration.

```python
def test_bodyless_class_routine_suffix_is_scanned_linearly(monkeypatch, tmp_path):
    declarations = 18
    source = "unit ForwardOnly;\ninterface\nimplementation\n" + "".join(
        f"class procedure TRegistry.Step{index:04d};\n" for index in range(declarations)
    ) + "end.\n"
    path = tmp_path / "ForwardOnly.pas"
    path.write_text(source, encoding="utf-8")
    document = agent_context_module._SourceDocument(path, path.name, source)
    calls = 0
    original = agent_context_module._heading_semicolon_index

    def counted(tokens, start):
        nonlocal calls
        calls += 1
        return original(tokens, start)

    monkeypatch.setattr(agent_context_module, "_heading_semicolon_index", counted)
    assert agent_context_module._routine_span(document, source.index("class procedure")) is None
    assert calls <= declarations * 4
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_agent_context.py::test_bodyless_class_routine_suffix_is_scanned_linearly`

Expected: FAIL because the recursive implementation scans the same suffix exponentially.

### Task 2: Make routine scanning iterative and memoized

**Files:**
- Modify: `delphi_lsp/agent_context.py`
- Modify: `tests/test_agent_context.py`

- [ ] **Step 1: Add a document cache**

Add `routine_token_spans: dict[int, tuple[int, int, int] | None]` to `_SourceDocument` and pass it from `_routine_span` into `_find_routine_token_span`.

- [ ] **Step 2: Replace recursion with frames**

Represent each pending routine with its start token, current scan token, and optional child continuation. Push unresolved nested routines, cache positive and negative results on pop, then resume the parent from either the child end or declaration end. Preserve all directive, structured-type, `begin`, `asm`, and `end` branches.

- [ ] **Step 3: Verify GREEN and recursion independence**

Run the focused test, then increase the generated declaration count to 1,200 and keep the linear call bound.

Run: `python -m pytest -q tests/test_agent_context.py -p no:cacheprovider`

Expected: all agent-context tests pass.

### Task 3: Remove symbol-by-container filtering

**Files:**
- Modify: `delphi_lsp/agent_context.py`
- Modify: `tests/test_agent_context.py`

- [ ] **Step 1: Write a failing containment-count test**

Generate many top-level routines and many local raw symbols, instrument interval comparisons, and assert filtering grows with symbols plus containers rather than their product.

- [ ] **Step 2: Implement sorted interval filtering**

Sort containers by `(start, end)`, sort symbols by source offset while retaining original order, advance a container cursor, maintain active ends, and omit symbols strictly enclosed by a different container. Restore original symbol order in the result.

- [ ] **Step 3: Verify behavior**

Run the existing nested local routine, forward declaration, type-body, and body extraction tests together with the new regression.

### Task 4: Build the pinned GitHub corpus harness

**Files:**
- Create: `tests/corpora.performance.lock.json`
- Create: `scripts/build_github_performance_corpus.py`
- Create: `scripts/benchmark_github_corpus.py`
- Create: `tests/test_github_performance_corpus.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing lock and manifest tests**

Validate full 40-character revisions, HTTPS GitHub URLs, unique corpus names, allowed Pascal extensions, deterministic selection, rejection of dirty/wrong-revision checkouts, and a minimum manifest LOC of 2,000,000.

- [ ] **Step 2: Implement explicit acquisition and assembly**

The builder accepts `--cache`, `--workspace`, and `--offline`. It clones/fetches only when explicitly requested, verifies `HEAD`, hard-links or copies selected `.pas`, `.dpr`, `.dpk`, and `.inc` files, and writes `corpus-manifest.json` with per-file SHA-256 and LOC.

- [ ] **Step 3: Implement benchmark actions**

Measure `AgentContext.open`, cold and warm `find`, `focus`, and `inspect`; record result counts, known evidence, wall time, and peak RSS in JSON; exit nonzero if LOC, 60-second cold query, or one-second warm query gates fail.

- [ ] **Step 4: Verify the real corpus**

Build the corpus outside the repository and run the benchmark. Preserve the manifest and report under `/private/tmp/python-delphi-lsp-2m-evidence/`.

### Task 5: Add the OpenRouter model E2E

**Files:**
- Create: `scripts/run_openrouter_github_e2e.py`
- Create: `tests/test_openrouter_github_e2e.py`
- Modify: `scripts/run_opencode_lsp_probe.py`

- [ ] **Step 1: Write failing command/evidence tests**

Assert the harness selects `openrouter/google/gemma-4-31b-it`, installs into isolated XDG directories, requires `delphi_codebase.open/find/focus/inspect`, forbids `bash/read/grep/glob`, and validates the final path, line, and source text against the manifest.

- [ ] **Step 2: Implement the online wrapper**

Reuse the JSONL probe parser, never print the API key, preserve raw JSONL plus a redacted summary, and fail on missing tool order, forbidden tools, timeout, nonzero process status, or invalid citation.

- [ ] **Step 3: Run real OpenCode E2E**

Run OpenCode against the assembled corpus with Gemma 4 31B and store artifacts under `/private/tmp/python-delphi-lsp-2m-evidence/opencode/`.

### Task 6: Release 2.0.3 conditionally

**Files:**
- Modify: `pyproject.toml`
- Modify: `delphi_lsp/_version.py`
- Modify: `README.md`
- Create: `docs/release-evidence/2.0.3.md`

- [ ] **Step 1: Verify all source gates**

Run the full pytest suite, compileall, build, Twine check, corpus benchmark, and `git diff --check`.

- [ ] **Step 2: Verify a fresh wheel**

Install the wheel in a new virtual environment outside the checkout, run imports and CLI smoke tests, install the OpenCode integration into an isolated home, and repeat the corpus benchmark and model E2E using only the installed wheel.

- [ ] **Step 3: Publish only on complete green**

Upload 2.0.3 to PyPI only when every recorded gate is green. Verify both PyPI JSON and Simple index, then install 2.0.3 from public PyPI in another clean environment and rerun the smoke test. Do not upload if any gate is failed, skipped, unavailable, or stale.
