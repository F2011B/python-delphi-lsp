import pathlib
import json
import subprocess
import sys
import tempfile
import time
import unittest

import delphi_lsp.lsp_server as lsp_server
from delphi_lsp.lsp_server import LspWorkspaceState, find_reference_at_position, iter_symbols
from delphi_lsp.parser import DelphiParser, parse
from delphi_lsp.semantic import SymbolKind


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'
LARGE_FILE_LSP_COLD_START_TIMEOUT_SECONDS_BY_PLATFORM = {
    'linux': 3.0,
    'linux2': 3.0,
    'darwin': 2.0,
    'win32': 3.0,
}
LARGE_FILE_LSP_COLD_START_TIMEOUT_SECONDS = LARGE_FILE_LSP_COLD_START_TIMEOUT_SECONDS_BY_PLATFORM.get(
    sys.platform,
    3.0,
)


# Budgets include cold process startup, initialize, and the 100k-line rename; they
# allow measured platform variance while retaining a regression guard far below 25s.


def _position_for(text: str, needle: str, *, offset: int = 0) -> tuple[int, int]:
    index = text.index(needle) + offset
    line = text.count('\n', 0, index)
    last_nl = text.rfind('\n', 0, index)
    col = index if last_nl < 0 else index - last_nl - 1
    return line, col


def _send_lsp_message(proc: subprocess.Popen, message: dict) -> None:
    data = json.dumps(message).encode('utf-8')
    assert proc.stdin is not None
    proc.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode('ascii'))
    proc.stdin.write(data)
    proc.stdin.flush()


