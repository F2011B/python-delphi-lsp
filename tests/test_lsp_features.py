import pathlib
import tempfile
import unittest

from delphiast.lsp_server import (
    LspWorkspaceState,
    WorkspaceConfig,
    build_outline_semantic_model,
    extract_completion_base,
    find_symbol_at_position,
    hover_text,
    iter_member_symbols,
    iter_symbols,
    resolve_base_for_member_completion,
    resolve_reference,
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

    def test_hover_text_resolves_symbol_declaration_position(self) -> None:
        text = (FIXTURE_DIR / 'unit_inheritance.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_inheritance.pas', build_semantic=True)
        model = result.semantic
        self.assertIsNotNone(model)

        symbol = find_symbol_at_position(model, line=5, character=4)

        self.assertIsNotNone(symbol)
        self.assertEqual(symbol.name, 'TBase')
        self.assertIn('class TBase', hover_text(symbol))

    def test_outline_model_keeps_type_section_after_record_end(self) -> None:
        text = '''unit OutlineTypeSection;

interface

type
  TFirst = record
    Value: Integer;
  end;

  TWithMethod = class
  public
    procedure Run;
  end;

  TSecond = class
  end;

implementation

end.
'''
        model = build_outline_semantic_model(text, 'OutlineTypeSection.pas')

        names = {symbol.name for symbol in iter_symbols(model.unit_scope)}

        self.assertIn('TFirst', names)
        self.assertIn('TWithMethod', names)
        self.assertIn('TSecond', names)

    def test_workspace_config_indexing(self) -> None:
        state = LspWorkspaceState()
        config = WorkspaceConfig(roots=[str(FIXTURE_DIR)], eager_index=True)
        state.configure(config)
        self.assertTrue(state.workspace_files)

    def test_semantic_for_uri_uses_indexed_workspace_file(self) -> None:
        source_path = FIXTURE_DIR / 'unit_inheritance.pas'
        state = LspWorkspaceState()
        state.configure(WorkspaceConfig(roots=[str(FIXTURE_DIR)], eager_index=True))

        semantic = state.semantic_for_uri(source_path.absolute().as_uri())

        self.assertIsNotNone(semantic)
        self.assertEqual(semantic.unit_scope.name, 'UnitInheritance')

    def test_semantic_for_uri_reads_unindexed_workspace_file(self) -> None:
        source_path = FIXTURE_DIR / 'unit_inheritance.pas'
        state = LspWorkspaceState()

        semantic = state.semantic_for_uri(source_path.absolute().as_uri())

        self.assertIsNotNone(semantic)
        self.assertEqual(semantic.unit_scope.name, 'UnitInheritance')

    def test_structure_semantic_for_uri_reads_small_file_without_size_gate(self) -> None:
        source_path = FIXTURE_DIR / 'unit_inheritance.pas'
        state = LspWorkspaceState()

        semantic = state.structure_semantic_for_uri(source_path.absolute().as_uri())

        self.assertIsNotNone(semantic)
        names = {symbol.name for symbol in iter_symbols(semantic.unit_scope)}
        self.assertIn('TBase', names)
        self.assertIn('TChild', names)

    def test_workspace_config_reads_bom_encoded_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = pathlib.Path(temp_dir) / 'Utf16Unit.pas'
            source_path.write_bytes('unit Utf16Unit; interface implementation end.'.encode('utf-16'))
            state = LspWorkspaceState()
            state.configure(WorkspaceConfig(roots=[temp_dir], eager_index=True))
            self.assertIn(str(source_path), state.file_cache)
            self.assertEqual(state.file_cache[str(source_path)].text[:4], 'unit')


if __name__ == '__main__':
    unittest.main()
