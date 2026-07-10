import pathlib
import unittest
from unittest.mock import patch

from delphiast.parser import DelphiParser
from delphiast.semantic import ReferenceKind, SymbolKind
from delphiast import workspace
from delphiast.workspace import build_workspace_semantics


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


class WorkspaceTests(unittest.TestCase):
    def test_cross_unit_resolution(self) -> None:
        sources = {
            'unit_math.pas': (FIXTURE_DIR / 'unit_math.pas').read_text(encoding='utf-8'),
            'unit_consumer.pas': (FIXTURE_DIR / 'unit_consumer.pas').read_text(encoding='utf-8'),
        }
        result = build_workspace_semantics(sources)
        consumer = result.models['unit_consumer.pas']
        resolved_calls = [
            ref
            for ref in consumer.references
            if ref.name == 'Add'
            and ref.kind == ReferenceKind.CALL
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.FUNCTION
        ]
        self.assertTrue(resolved_calls)

    def test_build_from_roots_matches_source_build_without_reparsing(self) -> None:
        sources = {
            'unit_math.pas': (FIXTURE_DIR / 'unit_math.pas').read_text(encoding='utf-8'),
            'unit_consumer.pas': (FIXTURE_DIR / 'unit_consumer.pas').read_text(encoding='utf-8'),
        }
        parser = DelphiParser()
        roots = {
            file_name: parser.parse(text, file_name, build_semantic=False).root
            for file_name, text in sources.items()
        }

        with patch.object(DelphiParser, 'parse', side_effect=AssertionError('unexpected reparse')):
            from_roots = workspace.build_workspace_semantics_from_roots(roots)
        from_sources = build_workspace_semantics(sources)

        root_calls = [
            ref
            for ref in from_roots.models['unit_consumer.pas'].references
            if ref.kind == ReferenceKind.CALL and ref.resolved is not None
        ]
        source_calls = [
            ref
            for ref in from_sources.models['unit_consumer.pas'].references
            if ref.kind == ReferenceKind.CALL and ref.resolved is not None
        ]
        self.assertEqual(
            [(ref.name, ref.resolved.name, ref.resolved.kind) for ref in root_calls],
            [(ref.name, ref.resolved.name, ref.resolved.kind) for ref in source_calls],
        )
        self.assertEqual(
            sorted(symbol.name for symbol in from_roots.index.lookup('Add')),
            sorted(symbol.name for symbol in from_sources.index.lookup('Add')),
        )

    def test_source_build_parses_each_source_once(self) -> None:
        sources = {
            'unit_math.pas': (FIXTURE_DIR / 'unit_math.pas').read_text(encoding='utf-8'),
            'unit_consumer.pas': (FIXTURE_DIR / 'unit_consumer.pas').read_text(encoding='utf-8'),
        }
        calls: list[str] = []
        original_parse = DelphiParser.parse

        def counting_parse(parser, text, file_name='', *args, **kwargs):
            calls.append(file_name)
            return original_parse(parser, text, file_name, *args, **kwargs)

        with patch.object(DelphiParser, 'parse', new=counting_parse):
            build_workspace_semantics(sources)

        self.assertCountEqual(calls, sources)


if __name__ == '__main__':
    unittest.main()
