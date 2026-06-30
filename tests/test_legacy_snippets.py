import unittest
from pathlib import Path

from delphiast.parser import parse


SNIPPET_DIR = Path(__file__).resolve().parent / 'fixtures' / 'legacy_snippets'


def _read_text_with_bom(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b'\xff\xfe') or data.startswith(b'\xfe\xff'):
        return data.decode('utf-16')
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('latin-1')


class LegacySnippetTests(unittest.TestCase):
    def test_all_pas_snippets_parse(self) -> None:
        snippets = sorted(SNIPPET_DIR.glob('*.pas'))
        self.assertTrue(snippets, 'no snippet fixtures found')
        for snippet in snippets:
            with self.subTest(snippet=snippet.name):
                text = _read_text_with_bom(snippet)
                result = parse(text, str(snippet), build_semantic=False)
                self.assertIsNotNone(result.root)


if __name__ == '__main__':
    unittest.main()
