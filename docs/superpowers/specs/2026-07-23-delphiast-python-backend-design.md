# DelphiAST Python Backend Design

## Goal

Replace the pathological Lark deep-parse path with a pure-Python,
DelphiAST-compatible lexer and recursive-descent parser. Make that backend the
default execution path, retain Lark as an explicit compatibility fallback, and
keep every CLI command responsive on the pinned 4,021,192-line FPC corpus.

## Evidence and acceptance baseline

The pinned corpus is `fpc/FPCSource` at
`a8e7ad4e2f2f6d3bdc240850075d85e659a42ff8`. Its verified workspace contains
4,021,192 `.pas/.inc` lines in 8,549 files and 4,421 discovered units.

The current Lark parser does not finish
`compiler/aarch64/aasmcpu.pas` within 60 seconds. Native DelphiAST parses the
same file in 6 ms and all 4,421 units in 9.955 seconds after applying the FPC
`TStringStreamHelper.GetDataString` recursion fix. Native DelphiAST reports
2,248 syntax errors, which is expected because the corpus mixes many FPC
dialects and platform branches. The native executable and compiler artifacts
remain external test evidence and are never shipped.

Release acceptance requires:

- the isolated `aasmcpu.pas` Python parse to finish below one second;
- all 4,421 corpus units to complete the tolerant deep-parse pass without a
  per-file or transport timeout;
- cache prewarm to remain below 60 seconds on this Mac;
- repeated `open`, `find`, `focus`, and `inspect` requests to stay below one
  second after warmup;
- `trace` and `metrics` to complete or return bounded partial results rather
  than hanging;
- the complete existing test suite and package checks to pass.

## Architecture

### Backend boundary

`DelphiParser` owns preprocessing and public `ParseResult` construction. It
delegates syntax construction through a small backend protocol:

- `delphiast`: the default pure-Python lexer and recursive-descent parser;
- `lark`: the existing parser, retained for explicit compatibility;
- `hybrid`: DelphiAST first with a bounded Lark fallback when strict parsing is
  requested and the fast parser reports an unsupported construct.

The default public backend is `delphiast`. Bulk operations (`metrics`,
relations, project indexing, and cache workers) request tolerant parsing and
never invoke an unbounded Lark fallback. Callers can select `lark` explicitly
for exact legacy behavior while migration is in progress.

### Pure-Python port

The port follows the MPL-2.0 DelphiAST/SimpleParser organization without
copying native binaries:

- `delphiast_tokens.py`: immutable token records and token kinds;
- `delphiast_lexer.py`: a linear scanner for identifiers, escaped identifiers,
  numbers, strings, comments, compiler directives, operators, and assembler
  regions;
- `delphiast_parser.py`: a tolerant recursive-descent parser that creates the
  package's existing `SyntaxNode` classes and `SyntaxNodeType` values;
- `parser_backend.py`: backend selection, strict/tolerant policy, and fallback
  reporting.

Preprocessing remains in the existing Python `Preprocessor`, so include
loading, conditional symbols, source mapping, and comments stay compatible.
The parser uses iterative synchronization at `;`, section keywords, and
balanced block terminators. A malformed unit yields a partial tree plus
structured parser problems instead of consuming unbounded time.

### Compatibility strategy

The first port covers the structures required by semantic indexing, metrics,
and relations:

- units/programs/packages and interface/implementation sections;
- uses/contains/requires clauses;
- constants, variables, type declarations, fields, properties, parameters,
  and routine declarations;
- class, record, interface, helper, generic, and procedural types;
- begin/end blocks and control-flow statements;
- expression structure needed for assignments, calls, references, and
  cyclomatic metrics.

Differential tests compare stable XML-like tree projections against the legacy
backend. Unsupported strict cases may use the bounded legacy fallback; tolerant
bulk parsing records a problem and preserves the partial tree.

### Large-workspace request path

The cache daemon stores a short revision-check deadline after a successful
scan. Requests inside that interval reuse the known revision, avoiding an
O(file-count) scan for every warm command. Standalone parser behavior remains
immediately consistent.

Socket connect timeout and response timeout are separated. Connecting remains
short; a valid long-running first request gets a bounded, substantially longer
response deadline. Deep operations expose partial problem records and never
silently turn a timeout into `cache_error:unavailable`.

The default daemon budget becomes 1 GiB. The existing inclusive 80% warning and
compaction behavior remain unchanged.

## Errors and observability

Parser problems contain only sanitized relative paths, line, column, category,
and a bounded message. Backend name, fallback count, files parsed, parse-error
count, and elapsed seconds are included in cache status and benchmark reports.
No source text, absolute external path, or authentication token is logged.

## Licensing

The Python port is distributed under MPL-2.0, matching both this package and
DelphiAST. Ported files include an attribution header naming DelphiAST and its
upstream repository. The native benchmark patch and compiled artifacts are not
part of the wheel or source distribution.

## Release preparation

Before asking for publication approval, run focused parser tests, the full
suite, formatting/static checks, wheel/sdist build, `twine check`, manifest
inspection, and the pinned 4M-LOC benchmark. Publishing to PyPI or GitHub
requires a separate explicit user approval.
