# Architecture Metrics Design

## Goal

Extend `python-delphi-lsp` with deterministic, queryable architecture metrics
for every Delphi unit and aggregate line metrics for the selected project.
Expose the same calculations through the Python API, the agent CLI, and the
Protocol v2 OpenCode integration. Publish the feature as patch release 2.0.1
only after a real local OpenCode run against vLLM Ornith proves that the model
can retrieve and reason about the metrics without raw source tools.

## Scope

The release includes:

- physical and source line metrics per unit;
- symbol counts and routine-level cyclomatic complexity;
- Halstead metrics and a normalized maintainability index;
- afferent/efferent coupling, instability, abstractness, and distance from the
  main sequence;
- project aggregates, including total LOC with and without include files;
- an additive Protocol v2 `metrics` action;
- a `metrics` layer for `delphi-lsp-agent view`;
- generated OpenCode schema and skill guidance for metric queries;
- a reproducible vLLM Ornith/OpenCode acceptance probe.

The implementation does not add a persistent database, mutate analyzed source,
change existing Protocol v2 response envelopes, or expose additional raw source
text to OpenCode.

## Components

### Public metrics engine

`delphi_lsp/metrics.py` owns immutable result types and source-level
calculations. It is independent of the agent protocol and can be imported by
normal Python consumers. The public entry points calculate one unit and combine
unit analyses into a project result.

### Agent adapter

`delphi_lsp/agent_metrics.py` reads the active `AgentWorkspace`, supplies stable
unit IDs and display paths, builds the project dependency graph, and serializes
compact metric cards. It caches the complete project result by
`workspace_revision`; a changed source automatically invalidates the cache.

### Existing surfaces

- `delphi_lsp.agent_context` dispatches the new `metrics` action and preserves
  the existing schema, pagination, focus, and context-budget envelope.
- `delphi_lsp.agent_layers` renders the same engine output as JSON or Markdown
  for `view --layer metrics`.
- `delphi_lsp.agent_templates` adds `metrics` to the generated OpenCode tool
  schema and teaches the navigator skill when to call it.

## Line Metrics

Physical lines are classified with a Delphi-aware lexical scan that preserves
multi-line comment state and recognizes compiler directives separately.

- `total_lines`: all physical source lines; an empty file has zero lines.
- `source_lines`: lines containing declarative or executable Delphi tokens.
- `blank_lines`: whitespace-only lines.
- `comment_only_lines`: lines containing comments but no code or directive.
- `comment_lines`: all lines containing any comment, including mixed lines.
- `directive_lines`: lines containing Delphi compiler directives.

The categories used for `source_lines`, `blank_lines`, and
`comment_only_lines` are exclusive. `comment_lines` is intentionally an
additional, non-exclusive observation so mixed code/comment lines remain
visible.

Project `total_loc` is the sum of unique `.dpr`, `.dpk`, and `.pas` files in
the selected project. Unique `.inc` files are reported as `include_loc`.
`total_loc_with_includes` is their sum. No file is counted twice.

## Cyclomatic Complexity

Each routine begins at complexity 1. The analyzer adds one for every decision
node in that routine:

- `if`, including an `if` in an `else if` chain;
- `for`, `while`, and `repeat` loops;
- each non-`else` `case` branch;
- each typed exception handler and one untyped `except` branch.

Boolean operators do not add decision points because Delphi permits both
boolean and bitwise meanings and short-circuit behavior depends on compiler
settings. Unit output contains routine count, total, average, maximum, and a
stable descending list of the most complex routines. Project complexity is the
sum of routine complexities across unique project sources.

## Halstead Metrics

The lexical analyzer excludes whitespace, comments, and compiler directives.
Delphi keywords, punctuation, and symbolic operators are operators.
Identifiers and literals are operands. Identifiers and keywords are compared
case-insensitively; literal spellings remain distinct.

For unique operator/operand counts `n1` and `n2`, and total counts `N1` and
`N2`, the result reports:

