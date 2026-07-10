import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from delphi_lsp.lsp_server import outline_source
from delphi_lsp.parser import DelphiParser
from delphi_lsp.project_indexer import ProjectIndexer, ProjectProblemType


class ProjectIndexerTests(unittest.TestCase):
    def test_source_transform_receives_normalized_newlines_for_supported_encodings(self) -> None:
        source = 'unit Newlines;\ninterface\nimplementation\nend.\n'
        encoded_sources = {
            'utf-8': source.replace('\n', '\r\n').encode('utf-8'),
            'utf-8-bom': source.replace('\n', '\r').encode('utf-8-sig'),
            'utf-16': source.replace('\n', '\r\n').encode('utf-16'),
            'latin-1': source.replace('Newlines', 'Néwlines').replace('\n', '\r').encode('latin-1'),
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for encoding, encoded_source in encoded_sources.items():
                with self.subTest(encoding=encoding):
                    source_path = root / f'{encoding}.pas'
                    source_path.write_bytes(encoded_source)
                    transform_inputs: list[str] = []

                    ProjectIndexer(
                        source_transform=lambda text: transform_inputs.append(text) or text,
                    ).index(str(source_path))

                    expected = source
                    if encoding == 'latin-1':
                        expected = source.replace('Newlines', 'Néwlines')
                    self.assertEqual(transform_inputs, [expected])

    def test_source_transform_runs_after_read_and_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'Ordered.pas'
            source_path.write_text('unused', encoding='utf-8')
            source = 'unit Ordered; interface implementation end.'
            transformed = '// transformed\n' + source
            events: list[str] = []
            original_parse = DelphiParser.parse

            def read_source(_path: Path) -> str:
                events.append('read')
                return source

            def transform(text: str) -> str:
                self.assertEqual(text, source)
                events.append('transform')
                return transformed

            def recording_parse(parser: DelphiParser, text: str, file_name: str, **kwargs):
                self.assertEqual(text, transformed)
                events.append('parse')
                return original_parse(parser, text, file_name, **kwargs)

            with (
                mock.patch('delphi_lsp.project_indexer.read_source_text', side_effect=read_source),
                mock.patch.object(DelphiParser, 'parse', autospec=True, side_effect=recording_parse),
            ):
                result = ProjectIndexer(source_transform=transform).index(str(source_path))

            self.assertEqual(events, ['read', 'transform', 'parse'])
            self.assertEqual([item.name for item in result.parsed_units], ['Ordered'])

    def test_source_transform_exception_is_a_parse_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'BrokenTransform.pas'
            source_path.write_text(
                'unit BrokenTransform; interface implementation end.',
                encoding='utf-8',
            )
            parsed_hook_calls: list[str] = []

            def fail_transform(_text: str) -> str:
                raise RuntimeError('transform failed')

            indexer = ProjectIndexer(
                source_transform=fail_transform,
                on_unit_parsed=lambda name, path, tree, from_parser: parsed_hook_calls.append(name) or False,
            )
            with mock.patch.object(DelphiParser, 'parse') as parser_parse:
                result = indexer.index(str(source_path))

            parser_parse.assert_not_called()
            self.assertEqual(len(result.problems), 1)
            self.assertEqual(result.problems[0].problem_type, ProjectProblemType.CANT_PARSE_FILE)
            self.assertEqual(result.problems[0].file_name, str(source_path.resolve()))
            self.assertEqual(result.problems[0].description, 'transform failed')
            self.assertEqual(len(result.parsed_units), 1)
            self.assertTrue(result.parsed_units[0].has_error)
            self.assertEqual(result.parsed_units[0].error_info.error, 'transform failed')
            self.assertEqual(parsed_hook_calls, [])

    def test_source_transform_is_not_called_when_syntax_hook_supplies_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'Hooked.pas'
            source = 'unit Hooked; interface implementation end.'
            source_path.write_text(source, encoding='utf-8')
            syntax_tree = DelphiParser().parse(source, str(source_path)).root
            transform_calls: list[str] = []
            parsed_from_parser: list[bool] = []

            indexer = ProjectIndexer(
                source_transform=lambda text: transform_calls.append(text) or text,
                on_get_unit_syntax=lambda path: (syntax_tree, True, False),
                on_unit_parsed=lambda name, path, tree, from_parser: (
                    parsed_from_parser.append(from_parser) or False
                ),
            )
            result = indexer.index(str(source_path))

            self.assertEqual(transform_calls, [])
            self.assertEqual(parsed_from_parser, [False])
            self.assertEqual([item.name for item in result.parsed_units], ['Hooked'])

    def test_source_transform_is_not_called_when_syntax_hook_skips_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / 'Skipped.pas'
            source_path.write_text('unit Skipped; interface implementation end.', encoding='utf-8')
            transform_calls: list[str] = []

            indexer = ProjectIndexer(
                source_transform=lambda text: transform_calls.append(text) or text,
                on_get_unit_syntax=lambda path: (None, False, False),
            )
            result = indexer.index(str(source_path))

            self.assertEqual(transform_calls, [])
            self.assertEqual(result.parsed_units, [])
            self.assertEqual(result.problems, [])

    def test_default_parser_receives_original_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = 'unit Original; interface implementation end.'
            source_path = root / 'Original.pas'
            source_path.write_text(source, encoding='utf-8')
            parsed_sources: list[str] = []
            original_parse = DelphiParser.parse

            def recording_parse(parser: DelphiParser, text: str, file_name: str, **kwargs):
                parsed_sources.append(text)
                return original_parse(parser, text, file_name, **kwargs)

            with mock.patch.object(DelphiParser, 'parse', autospec=True, side_effect=recording_parse):
                ProjectIndexer().index(str(source_path))

            self.assertEqual(parsed_sources, [source])

    def test_source_transform_runs_once_for_every_small_and_large_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_source = textwrap.dedent(
                '''
                program Main;
                uses UnitA in 'UnitA.pas';
                begin
                end.
                '''
            ).strip()
            large_body = '\n'.join(f'  // filler {index}' for index in range(2_000))
            unit_source = textwrap.dedent(
                f'''
                unit UnitA;
                interface
                implementation
                procedure LargeRoutine;
                begin
                {large_body}
                end;
                end.
                '''
            ).strip()
            (root / 'Main.dpr').write_text(project_source, encoding='utf-8')
            (root / 'UnitA.pas').write_text(unit_source, encoding='utf-8')
            transform_inputs: list[str] = []
            parsed_sources: list[str] = []
            original_parse = DelphiParser.parse

            def transform(text: str) -> str:
                transform_inputs.append(text)
                return '// transformed\n' + text

            def recording_parse(parser: DelphiParser, text: str, file_name: str, **kwargs):
                parsed_sources.append(text)
                return original_parse(parser, text, file_name, **kwargs)

            indexer = ProjectIndexer(source_transform=transform)
            with mock.patch.object(DelphiParser, 'parse', autospec=True, side_effect=recording_parse):
                result = indexer.index(str(root / 'Main.dpr'))

            self.assertEqual(transform_inputs, [project_source, unit_source])
            self.assertLess(project_source.count('\n'), 10)
            self.assertGreater(unit_source.count('\n'), 1_000)
            self.assertEqual(parsed_sources, ['// transformed\n' + text for text in transform_inputs])
            self.assertEqual({item.name for item in result.parsed_units}, {'Main', 'UnitA'})

    def test_outline_transformed_project_keeps_dependencies_and_interface_include(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Bundle.dpk').write_text(
                textwrap.dedent(
                    '''
                    package Bundle;
                    contains
                      UnitA in 'UnitA.pas';
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
                    uses UnitB;
                    {$I 'api.inc'}
                    procedure PublicApi;
                    implementation
                    procedure PublicApi;
                    begin
                      UnitB.DoThing;
                    end;
                    end.
                    '''
                ).strip(),
                encoding='utf-8',
            )
            (root / 'UnitB.pas').write_text(
                'unit UnitB; interface procedure DoThing; implementation end.',
                encoding='utf-8',
            )
            (root / 'api.inc').write_text('const IncludedValue = 1;', encoding='utf-8')

            indexer = ProjectIndexer(
                search_paths=[str(root)],
                include_paths=[str(root)],
                source_transform=outline_source,
            )
            result = indexer.index(str(root / 'Bundle.dpk'))

            self.assertEqual(
                {item.name for item in result.parsed_units},
                {'Bundle', 'UnitA', 'UnitB'},
            )
            self.assertEqual({item.name for item in result.include_files}, {'api.inc'})
            self.assertFalse(result.not_found_units)

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
