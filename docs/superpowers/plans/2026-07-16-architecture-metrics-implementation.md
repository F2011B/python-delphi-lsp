# Architecture Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans to implement this plan task-by-task. Progress is
> tracked in Beads issue `mac-358a`; this plan uses numbered steps instead of a
> Markdown task list to comply with the governing Beads workflow.

**Goal:** Add deterministic Delphi unit/project architecture metrics, prove
that restricted OpenCode with vLLM Ornith can use them, and publish
`python-delphi-lsp` 2.0.1.

**Architecture:** A pure `delphi_lsp.metrics` engine calculates source and
project metrics. `delphi_lsp.agent_metrics` adapts active workspaces and legacy
codebase indexes to Protocol v2 and CLI payloads. Existing protocol envelopes,
pagination, worker isolation, and source restrictions remain intact.

**Tech Stack:** Python 3.10+, dataclasses, existing Delphi parser/semantic
model, pytest, pygls/lsprotocol, OpenCode, local vLLM Metal with Ornith 1.0 9B,
setuptools/build/twine.

---

### Task 1: Preserve metric-relevant Delphi syntax

**Files:**

- Modify: `delphi_lsp/lark_builder.py`
- Test: `tests/test_parser.py`

1. Add a parser test whose source contains two non-else `case` selectors and
   assert that the `ntCase` node owns two `ntCaseSelector` children. Also parse
   a `class abstract` declaration and assert its type node preserves
   `AttributeName.anAbstract == "true"`.

```python
def test_parser_preserves_case_selectors_and_abstract_class_modifier() -> None:
    source = """unit MetricsSyntax;
interface
type TAbstractThing = class abstract end;
implementation
procedure Score(Value: Integer);
begin
  case Value of
    1: Value := 2;
    2, 3: Value := 4;
  else
    Value := 0;
  end;
end;
end.
"""
    result = DelphiParser().parse(source, "MetricsSyntax.pas", build_semantic=False)
    case_node = result.root.find_node(SyntaxNodeType.ntCase)
    assert case_node is not None
    assert sum(child.typ == SyntaxNodeType.ntCaseSelector for child in case_node.child_nodes) == 2
    type_node = result.root.find_node(SyntaxNodeType.ntType)
    assert type_node is not None
    assert type_node.get_attribute(AttributeName.anAbstract) == "true"
```

2. Run
   `.venv/bin/python -m pytest tests/test_parser.py::test_parser_preserves_case_selectors_and_abstract_class_modifier -q`.
   Expected result: failure because `case_statement` drops list children and
   `class_type` drops the modifier token collection.

3. Make `case_statement` flatten list children, add a `class_modifiers`
   transformer returning normalized modifier names, and apply those names to
   `ntType` attributes in `class_type`.

```python
def class_modifiers(self, meta: Any, *children: Any) -> set[str]:
    return {
        token_value(child).casefold()
        for child in children
        if self._is_text(child) or is_token(child)
    }

def case_statement(self, meta: Any, *children: Any) -> SyntaxNode:
    node = self._make_node(SyntaxNodeType.ntCase, meta)
    for child in self._flatten(children):
        if isinstance(child, SyntaxNode):
            node.add_child(child)
    return node
```

4. Run the focused parser test, then all `tests/test_parser.py`. Expected:
   both commands pass.

5. Commit the parser preservation change with the repository commit policy.

### Task 2: Implement the public metrics engine

**Files:**

- Create: `delphi_lsp/metrics.py`
- Modify: `delphi_lsp/__init__.py`
- Create: `tests/test_metrics.py`

1. Write failing tests for physical line classification, multi-line comments,
   compiler directives, symbol counts, two routines with known decision nodes,
   Halstead identities, empty-source MI, dependency extraction, abstractness,
   coupling, and project/include LOC deduplication.

```python
def test_unit_metrics_cover_lines_complexity_halstead_and_symbols() -> None:
    source = """unit Alpha;
interface
// public declaration
procedure Simple;
implementation
{$IFDEF DEBUG}
procedure Simple;
begin
  if Ready then
    while Busy do
      Tick;
end;
{$ENDIF}
end.
"""
    analysis = analyze_unit(source, "Alpha.pas")
    assert analysis.lines.total_lines == len(source.splitlines())
    assert analysis.lines.comment_only_lines == 1
    assert analysis.lines.directive_lines == 2
    assert analysis.cyclomatic.total == 3
    assert analysis.cyclomatic.maximum == 3
    assert analysis.symbol_counts["procedure"] >= 1
    assert analysis.halstead.length > 0
    assert 0.0 <= analysis.maintainability_index <= 100.0
```

