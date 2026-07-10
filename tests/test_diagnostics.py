import pathlib
import unittest

from delphi_lsp.parser import parse


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


class DiagnosticTests(unittest.TestCase):
    def test_reports_unresolved_units_and_types(self) -> None:
        text = (FIXTURE_DIR / 'unit_unresolved.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_unresolved.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        problems = result.semantic.problems
        messages = [problem.message for problem in problems]
        self.assertTrue(any('Unresolved unit MissingUnit' in msg for msg in messages))
        self.assertTrue(any('Unresolved type TMissingBase' in msg for msg in messages))
        self.assertTrue(any('Unresolved type UnknownType' in msg for msg in messages))


if __name__ == '__main__':
    unittest.main()