- vocabulary `n = n1 + n2`;
- length `N = N1 + N2`;
- calculated length `n1*log2(n1) + n2*log2(n2)`;
- volume `V = N*log2(n)`;
- difficulty `D = (n1/2)*(N2/n2)`;
- effort `E = D*V`;
- estimated time `E/18` seconds;
- estimated defects `V/3000`.

Zero denominators produce zero rather than non-finite JSON values. Project
Halstead values are recalculated from the union and totals of project tokens,
not averaged from rounded unit results.

## Maintainability Index

The normalized 0-100 maintainability index deliberately omits the optional
comment bonus, whose variants make cross-tool results difficult to compare:

```text
MI = clamp(
  (171 - 5.2*ln(max(V, 1))
       - 0.23*G
       - 16.2*ln(max(SLOC, 1))) * 100/171,
  0,
  100
)
```

`V` is Halstead volume, `G` is total cyclomatic complexity, and `SLOC` is
`source_lines`. An empty source returns 100. Project MI is calculated from
project aggregates rather than from the mean of unit indexes.

## Coupling and Main-Sequence Metrics

Dependency names come from project, package, interface, and implementation
dependency clauses. Self-dependencies are ignored and names are deduplicated
case-insensitively.

- afferent coupling (`Ca`, fan-in) is the number of other selected-project
  sources that directly depend on the unit;
- efferent coupling (`Ce`, fan-out) is the number of distinct dependencies of
  the unit, including external dependencies;
- internal and external dependency names and counts are reported separately;
- instability is `Ce/(Ca+Ce)`, or zero when both values are zero;
- abstractness is `(interfaces + abstract classes) / class-like types`, or zero
  when no class-like types exist;
- distance is `abs(abstractness + instability - 1)`.

All ratios are finite values in the range 0-1.

## Protocol Contract

`metrics` is an additive member of `SUPPORTED_ACTIONS`; Protocol v2 and the
existing response envelope remain unchanged.

- With no `query` or `target_id`, the first item is the project metric summary,
  followed by paginated unit summary cards.
- `query` filters units case-insensitively by unit name or stable display path.
- A unit `target_id` returned by `open` selects exactly that unit.
- `detail: "summary"` returns headline metrics.
- `detail: "members"` additionally returns routine complexity entries,
  dependency names, complete Halstead fields, and symbol counts.
- Other details are rejected for `metrics` with a specific protocol error.

Responses remain subject to `max_items`, `max_chars`, cursor fingerprints, and
workspace revision checks. Metric results never contain source fragments.

## CLI Contract

`delphi-lsp-agent view --layer metrics` renders project and per-unit summary
metrics. `--query` filters units. `--format json` returns stable machine-readable
objects; Markdown displays the same values for humans. Metrics force the
project dependency index needed for coupling calculations, so callers do not
need a separate deep-index flag.

## Error Handling

Unreadable unit or include files become metric problems attached to the
project result; readable units still produce metrics. A missing unit target is
`target_not_found`. A metrics request without a selected project is
`project_required`. JSON output never contains NaN or infinity.

## Verification

Automated tests cover exact line classifications, compiler directives,
multi-line comments, routine complexity decisions, Halstead formulas,
maintainability edge cases, coupling and main-sequence ratios, project LOC
deduplication, protocol validation/pagination, CLI rendering, generated
OpenCode schema, and cache invalidation.

The real-model gate generates a deterministic Delphi project with multiple
units and known dependency/complexity differences. OpenCode runs the restricted
`vllm-delphi-codebase` agent against the local Ornith vLLM endpoint. The probe
requires successful `skill`, `open`, and `metrics` tool calls, forbids raw
source/search/shell tools, and verifies that the final answer identifies the
highest-complexity unit and reports the exact project LOC returned by the tool.

## Release Gate

Release 2.0.1 requires, on the exact release commit:

1. full pytest suite;
2. package build and `twine check`;
3. wheel smoke install;
4. real OpenCode/vLLM Ornith metrics proof with retained artifact;
5. clean diff and staged-artifact checks;
6. commit and tag pushed to GitHub;
7. successful GitHub CI for the release commit;
8. PyPI upload of version 2.0.1;
9. fresh isolated installation from PyPI with Python API, CLI, and version
   verification.