```python
def test_project_metrics_compute_coupling_main_sequence_and_loc() -> None:
    project = analyze_project(
        {
            "Main.dpr": "program Main; uses Alpha; begin end.\n",
            "Alpha.pas": "unit Alpha; interface uses Beta, External.Api; implementation end.\n",
            "Beta.pas": "unit Beta; interface type IService = interface end; implementation end.\n",
        },
        include_sources={"Shared.inc": "const Shared = 1;\n"},
    )
    alpha = project.unit_by_name("Alpha")
    beta = project.unit_by_name("Beta")
    assert alpha.afferent_coupling == 1
    assert alpha.efferent_coupling == 2
    assert beta.afferent_coupling == 1
    assert beta.abstractness == 1.0
    assert project.total_loc == 3
    assert project.include_loc == 1
    assert project.total_loc_with_includes == 4
```

2. Run `.venv/bin/python -m pytest tests/test_metrics.py -q`. Expected:
   collection failure because `delphi_lsp.metrics` does not exist.

3. Implement frozen result dataclasses `LineMetrics`, `HalsteadMetrics`,
   `RoutineComplexity`, `CyclomaticMetrics`, `UnitMetrics`, `ProjectMetrics`,
   and `MetricProblem`. Implement a single Delphi lexer that returns token kind,
   spelling, offsets, and per-line code/comment/directive flags. Reject all
   non-finite values before serialization.

4. Implement `analyze_unit` with one full parser pass. Count routine decision
   nodes while skipping nested `ntMethod` nodes, count `ntCaseSelector`
   children, extract dependency names from uses/contains nodes, and walk semantic
   scopes for symbol and abstract-type counts.

5. Implement Halstead formulas and MI exactly as specified in the design.
   Implement `analyze_project` so it canonicalizes unit names, builds reverse
   dependency edges, separates internal/external dependencies, recomputes
   aggregate Halstead vocabulary/totals, and sums unique source paths.

6. Export the documented public result types and functions from
   `delphi_lsp.__init__`.

7. Run `tests/test_metrics.py`, `tests/test_parser.py`, and the full pytest
   suite. Expected: all pass with the existing single platform skip allowed.

8. Commit the public metrics engine with the repository commit policy.

### Task 3: Add Protocol v2 metric queries and caching

**Files:**

- Create: `delphi_lsp/agent_metrics.py`
- Modify: `delphi_lsp/agent_protocol.py`
- Modify: `delphi_lsp/agent_workspace.py`
- Modify: `delphi_lsp/agent_context.py`
- Test: `tests/test_agent_protocol.py`
- Create: `tests/test_agent_metrics.py`
- Test: `tests/test_agent_worker.py`

1. Add failing protocol tests asserting `metrics` is accepted, an unknown
   action is still rejected, and metric requests reject detail values other
   than `summary` and `members`.

```python
def test_protocol_accepts_metrics_action() -> None:
    request = AgentRequest.from_mapping({"action": "metrics", "query": "Alpha"})
    assert request.action == "metrics"
```

2. Add failing context/worker tests for project summary plus unit cards,
   case-insensitive query filtering, exact selection by the unit ID returned by
   `open`, detailed routine/dependency fields, paging, finite JSON, and cache
   invalidation after a source edit.

```python
opened = context.handle({"action": "open", "max_items": 50})
unit_id = next(item["unit_id"] for item in opened.result if item.get("name") == "Alpha")
detailed = context.handle({
    "action": "metrics",
    "target_id": unit_id,
    "detail": "members",
    "max_items": 50,
    "max_chars": 40000,
})
assert detailed.result[0]["item_type"] == "unit_metrics"
assert detailed.result[0]["name"] == "Alpha"
assert detailed.result[0]["routines"]
```

3. Run the focused tests. Expected: protocol failure for unsupported
   `metrics` and no context dispatch.

4. Add `metrics` to `SUPPORTED_ACTIONS`. Add shared stable unit source/display
   helpers to `agent_workspace` and use them from both `open` and the metrics
   adapter so external paths never leak and unit IDs match exactly.

5. Implement `agent_metrics` factories for an `AgentWorkspace` and a
   `CodebaseIndex`, plus compact summary and detailed mapping functions.

6. Add an `AgentContext` metrics cache keyed by active project ID and
   `workspace_revision`. Dispatch `metrics`, validate action-specific details,
   filter/select units, then pass items through the existing pagination and
   context-budget path.

7. Run the focused protocol/metrics/worker tests and then full pytest. Expected:
   all pass.

8. Commit Protocol v2 metric queries with the repository commit policy.

### Task 4: Add CLI and generated OpenCode support

