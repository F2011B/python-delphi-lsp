# FPC overview performance benchmark

This benchmark answers one product question:

> On an Intel Core i5 laptop with 16 GB RAM and Windows 11, can
> `delphi-lsp-agent view --layer overview` process a repository with
> 3,000,000 physical Object Pascal lines within 60 seconds?

The corpus is the coherent Free Pascal compiler source tree pinned in
`tests/corpora.performance.lock.json`. Corpus files and reports stay outside
the repository and are never included in a distribution.

## Prepare the corpus

```console
.venv/bin/python scripts/prepare_fpc_corpus.py \
  --target-lines 3000000 \
  --workspace /tmp/fpc-3m \
  --fetch
```

Preparation clones or reuses the exact locked revision, selects anchors
followed by a deterministic path-sorted prefix of `.pas` and `.inc` files,
hard-links or copies those files into the external workspace, and verifies
every line count and SHA-256 before writing `corpus-manifest.json`. An existing
non-empty workspace is never cleared implicitly.

Use `--offline` after the revision is cached. An existing clean checkout at
the locked revision can be supplied with `--checkout PATH`.

## Run the benchmark

```console
.venv/bin/python scripts/benchmark_fpc_corpus.py \
  --workspace /tmp/fpc-3m \
  --sizes 250000,500000,1000000,2000000,3000000 \
  --workers 0,1,2,4 \
  --output /tmp/fpc-report.json
```

The default run performs:

- cold and warm overview calls in separate Python processes;
- five deterministic scaling slices and a log-log scaling fit;
- a 1M-line worker sweep;
- import-floor and phase diagnostics;
- a full recovery-oriented DelphiAST compatibility audit.

`--skip-warm`, `--no-parse-audit`, `--no-worker-sweep`, and
`--no-phase-breakdown` shorten exploratory runs. `--report-only` writes all
failures but exits successfully. The release budgets are configurable with
`--budget-cold`, `--budget-warm`, `--budget-rss-gib`,
`--min-parse-ratio`, and `--max-scaling-exponent`.

The opt-in pytest integration probe uses the smallest slice:

```console
DELPHI_LSP_FPC_CORPUS=/tmp/fpc-3m \
  .venv/bin/python -m pytest -q -m perf \
  tests/test_fpc_performance_corpus.py
```

Normal pytest runs exclude the `perf` marker and require neither network nor
an external corpus.

## Budgets and interpretation

| Gate | Default | Meaning |
|---|---:|---|
| 3M cold overview | 60.0 s | Primary product question |
| 3M warm overview | 5.0 s | Second invocation in a fresh process |
| Peak RSS | 11 GiB | Leaves room on a 16 GB Windows/editor machine |
| Parse success ratio | 0.95 | Parsed cleanly or recovered with reported problems |
| Scaling exponent | 1.15 | Rejects growth that extrapolates poorly |

Cold means the exact slice-local `.delphi-lsp` directory is removed and its
absence verified. Warm means a second fresh process without another removal.
The current `view` path does not write a persistent navigation cache, so its
warm result benefits only from the operating system's filesystem cache.

The overview command performs project/source discovery, source reads,
conditional outline processing, outline semantic-model construction, unit
scope registration, and overview payload construction. It does not full-parse
units or traverse complete reference relations. The full parser audit is
therefore measured and reported separately. It selects recovery-oriented
DelphiAST tolerant mode so every recoverable FPC problem remains countable;
the report states that mode and that the small-file Lark fallback is disabled
for this audit.

`parse_success_ratio` counts both clean files and files that produced a
recoverable syntax/preprocessor problem. Always read it together with
`files_with_problems`, `problem_types`, and `common_problem_descriptions`.

## Measured result, 2026-07-28

The report was produced at
`/tmp/python-delphi-lsp-fpc-report.json` from FPC revision
`a8e7ad4e2f2f6d3bdc240850075d85e659a42ff8` on Darwin arm64, Python
3.14.3, 10 logical CPUs, and 32 GiB RAM. These are not target-hardware
measurements.