def _receive_lsp_message(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    headers = b''
    while b'\r\n\r\n' not in headers:
        chunk = proc.stdout.read(1)
        if not chunk:
            raise AssertionError('language server closed stdout before response')
        headers += chunk
    length = None
    for line in headers.decode('ascii').split('\r\n'):
        if line.lower().startswith('content-length:'):
            length = int(line.split(':', 1)[1].strip())
    if length is None:
        raise AssertionError('language server response did not include Content-Length')
    return json.loads(proc.stdout.read(length))


def _request_lsp(proc: subprocess.Popen, message: dict) -> dict:
    _send_lsp_message(proc, message)
    expected_id = message.get('id')
    if expected_id is None:
        return {}
    while True:
        response = _receive_lsp_message(proc)
        if response.get('id') == expected_id:
            return response


def _flatten_document_symbols(items: list[dict]) -> list[dict]:
    flattened: list[dict] = []
    for item in items:
        flattened.append(item)
        flattened.extend(_flatten_document_symbols(item.get('children') or []))
    return flattened


def _generated_mega_unit_source(
    *,
    proc_count: int = 2500,
    statements_per_proc: int = 40,
) -> str:
    lines = [
        'unit Mega100kUnit;',
        '',
        'interface',
        '',
        'type',
        '  TMegaValue = Integer;',
        '',
        'implementation',
        '',
    ]
    for index in range(1, proc_count + 1):
        lines.append(f'procedure MegaProc{index:05d};')
        lines.append('var')
        lines.append('  Value: Integer;')
        lines.append('begin')
        lines.append('  Value := 0;')
        for statement in range(1, statements_per_proc + 1):
            lines.append(f'  Value := Value + {statement};')
        lines.append('end;')
        lines.append('')
    lines.append('end.')
    return '\n'.join(lines) + '\n'


def _outline_source(text: str) -> str:
    transform = getattr(lsp_server, 'outline_source', None)
    assert transform is not None, 'lsp_server.outline_source must be public'
    return transform(text)


class LspSupportTests(unittest.TestCase):
    def test_outline_source_always_blanks_a_one_line_program_body(self) -> None:
        source = 'program P; begin DoWork; end.'

        transformed = _outline_source(source)

        self.assertNotIn('DoWork', transformed)
        self.assertEqual(len(transformed), len(source))
        parse(transformed, 'one_line.dpr')

    def test_outline_large_source_keeps_existing_threshold_policy(self) -> None:
        one_line = 'program P; begin DoWork; end.'
        multiline = 'program P;\nbegin\n  DoWork;\nend.\n'

        self.assertEqual(lsp_server.outline_large_source(one_line, 1), one_line)
        self.assertEqual(lsp_server.outline_large_source(multiline, 10), multiline)
        self.assertEqual(
            lsp_server.outline_large_source(multiline, 1),
            _outline_source(multiline),
        )

    def test_outline_source_tracks_nested_case_until_the_outer_end(self) -> None:
        source = '''program NestedCase;
var X: Integer;
begin
  case X of
    0:
      begin
        X := 1;
      end;
  else
    X := 2;
  end;
  asm
    NOP
  end;
  X := 3;
end.
'''

        transformed = _outline_source(source)

        self.assertNotIn('X := 3', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        self.assertEqual(len(transformed), len(source))
        parse(transformed, 'nested_case.dpr')

    def test_outline_source_tracks_nested_try_except_until_the_outer_end(self) -> None:
        source = '''program NestedExcept;
begin
  try
    begin
      DoTryWork;
    end;
  except
    HandleFailure;
  end;
  DoAfterExcept;
end.
'''

        transformed = _outline_source(source)

        self.assertNotIn('DoAfterExcept', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'nested_except.dpr')

    def test_outline_source_tracks_nested_try_finally_until_the_outer_end(self) -> None:
        source = '''program NestedFinally;
begin
  try
    DoTryWork;
  finally
    begin
      Cleanup;
    end;
  end;
  DoAfterFinally;
end.
'''

        transformed = _outline_source(source)

        self.assertNotIn('DoAfterFinally', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'nested_finally.dpr')

    def test_outline_source_preserves_keyword_text_in_strings_and_comments(self) -> None:
        source = '''program ProtectedKeywords;
const
  Keywords = 'begin case try asm end';
  // begin case try asm end remains a line comment
  Value = 1;
{ begin case try asm end remains a brace comment }
(* begin case try asm end remains a block comment *)
begin
  DoWork;
end.
'''

        transformed = _outline_source(source)

        self.assertIn("'begin case try asm end'", transformed)
        self.assertIn('// begin case try asm end remains a line comment\n', transformed)
        self.assertIn('{ begin case try asm end remains a brace comment }', transformed)
        self.assertIn('(* begin case try asm end remains a block comment *)', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'protected_keywords.dpr')

    def test_outline_source_returns_original_for_ifdef_else_body_reproducer(self) -> None:
        source = '''program ConditionalOutline;
var Value: Integer;
procedure Earlier;
begin
  Value := -1;
end;
begin
  if Value = 0 then
  begin
{$IFDEF X}
    Value := 1;
  end;
{$ELSE}
    Value := 2;
  end;
{$ENDIF}
  Value := 3;
end.
'''

        transformed = _outline_source(source)

        self.assertEqual(transformed, source)
        self.assertIn('Value := -1', transformed)
        for defines in ((), ('X',)):
            with self.subTest(defines=defines):
                DelphiParser(defines=defines).parse(source, 'conditional_outline.dpr')

    def test_outline_source_returns_original_for_paren_star_body_directive(self) -> None:
        source = '''program ParenDirective;
var Value: Integer;
begin
(*$IFDEF X*)
  Value := 1;
(*$ELSE*)
  Value := 2;
(*$ENDIF*)
end.
'''

        self.assertEqual(_outline_source(source), source)
        for defines in ((), ('X',)):
            with self.subTest(defines=defines):
                DelphiParser(defines=defines).parse(source, 'paren_directive.dpr')

    def test_outline_source_ignores_dollar_text_in_ordinary_comments(self) -> None:
        source = '''program DollarComments;
{ ordinary comment with $IFDEF X text }
(* ordinary comment with $IFDEF X text *)
// ordinary comment with {$IFDEF X} text
begin
  DoWork;
end.
'''

        transformed = _outline_source(source)

        self.assertNotEqual(transformed, source)
        self.assertNotIn('DoWork', transformed)
        self.assertIn('{ ordinary comment with $IFDEF X text }', transformed)
        self.assertIn('(* ordinary comment with $IFDEF X text *)', transformed)
        self.assertIn('// ordinary comment with {$IFDEF X} text\n', transformed)
        parse(transformed, 'dollar_comments.dpr')

    def test_outline_source_optimizes_body_after_outside_directives(self) -> None:
        source = '''program OutsideDirective;
{$IFDEF X}
const Selected = 1;
{$ELSE}
const Selected = 2;
{$ENDIF}
begin
  DoWork;
end.
'''

        transformed = _outline_source(source)

        self.assertNotEqual(transformed, source)
        self.assertNotIn('DoWork', transformed)
        self.assertIn('{$IFDEF X}', transformed)
        for defines in ((), ('X',)):
            with self.subTest(defines=defines):
                DelphiParser(defines=defines).parse(transformed, 'outside_directive.dpr')

    def test_outline_source_tracks_inline_structured_types_and_variant_case(self) -> None:
        source = '''program InlineTypes;
begin
  var C: class
    class procedure Build;
  end;
  var O: object end;
  var I: interface end;
  var D: dispinterface end;
  var R: record
    Value: Integer;
    case Integer of
      0: (A: Integer);
      1: (B: Integer);
  end;
  var K: class of TObject;
  var Callback: procedure of object;
  R.Value := 1;
end.
'''

        transformed = _outline_source(source)

        self.assertNotIn('R.Value := 1', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'inline_types.dpr')

    def test_outline_source_ignores_constraint_keyword_in_inline_generic_class(self) -> None:
        source = '''program InlineGenericClass;
begin
  var GenericClass: class<T: class>
    procedure Execute;
  end;
  GenericClass := nil;
end.
'''

        parse(source, 'inline_generic_class.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('GenericClass := nil', transformed)
        parse(transformed, 'inline_generic_class.dpr')

    def test_outline_source_ignores_nested_generic_routine_constraints(self) -> None:
        source = '''program GenericRoutineConstraints;
begin
  var Host: object
    procedure RunClass<T: class>;
    procedure RunRecord<T: record>;
    procedure RunNested<T: IFoo<IBar<TItem>>; TItem: interface>;
  end;
  Host := Host;
end.
'''

        parse(source, 'generic_routine_constraints.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('Host := Host', transformed)
        parse(transformed, 'generic_routine_constraints.dpr')

    def test_outline_source_does_not_treat_comparisons_as_generic_angles(self) -> None:
        source = '''program ComparisonAngles;
var A, B, C, D: Integer;
begin
  if A < B then
    case A of
      0: A := 1;
    end;
  if C > D then
    C := D;
  if A <= B then
    A := B;
  if C >= D then
    C := D;
  if A <> D then
    A := D;
  DoAfterComparisons;
end.
'''

        parse(source, 'comparison_angles.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('DoAfterComparisons', transformed)
        parse(transformed, 'comparison_angles.dpr')

    def test_outline_source_ignores_escaped_keyword_identifiers_in_body(self) -> None:
        source = '''program EscapedIdentifiers;
var
  &end, &case, &try, &class, &object: Integer;
begin
  &end := 1;
  &case := &end;
  &try := &case;
  &class := &try;
  &object := &class;
end.
'''

        parse(source, 'escaped_identifiers.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('&end := 1', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'escaped_identifiers.dpr')

    def test_outline_source_ignores_escaped_keyword_identifiers_in_asm(self) -> None:
        source = '''program EscapedAsmIdentifiers;
procedure Run;
asm
  MOV &end, &case
  XOR &try, &class
  MOV &object, &end
end;
begin
  Run;
end.
'''

        parse(source, 'escaped_asm_identifiers.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('MOV &end', transformed)
        self.assertEqual(transformed.count('\n'), source.count('\n'))
        parse(transformed, 'escaped_asm_identifiers.dpr')

    def test_outline_source_tracks_anonymous_class_with_routine_first_member(self) -> None:
        source = '''program AnonymousClasses;
begin
  var ProcedureFirst: class
    procedure Run;
  end;
  var ClassProcedureFirst: class
    class procedure Build;
  end;
  ProcedureFirst := nil;
end.
'''

        parse(source, 'anonymous_classes.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('ProcedureFirst := nil', transformed)
        parse(transformed, 'anonymous_classes.dpr')

    def test_outline_source_distinguishes_object_type_from_of_object_proc_type(self) -> None:
        source = '''program ObjectTypeContexts;
begin
  var EmptyClass: class end;
  var EmptyObject: object end;
  var NonEmptyObject: object
    procedure Run;
  end;
  var Objects: array[0..1] of object
    procedure Execute;
  end;
  var ClassRef: class of TObject;
  var Holder: class
    ProcCallback: procedure(Sender: TObject; Value: Integer) of object; cdecl;
    FuncCallback: function(Sender: TObject): Boolean of object; stdcall;
  end;
  Objects[0] := nil;
end.
'''

        parse(source, 'object_type_contexts.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('Objects[0] := nil', transformed)
        parse(transformed, 'object_type_contexts.dpr')

    def test_outline_source_tracks_array_of_anonymous_object(self) -> None:
        source = '''program ArrayOfAnonymousObject;
begin
  var Objects: array[0..1] of object
    procedure Execute;
  end;
  Objects[0] := nil;
end.
'''

        parse(source, 'array_of_anonymous_object.dpr')
        transformed = _outline_source(source)

        self.assertNotIn('Objects[0] := nil', transformed)
        parse(transformed, 'array_of_anonymous_object.dpr')

    def test_outline_source_falls_back_for_ambiguous_of_object_context(self) -> None:
        source = '''program AmbiguousObjectContext;
begin
  var ItemFile: file of object end;
  ItemFile := ItemFile;
end.
'''

        parse(source, 'ambiguous_object_context.dpr')

        self.assertEqual(_outline_source(source), source)

    def test_outline_source_preserves_delphi_multiline_string_blocks(self) -> None:
        triple_block = (
            "'''\n"
            "don't  collapse   these\n"
            "&end begin case try class object\n"
            "{$IFDEF X}\n{$ENDIF}\n"
            "'''"
        )
        five_block = (
            "'''''\r\n"
            "don't  collapse   these\r\n"
            "// comment  text and ' apostrophe\r\n"
            "(*$IFDEF X*)\r\n(*$ENDIF*)\r\n"
            "'''''"
        )
        source = (
            "program MultilineBlocks;\n"
            "const\n"
            f"  TripleValue = {triple_block};\n"
            f"  FiveValue = {five_block};\n"
            "begin\n"
            "  DoWork;\n"
            "end.\n"
        )

        for defines in ((), ('X',)):
            with self.subTest(defines=defines, source='original'):
                DelphiParser(defines=defines).parse(source, 'multiline_blocks.dpr')
        transformed = _outline_source(source)

        self.assertIn(triple_block, transformed)
        self.assertIn(five_block, transformed)
        self.assertNotIn('DoWork', transformed)
        for defines in ((), ('X',)):
            with self.subTest(defines=defines, source='transformed'):
                DelphiParser(defines=defines).parse(transformed, 'multiline_blocks.dpr')

    def test_reference_lookup_at_position(self) -> None:
        text = (FIXTURE_DIR / 'unit_inheritance.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_inheritance.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        line, col = _position_for(text, 'Child.Foo', offset=len('Child.'))
        ref = find_reference_at_position(result.semantic, line=line, character=col)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.name, 'Child.Foo')
        self.assertIsNotNone(ref.resolved)
        self.assertEqual(ref.resolved.kind, SymbolKind.PROCEDURE)

    def test_document_symbols_request_returns_symbols_for_file_uri(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        proc = subprocess.Popen(
            [sys.executable, '-m', 'delphi_lsp.lsp_server'],
            cwd=root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            initialize_started_at = time.perf_counter()
            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {'processId': None, 'rootUri': None, 'capabilities': {}},
                },
            )
            self.assertLess(time.perf_counter() - initialize_started_at, 10.0)
            self.assertIn('documentSymbolProvider', response['result']['capabilities'])
            _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'textDocument/documentSymbol',
                    'params': {'textDocument': {'uri': (FIXTURE_DIR / 'unit_inheritance.pas').absolute().as_uri()}},
                },
            )

            self.assertNotIn('error', response)
            names = {item['name'] for item in response['result']}
            self.assertIn('TBase', names)
            self.assertIn('TChild', names)
        finally:
            stderr = ''
            if proc.poll() is None:
                try:
                    _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                    _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr = proc.stderr.read().decode('utf-8', errors='replace')
            self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_large_corpus_files_return_document_symbols(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        targets = [
            (
                root_dir / 'test_projects/github_repos/mORMot2/src/core/mormot.core.base.pas',
                {'PtrInt', 'RawUtf8'},
                1000,
            ),
            (
                root_dir / 'test_projects/github_repos/python4delphi/Source/PythonEngine.pas',
                {'PyObject', 'TPythonEngine'},
                800,
            ),
        ]
        missing = [str(path) for path, _, _ in targets if not path.exists()]
        if missing:
            self.skipTest(f'missing GitHub corpus files: {missing}')

        proc = subprocess.Popen(
            [sys.executable, '-m', 'delphi_lsp.lsp_server'],
            cwd=root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            initialize_started_at = time.perf_counter()
            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {'processId': None, 'rootUri': root_dir.as_uri(), 'capabilities': {}},
                },
            )
            self.assertLess(time.perf_counter() - initialize_started_at, 10.0)
            self.assertIn('documentSymbolProvider', response['result']['capabilities'])
            _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

            for index, (source_path, expected_names, min_symbols) in enumerate(targets, start=2):
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': index,
                        'method': 'textDocument/documentSymbol',
                        'params': {'textDocument': {'uri': source_path.absolute().as_uri()}},
                    },
                )

                self.assertNotIn('error', response)
                symbols = _flatten_document_symbols(response['result'])
                names = {item['name'] for item in symbols}
                self.assertGreaterEqual(len(symbols), min_symbols)
                self.assertTrue(expected_names.issubset(names))
        finally:
            stderr = ''
            if proc.poll() is None:
                try:
                    _request_lsp(proc, {'jsonrpc': '2.0', 'id': 4, 'method': 'shutdown', 'params': None})
                    _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr = proc.stderr.read().decode('utf-8', errors='replace')
            self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_lsp_definition_resolves_generic_constructor_call(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source_path = FIXTURE_DIR / 'unit_generics.pas'
        text = source_path.read_text(encoding='utf-8')
        call_line, call_col = _position_for(text, 'TBox<string>.Create', offset=len('TBox<string>.'))
        decl_line = text.count('\n', 0, text.index('constructor Create;'))

        proc = subprocess.Popen(
            [sys.executable, '-m', 'delphi_lsp.lsp_server'],
            cwd=root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {'processId': None, 'rootUri': None, 'capabilities': {}},
                },
            )
            self.assertIn('definitionProvider', response['result']['capabilities'])
            _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'textDocument/definition',
                    'params': {
                        'textDocument': {'uri': source_path.absolute().as_uri()},
                        'position': {'line': call_line, 'character': call_col},
                    },
                },
            )

            self.assertNotIn('error', response)
            self.assertEqual(response['result']['uri'], source_path.absolute().as_uri())
            self.assertEqual(response['result']['range']['start']['line'], decl_line)
        finally:
            stderr = ''
            if proc.poll() is None:
                try:
                    _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                    _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr = proc.stderr.read().decode('utf-8', errors='replace')
            self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_lsp_document_symbols_cover_modern_delphi_members(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source_path = FIXTURE_DIR / 'unit_advanced.pas'

        proc = subprocess.Popen(
            [sys.executable, '-m', 'delphi_lsp.lsp_server'],
            cwd=root_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 1,
                    'method': 'initialize',
                    'params': {'processId': None, 'rootUri': None, 'capabilities': {}},
                },
            )
            self.assertIn('documentSymbolProvider', response['result']['capabilities'])
            _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

            response = _request_lsp(
                proc,
                {
                    'jsonrpc': '2.0',
                    'id': 2,
                    'method': 'textDocument/documentSymbol',
                    'params': {'textDocument': {'uri': source_path.absolute().as_uri()}},
                },
            )

            self.assertNotIn('error', response)
            names = {item['name'] for item in _flatten_document_symbols(response['result'])}
            self.assertTrue({'IFoo', 'TImpl', 'DoThing', 'Log', 'OnChange'}.issubset(names))
        finally:
            stderr = ''
            if proc.poll() is None:
                try:
                    _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                    _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stderr is not None:
                stderr = proc.stderr.read().decode('utf-8', errors='replace')
            self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_lsp_completion_returns_members_for_generic_variable(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = '''unit LspGenericCompletion;

interface

type
  TItem = class
  end;

  TBox<T: class> = class
  public
    procedure Clear;
    function Map: Integer;
    property Count: Integer read Map;
  end;

procedure UseBox;

implementation

procedure UseBox;
var
  Box: TBox<TItem>;
begin
  Box.Clear;
end;

end.
'''
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-generic-completion-') as temp_dir:
            source_path = pathlib.Path(temp_dir) / 'LspGenericCompletion.pas'
            source_path.write_text(source, encoding='utf-8')
            completion_line, completion_col = _position_for(source, 'Box.', offset=len('Box.'))

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {'processId': None, 'rootUri': pathlib.Path(temp_dir).as_uri(), 'capabilities': {}},
                    },
                )
                self.assertIn('completionProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
                _send_lsp_message(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'method': 'textDocument/didOpen',
                        'params': {
                            'textDocument': {
                                'uri': source_path.as_uri(),
                                'languageId': 'delphi',
                                'version': 1,
                                'text': source,
                            }
                        },
                    },
                )

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/completion',
                        'params': {
                            'textDocument': {'uri': source_path.as_uri()},
                            'position': {'line': completion_line, 'character': completion_col},
                        },
                    },
                )

                self.assertNotIn('error', response)
                labels = {item['label'] for item in response['result']['items']}
                self.assertTrue({'Clear', 'Map', 'Count'}.issubset(labels))
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_file_returns_document_symbols(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('documentSymbolProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/documentSymbol',
                        'params': {'textDocument': {'uri': source_path.as_uri()}},
                    },
                )

                self.assertNotIn('error', response)
                symbols = _flatten_document_symbols(response['result'])
                names = {item['name'] for item in symbols}
                self.assertGreaterEqual(len(symbols), 5_000)
                self.assertIn('TMegaValue', names)
                self.assertIn('MegaProc00001', names)
                self.assertIn('MegaProc02500', names)
                self.assertLess(time.perf_counter() - started_at, 10.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_workspace_symbol_uses_lazy_outline_index(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-workspace-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('workspaceSymbolProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'workspace/symbol',
                        'params': {'query': ' MegaProc02500\n'},
                    },
                )

                self.assertNotIn('error', response)
                self.assertEqual([item['name'] for item in response['result']], ['MegaProc02500'])
                self.assertLess(time.perf_counter() - started_at, 2.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_workspace_symbol_is_fast_in_mixed_workspace(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mixed-workspace-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            (workspace_dir / 'SmallUnit.pas').write_text(
                'unit SmallUnit; interface type TSmall = class end; implementation end.\n',
                encoding='utf-8',
            )

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('workspaceSymbolProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'workspace/symbol',
                        'params': {'query': 'MegaProc02500'},
                    },
                )

                self.assertNotIn('error', response)
                self.assertEqual([item['name'] for item in response['result']], ['MegaProc02500'])
                self.assertLess(time.perf_counter() - started_at, 2.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_workspace_symbol_filters_non_matching_large_files(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        other_source = (
            source
            .replace('Mega100kUnit', 'Other100kUnit')
            .replace('TMegaValue', 'TOtherValue')
            .replace('MegaProc', 'OtherProc')
        )
        self.assertGreater(source.count('\n'), 100_000)
        self.assertNotIn('MegaProc02500', other_source)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-query-filter-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            (workspace_dir / 'Mega100kUnit.pas').write_text(source, encoding='utf-8')
            for index in range(5):
                unit_source = other_source.replace('Other100kUnit', f'Other{index}Unit')
                (workspace_dir / f'Other{index}Unit.pas').write_text(unit_source, encoding='utf-8')

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('workspaceSymbolProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'workspace/symbol',
                        'params': {'query': 'MegaProc02500'},
                    },
                )

                self.assertNotIn('error', response)
                self.assertEqual([item['name'] for item in response['result']], ['MegaProc02500'])
                self.assertLess(time.perf_counter() - started_at, 1.5)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_definition_resolves_body_identifier_quickly(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-definition-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            proc_start = source.index('procedure MegaProc02500;')
            use_text = 'Value := Value + 40;'
            use_start = source.index(use_text, proc_start)
            use_line = source.count('\n', 0, use_start)
            use_col = len('  Value := ')
            decl_line = source.count('\n', 0, source.index('  Value: Integer;', proc_start))

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('definitionProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/definition',
                        'params': {
                            'textDocument': {'uri': source_path.as_uri()},
                            'position': {'line': use_line, 'character': use_col},
                        },
                    },
                )

                self.assertNotIn('error', response)
                self.assertEqual(response['result']['uri'], source_path.as_uri())
                self.assertEqual(response['result']['range']['start']['line'], decl_line)
                self.assertLess(time.perf_counter() - started_at, 2.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_hover_resolves_body_identifier_quickly(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-hover-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            proc_start = source.index('procedure MegaProc02500;')
            use_start = source.index('Value := Value + 40;', proc_start)
            use_line = source.count('\n', 0, use_start)
            use_col = len('  Value := ')

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('hoverProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/hover',
                        'params': {
                            'textDocument': {'uri': source_path.as_uri()},
                            'position': {'line': use_line, 'character': use_col},
                        },
                    },
                )

                self.assertNotIn('error', response)
                self.assertIn('variable Value', response['result']['contents'])
                self.assertLess(time.perf_counter() - started_at, 2.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_references_resolve_body_identifier_quickly(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-references-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            proc_start = source.index('procedure MegaProc02500;')
            use_start = source.index('Value := Value + 40;', proc_start)
            use_line = source.count('\n', 0, use_start)
            use_col = len('  Value := ')
            decl_line = source.count('\n', 0, source.index('  Value: Integer;', proc_start))

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('referencesProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/references',
                        'params': {
                            'textDocument': {'uri': source_path.as_uri()},
                            'position': {'line': use_line, 'character': use_col},
                            'context': {'includeDeclaration': True},
                        },
                    },
                )

                self.assertNotIn('error', response)
                self.assertGreaterEqual(len(response['result']), 40)
                self.assertTrue(
                    any(item['range']['start']['line'] == decl_line for item in response['result'])
                )
                self.assertLess(time.perf_counter() - started_at, 2.0)
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_rename_resolves_body_identifier_quickly(self) -> None:
        root_dir = pathlib.Path(__file__).parents[1]
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-rename-') as temp_dir:
            workspace_dir = pathlib.Path(temp_dir)
            source_path = workspace_dir / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            proc_start = source.index('procedure MegaProc02500;')
            use_start = source.index('Value := Value + 40;', proc_start)
            use_line = source.count('\n', 0, use_start)
            use_col = len('  Value := ')
            decl_line = source.count('\n', 0, source.index('  Value: Integer;', proc_start))

            proc = subprocess.Popen(
                [sys.executable, '-m', 'delphi_lsp.lsp_server'],
                cwd=root_dir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_at = time.perf_counter()
            try:
                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'initialize',
                        'params': {
                            'processId': None,
                            'rootUri': workspace_dir.as_uri(),
                            'capabilities': {},
                        },
                    },
                )
                self.assertIn('renameProvider', response['result']['capabilities'])
                _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

                response = _request_lsp(
                    proc,
                    {
                        'jsonrpc': '2.0',
                        'id': 2,
                        'method': 'textDocument/rename',
                        'params': {
                            'textDocument': {'uri': source_path.as_uri()},
                            'position': {'line': use_line, 'character': use_col},
                            'newName': 'RenamedValue',
                        },
                    },
                )

                self.assertNotIn('error', response)
                edits = response['result']['changes'][source_path.as_uri()]
                self.assertGreaterEqual(len(edits), 40)
                self.assertTrue(
                    any(item['range']['start']['line'] == decl_line for item in edits)
                )
                self.assertLess(
                    time.perf_counter() - started_at,
                    LARGE_FILE_LSP_COLD_START_TIMEOUT_SECONDS,
                )
            finally:
                stderr = ''
                if proc.poll() is None:
                    try:
                        _request_lsp(proc, {'jsonrpc': '2.0', 'id': 3, 'method': 'shutdown', 'params': None})
                        _send_lsp_message(proc, {'jsonrpc': '2.0', 'method': 'exit', 'params': None})
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                if proc.stderr is not None:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                self.assertNotIn('Failed to handle user defined notification "initialize"', stderr)

    def test_generated_100k_line_symbol_model_is_built_quickly(self) -> None:
        source = _generated_mega_unit_source()
        self.assertGreater(source.count('\n'), 100_000)
        with tempfile.TemporaryDirectory(prefix='delphi-lsp-mega-model-') as temp_dir:
            source_path = pathlib.Path(temp_dir) / 'Mega100kUnit.pas'
            source_path.write_text(source, encoding='utf-8')
            state = LspWorkspaceState()

            started_at = time.perf_counter()
            model = state.semantic_for_uri(source_path.as_uri())
            elapsed = time.perf_counter() - started_at

            self.assertIsNotNone(model)
            symbols = list(iter_symbols(model.unit_scope))
            names = {item.name for item in symbols}
            self.assertGreaterEqual(len(symbols), 5_000)
            self.assertIn('TMegaValue', names)
            self.assertIn('MegaProc00001', names)
            self.assertIn('MegaProc02500', names)
            self.assertLess(elapsed, 1.0)


if __name__ == '__main__':
    unittest.main()
