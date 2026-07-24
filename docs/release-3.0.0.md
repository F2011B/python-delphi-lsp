# Python Delphi LSP 3.0.0

Version 3.0.0 adds a lazy, bounded Code Property Graph and advances the agent
interface to Protocol v3.

## Protocol v3 and CPG

The new `cpg` action accepts an existing unit, type, or routine `target_id`.
The graph selector supports `ast`, `cfg`, `dfg`, `call`, and `full`;
`direction` supports `out`, `in`, and `both`; and `depth` is bounded from 1 to
16. Existing target IDs remain unchanged.

The response combines stable graph nodes and edges for syntax containment,
control flow, local definition/use and reaching-definition flow, and uniquely
resolved explicit calls. Unsupported, ambiguous, or incomplete semantics are
marked `sound_partial` and counted rather than guessed.

Graphs are constructed only after a focused CPG request. Navigation prewarming
does not parse CPG input. One request parses one source, then caches the
detached graph in a revision- and request-keyed LRU. The CPG cache receives 20
percent of the configured retained-memory budget, capped at 128 MiB. It does
not retain source text or complete parser trees.

## Cache startup diagnostics

The daemon now publishes readiness before large workspace discovery and
prewarming. A five-second bootstrap deadline therefore succeeds even while a
four-million-line repository is still warming. Actual bootstrap failures
report timeout or child exit state, exit code, Python executable, workspace,
and a sanitized child-stderr tail instead of only saying that the daemon did
not become ready. Warm-up failures are exposed through cache status and query
errors.

## Four-million-line performance gate

The release candidate was tested on the pinned FPC corpus:

| Measurement | Result |
| --- | ---: |
| Files | 8,549 |
| Physical lines | 4,021,192 |
| Navigation prewarm, 8 workers | 22.62 s |
| First focused CPG | 0.0166 s |
| Cached focused CPG | 0.000069 s |
| CPG cache retained | 4,007 bytes |
| Sources parsed for CPG | one source |
| Legacy throughput after CPG | 98.43 percent of control |

The release floor permits at most a five-percent legacy-throughput regression;
the measured 1.57-percent difference passes. The benchmark also proves that
prewarm creates no CPG state and that a repeated request performs no new parse.

Run the same gate with:

```bash
python scripts/benchmark_cpg.py \
  --root /path/to/corpus \
  --query Run \
  --workers 8 \
  --output cpg-4m.json
```

The benchmark can create a deterministic corpus with `--generate` when a
verified four-million-line workspace is unavailable.
