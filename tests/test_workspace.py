import pathlib
import unittest

from delphiast.semantic import ReferenceKind, SymbolKind
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


if __name__ == '__main__':
    unittest.main()
