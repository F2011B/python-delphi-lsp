import pathlib
import json
import subprocess
import sys
import tempfile
import time
import unittest

from delphiast.lsp_server import LspWorkspaceState, find_reference_at_position, iter_symbols
from delphiast.parser import parse
from delphiast.semantic import SymbolKind


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


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


class LspSupportTests(unittest.TestCase):
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
            [sys.executable, '-m', 'delphiast.lsp_server'],
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
            [sys.executable, '-m', 'delphiast.lsp_server'],
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
            [sys.executable, '-m', 'delphiast.lsp_server'],
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
            [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
                [sys.executable, '-m', 'delphiast.lsp_server'],
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
