# Conditional Outline Range Repair

## Problem

Since 2.0.0, the outline transformer returns the original source whenever it
encounters a compiler directive inside an `end`-terminated block. The cheap
outline model then treats the first nested line-level `end` as the routine end.
Consequently, valid symbols are found but their implementation ranges can be
silently truncated. This is reproducible in mORMot2 routines that choose
`try/finally` versus `begin/end` with compiler directives.

## Decision

Keep the linear outline path and add two complementary safeguards.

Semantic callers pass their effective Delphi defines to the outline builder.
The existing preprocessor selects the active conditional branch while replacing
inactive code with spaces. The transformation restores the source's original
line endings and rejects any result whose length or line layout changed. Active
non-conditional directives, including includes, remain available to project
indexing. Delphi multiline strings are consumed as single literals so directive
text inside them is never evaluated.

Callers without an effective define set, such as the project inventory pass,
keep all branches. Its linear block scanner records the block stack at each
opening directive and the stack produced by every branch. At `ELSE` or
`ELSEIF`, scanning restarts from the opening stack. At `ENDIF` or `IFEND`, the
branch stacks are merged only when:

- no branch closes a block that existed before the conditional;
- every possible branch has the same normalized block shape; and
- structured-type and statement blocks are not confused.

`begin`, `try`, `case`, and `asm` are equivalent only for the purpose of
matching the number of `end`-terminated statement blocks. Exact block kinds are
retained when all branches agree. Incompatible, malformed, or unterminated
conditional structures preserve only the ambiguous routine body and allow later
unambiguous routines in the file to be compacted.

`build_outline_semantic_model` owns the define-aware transformation, so the LSP,
legacy agent layers, and Protocol v2 cannot accidentally bypass it or transform
the same source twice.

## Rejected alternatives

- Pinning 1.1.1 would remove the `delphi_lsp` namespace and Protocol v2 worker
  required by current consumers.
- Full parsing every file would restore ranges but reintroduce multi-million
  line latency and memory problems.
- Guessing the final routine `end` after the outline model runs would duplicate
  parsing logic across the LSP, legacy agent layers, and Protocol v2.

## Validation

Regression tests cover balanced alternatives, correlated conditionals split
across a routine, active includes, and directive-looking text inside Delphi
multiline strings. The resulting outline must preserve source length and line
count, remove safe body content, parse successfully, and report each routine
through its outer `end`. Ambiguous raw branches remain locally unchanged without
disabling optimization of later routines. The full suite and existing large-file
budgets must pass, followed by a differential mORMot2 query against 1.1.1.
