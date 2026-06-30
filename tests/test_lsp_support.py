import pathlib
import unittest

from delphiast.lsp_server import find_reference_at_position
from delphiast.parser import parse
from delphiast.semantic import SymbolKind


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


def _position_for(text: str, needle: str, *, offset: int = 0) -> tuple[int, int]:
    index = text.index(needle) + offset
    line = text.count('\n', 0, index)
    last_nl = text.rfind('\n', 0, index)
    col = index if last_nl < 0 else index - last_nl - 1
    return line, col


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


if __name__ == '__main__':
    unittest.main()