**Files:**

- Modify: `delphi_lsp/agent_cli.py`
- Modify: `delphi_lsp/agent_layers.py`
- Modify: `delphi_lsp/agent_templates.py`
- Modify: `README.md`
- Test: `tests/test_agent_codebase.py`
- Test: `tests/test_opencode_config.py`

1. Add failing tests that `view --layer metrics --format json` produces a
   project summary and filtered unit cards, Markdown names the key metrics, the
   generated tool action union contains `metrics`, and the generated skill
   instructs the model to use it for architecture questions.

2. Run the focused CLI/template tests. Expected: parser choice failure and
   missing generated action/guidance.

3. Add `metrics` to the CLI layer choices. Force project indexing for this
   layer, render the adapter payload through JSON and Markdown, and retain all
   existing layers unchanged.

4. Extend generated TypeScript request types and `tool.schema.enum` with
   `metrics`. Update the generated skill workflow and tool-call documentation.

5. Document the Python API, metric definitions, CLI examples, Protocol action,
   and project/include LOC distinction in `README.md`.

6. Run focused tests and full pytest. Expected: all pass.

7. Commit CLI/OpenCode integration with the repository commit policy.

### Task 5: Build a deterministic Ornith/OpenCode metrics proof

**Files:**

- Create: `scripts/bootstrap_vllm_metrics_test.py`
- Create: `tests/test_bootstrap_vllm_metrics.py`
- Modify: `README.md`

1. Write failing tests for a sandbox containing a project and three units with
   deterministic LOC, coupling, and complexity. Assert the OpenCode command
   uses `vllm/ornith-lspctx`, the restricted `vllm-delphi-codebase` agent,
   requires completed `skill`, `open`, and `delphi_codebase` metric calls,
   forbids raw tools, and requires exact metric values in the final answer.

2. Run the focused test. Expected: import/file failure because the metrics
   bootstrap does not exist.

3. Implement the bootstrap by reusing `ensure_venv`, endpoint readiness,
   generated integration installation, and `run_opencode_lsp_probe.py`. Write
   artifacts below `output/metrics_ornith_proof/`, including sandbox sources,
   OpenCode JSONL, vLLM log when auto-started, and `summary.json` containing
   expected/tool/final-answer evidence.

4. Make the verifier fail unless the model calls the metrics action, reports
   the exact project `total_loc`, names the unit with maximum complexity, and
   uses no forbidden tools.

5. Run focused unit tests and full pytest. Expected: all pass without starting
   vLLM.

6. Run the real offline proof with the current endpoint if healthy, otherwise
   auto-start vLLM with the locally cached model and a memory-safe context. Keep
   the successful artifact path.

7. Commit the proof harness and documentation with the repository commit
   policy.

### Task 6: Prepare and verify release 2.0.1

**Files:**

- Modify: `pyproject.toml`
- Modify: `delphi_lsp/_version.py`
- Modify: `README.md`
- Test: `tests/test_package_metadata.py`

1. Change metadata tests first to require `2.0.1`, then run them and observe
   the expected `2.0.0` failure.

2. Update both version sources and README version text to `2.0.1`. Reinstall
   the editable package and rerun metadata tests.

3. Run fresh verification on the exact release tree:

```bash
.venv/bin/python -m pytest
rm -rf build dist *.egg-info
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
.venv/bin/python -m venv output/release-smoke-venv
output/release-smoke-venv/bin/python -m pip install dist/*.whl
output/release-smoke-venv/bin/python -c "import delphi_lsp; assert delphi_lsp.__version__ == '2.0.1'"
git diff --check
```

4. Review every changed file against the design, scan for secrets and forbidden
   artifacts, stage only release source/tests/docs, run the commit-policy staged
   checker, and commit the release metadata.

5. Push the feature branch, fast-forward `main`, rerun the decisive full test
   and package checks on merged `main`, then push `main`.

6. Wait for GitHub CI on the exact main commit to pass. Create and push the
   annotated tag `v2.0.1` after confirming it does not already exist.

7. Query PyPI immediately before upload and confirm 2.0.1 is absent. Upload only
   the checked `dist` artifacts with Twine and keyring-backed credentials; never
   print credentials.

8. Poll the PyPI JSON API until 2.0.1 is visible. In a new external temporary
   venv, install `python-delphi-lsp==2.0.1` from PyPI without local find-links,
   verify `delphi_lsp.__version__`, import and call the public metrics API, and
   execute `delphi-lsp-agent view --help`.

9. Close Beads issue `mac-358a` only after GitHub, tag, PyPI, and fresh-install
   evidence all agree on the same commit/version.