| Requested slice | Files | Physical lines | Cold | Warm | Peak RSS | Parse success |
|---:|---:|---:|---:|---:|---:|---:|
| 250k | 268 | 290,162 | 1.074 s | 1.121 s | 64.2 MiB | 100% |
| 500k | 584 | 500,068 | 1.816 s | 1.787 s | 95.7 MiB | 100% |
| 1M | 2,505 | 1,000,095 | 4.610 s | 4.140 s | 187.6 MiB | 100% |
| 2M | 6,007 | 2,000,205 | 9.015 s | 8.818 s | 422.7 MiB | 100% |
| 3M | 7,364 | 3,000,891 | 13.608 s | 12.819 s | 538.3 MiB | 100% |

The cold scaling exponent was **1.1054**, classified as **n_log_n**
(`R² = 0.9966`). This passes the 1.15 scaling gate. The run's overall status
was **fail** because the 3M fresh-process warm result exceeded its budget. The
measured cold 60-second target, RSS, parse-ratio, and scaling gates passed.
Cold throughput declined from 270,231 lines/s on the smallest slice to
220,529 lines/s at 3M; exact cold and warm throughput is present on every
slice in the JSON report.

The 3M parser audit processed 3,000,891 physical and 4,957,004
post-preprocessing lines in 22.112 seconds with eight workers. All 7,364 files
returned a recoverable result, but only 1,171 were problem-free; 6,193 carried
problems. The largest categories were 20,686 syntax and 2,533 unresolved
include problems. This is why the success ratio must not be read in isolation.

## Largest-slice phase profile

| Phase | Seconds |
|---|---:|
| Discovery | 1.497 |
| Read-only source probe | 0.619 |
| Conditional outline, parse, and model construction | 15.785 |
| Semantic index assembly | 0.199 |
| Full relation traversal | not used |
| Overview payload | 0.000009 |
| Total index diagnostic | 17.491 |

Outline workers combine reads, conditional preprocessing, outline parsing,
and model construction, so the read number is a separate diagnostic pass and
is not subtracted from the measured outline time.

## Worker sweep at 1M lines

| Workers | Cold | Speedup vs. one worker | Peak RSS |
|---:|---:|---:|---:|
| default/0 | 6.707 s | 2.261× | 186.7 MiB |
| 1 | 15.165 s | 1.000× | 221.1 MiB |
| 2 | 9.206 s | 1.647× | 185.3 MiB |
| 4 | 9.350 s | 1.622× | 188.2 MiB |

The automatic/default worker count was fastest on the measured machine.
Windows uses spawned workers too, but the optimum must be measured on the
target laptop.

## Verdict and findings

On the measured machine the answer is **yes**: the 3M cold overview completed
in 13.608 seconds. Applying the explicitly assumed, unverified 4× slowdown
projects 54.431 seconds on the Windows i5 target, within the 60-second budget.
That projection is not a Windows measurement, so the target-hardware question
remains projected rather than proven.

Important findings:

- `view` creates no persistent `.delphi-lsp` cache
  (`delphi_lsp/agent_cli.py:313-321`), so the second fresh-process invocation
  still rebuilds the whole outline and misses the 5-second warm budget.
- Outline/preprocessing/model work dominates the largest slice
  (`delphi_lsp/agent_layers.py:76-127`); overview payload construction is
  negligible.
- Discovery indexed all 7,364 manifest source files and skipped none. `.pp`
  files are excluded because the product task filter accepts `.pas`, `.inc`,
  `.dpr`, and `.dpk` (`delphi_lsp/agent_layers.py:76-81`).
- Recovery parsing exposes broad FPC-dialect incompatibility despite a 100%
  recoverable-result ratio: 84.1% of files report at least one problem
  (`delphi_lsp/parser.py:88-127`).
- The strict small-file Lark branch
  (`delphi_lsp/parser.py:99-108`) is not the overview path and proved
  unsuitable for a bounded full-corpus compatibility audit; the report uses
  and labels DelphiAST tolerant mode instead.
- The parent-process RSS measurement reuses the existing Windows PSAPI
  implementation but cannot aggregate spawned-worker RSS
  (`delphi_lsp/parallel_outline.py:123-181`).
