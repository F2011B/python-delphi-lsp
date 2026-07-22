# Conditional Outline Range Repair

## Problem

Since 2.0.0, the outline transformer returns the original source whenever it
encounters a compiler directive inside an `end`-terminated block. The cheap
outline model then treats the first nested line-level `end` as the routine end.
Consequently, valid symbols are found but their implementation ranges can be
silently truncated. This is reproducible in mORMot2 routines that choose
`try/finally` versus `begin/end` with compiler directives.

## Decision

Keep the linear outline path and make its block scanner aware of conditional
compiler branches. An active conditional frame records the block stack at its
opening directive and the stack produced by every branch. At `ELSE` or
`ELSEIF`, scanning restarts from the opening stack. At `ENDIF` or `IFEND`, the
branch stacks are merged only when:

- no branch closes a block that existed before the conditional;
- every possible branch has the same normalized block shape; and
- structured-type and statement blocks are not confused.

`begin`, `try`, `case`, and `asm` are equivalent only for the purpose of
matching the number of `end`-terminated statement blocks. Exact block kinds are
retained when all branches agree. Incompatible, malformed, or unterminated
conditional structures retain the current all-or-safe behavior and return the
original source.

Non-conditional compiler directives do not change block nesting and can be
skipped like comments.

## Rejected alternatives

- Pinning 1.1.1 would remove the `delphi_lsp` namespace and Protocol v2 worker
  required by current consumers.
- Full parsing every file would restore ranges but reintroduce multi-million
  line latency and memory problems.
- Guessing the final routine `end` after the outline model runs would duplicate
  parsing logic across the LSP, legacy agent layers, and Protocol v2.

## Validation

Regression tests must reproduce both conditional shapes seen in real Delphi:

1. `try` in one branch and `begin` in the other, followed by nested blocks;
2. compiler directives that do not alter nesting.

The resulting outline must preserve source length and line count, remove body
content, parse successfully, and report the routine range through its outer
`end`. Existing ambiguous conditionals that close a pre-existing block remain
unchanged. The full suite and the existing large-file performance checks must
pass, followed by a differential mORMot2 query against 1.1.1.
