import tempfile
import textwrap
import unittest
from pathlib import Path

from delphiast.project_indexer import ProjectIndexer, ProjectProblemType


class ProjectIndexerTests(unittest.TestCase):
    def test_indexes_project_units_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Main.dpr').write_text(
                textwrap.dedent(
                    '''
                    program Main;

                    uses
                      UnitA in 'UnitA.pas';

                    begin
                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (root / 'UnitA.pas').write_text(
                textwrap.dedent(
                    '''
                    unit UnitA;

                    interface

                    uses
                      UnitB;

                    implementation

                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (root / 'UnitB.pas').write_text(
                textwrap.dedent(
                    '''
                    unit UnitB;

                    interface

                    implementation

                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )

            indexer = ProjectIndexer(search_paths=[str(root)])
            result = indexer.index(str(root / 'Main.dpr'))

            parsed = {item.name for item in result.parsed_units}
            self.assertIn('Main', parsed)
            self.assertIn('UnitA', parsed)
            self.assertIn('UnitB', parsed)
            self.assertFalse(result.not_found_units)

    def test_reports_missing_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Main.dpr').write_text(
                textwrap.dedent(
                    '''
                    program Main;

                    uses
                      MissingUnit;

                    begin
                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )

            indexer = ProjectIndexer(search_paths=[str(root)])
            result = indexer.index(str(root / 'Main.dpr'))

            self.assertIn('MissingUnit', result.not_found_units)
            self.assertTrue(
                any(problem.problem_type == ProjectProblemType.CANT_FIND_FILE for problem in result.problems)
            )

    def test_tracks_include_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Main.pas').write_text(
                textwrap.dedent(
                    '''
                    unit Main;

                    interface
                    {$I 'extra.inc'}

                    implementation

                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (root / 'extra.inc').write_text('const IncludedValue = 1;', encoding='utf-8')

            indexer = ProjectIndexer(search_paths=[str(root)], include_paths=[str(root)])
            result = indexer.index(str(root / 'Main.pas'))

            self.assertTrue(result.include_files)
            include_names = {item.name for item in result.include_files}
            self.assertIn('extra.inc', include_names)

    def test_indexes_utf16_encoded_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = textwrap.dedent(
                '''
                unit Umlauts;

                interface

                procedure ÜbernehmeMindestAbbildung;

                implementation

                procedure ÜbernehmeMindestAbbildung;
                begin
                end;

                end.
                '''
            ).strip()
            (root / 'Umlauts.pas').write_bytes(source.encode('utf-16'))

            indexer = ProjectIndexer(search_paths=[str(root)])
            result = indexer.index(str(root / 'Umlauts.pas'))

            parsed = {item.name for item in result.parsed_units}
            self.assertIn('Umlauts', parsed)
            self.assertFalse(result.problems)


if __name__ == '__main__':
    unittest.main()
