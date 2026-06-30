import pathlib
import tempfile
import unittest

from delphiast.lsp_server import (
    LspWorkspaceState,
    WorkspaceConfig,
    extract_completion_base,
    resolve_reference,
    resolve_base_for_member_completion,
    iter_member_symbols,
)
from delphiast.parser import parse
from delphiast.semantic import SymbolKind


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


class LspFeatureTests(unittest.TestCase):
    def test_completion_base_extraction(self) -> None:
        text = 'Foo.Bar.Baz.'
        base = extract_completion_base(text, 0, len(text))
        self.assertEqual(base, 'Foo.Bar.Baz')

    def test_member_completion_symbols(self) -> None:
        text = (FIXTURE_DIR / 'unit_inheritance.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_inheritance.pas', build_semantic=True)
        model = result.semantic
        self.assertIsNotNone(model)
        base = resolve_base_for_member_completion(model, 'TChild')
        self.assertIsNotNone(base)
        members = list(iter_member_symbols(model, base))
        self.assertTrue(any(member.name == 'Foo' for member in members))

    def test_workspace_config_indexing(self) -> None:
        state = LspWorkspaceState()
        config = WorkspaceConfig(roots=[str(FIXTURE_DIR)])
        state.configure(config)
        self.assertTrue(state.workspace_files)

    def test_workspace_config_reads_bom_encoded_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = pathlib.Path(temp_dir) / 'Utf16Unit.pas'
            source_path.write_bytes('unit Utf16Unit; interface implementation end.'.encode('utf-16'))
            state = LspWorkspaceState()
            state.configure(WorkspaceConfig(roots=[temp_dir]))
            self.assertIn(str(source_path), state.file_cache)
            self.assertEqual(state.file_cache[str(source_path)].text[:4], 'unit')


if __name__ == '__main__':
    unittest.main()
