import unittest

from delphi_lsp.parser import parse
from delphi_lsp.preprocessor import PreprocessorOptions


class PreprocessorTests(unittest.TestCase):
    def test_ifopt_long_form(self) -> None:
        text = '''
unit IfOptLongDemo;

interface

{$IFOPT SCOPEDENUMS ON}
const ActiveValue = 1;
{$ELSE}
const ActiveValue = 2;
{$ENDIF}

implementation

end.
'''.strip()
        result = parse(
            text,
            'ifopt_long_demo.pas',
            preprocessor_options=PreprocessorOptions(option_states={'SCOPEDENUMS': True}),
        )
        self.assertIn('ActiveValue = 1', result.preprocessed.text)
        self.assertNotIn('ActiveValue = 2', result.preprocessed.text)

    def test_include_bom_is_ignored(self) -> None:
        text = '''
unit IncludeBomDemo;

interface

{$I bom.inc}

implementation

end.
'''.strip()

        def include_loader(parent_file: str, include_name: str):
            if include_name == 'bom.inc':
                return ('\ufeffconst IncludedValue = 1;\n', 'bom.inc')
            return None

        result = parse(text, 'include_bom_demo.pas', include_loader=include_loader)

        self.assertIn('const IncludedValue = 1;', result.preprocessed.text)
        self.assertNotIn('\ufeff', result.preprocessed.text)

    def test_pushopt_popopt_restores_state(self) -> None:
        text = '''
unit PushPopOptDemo;

interface

{$PUSHOPT}
{$R-}
{$IFOPT R+}
const InsidePush = 1;
{$ELSE}
const InsidePush = 2;
{$ENDIF}
{$POPOPT}

{$IFOPT R+}
const AfterPop = 1;
{$ELSE}
const AfterPop = 2;
{$ENDIF}

implementation

end.
'''.strip()
        result = parse(
            text,
            'push_pop_opt_demo.pas',
            preprocessor_options=PreprocessorOptions(option_states={'R': True}),
        )
        self.assertIn('InsidePush = 2', result.preprocessed.text)
        self.assertIn('AfterPop = 1', result.preprocessed.text)
        self.assertNotIn('InsidePush = 1', result.preprocessed.text)
        self.assertNotIn('AfterPop = 2', result.preprocessed.text)

    def test_opt_and_inline_auto(self) -> None:
        text = '''
unit OptDirectiveDemo;

interface

{$OPT R-}
{$INLINE AUTO}

{$IFOPT R+}
const RangeChecks = 1;
{$ELSE}
const RangeChecks = 2;
{$ENDIF}

{$IFOPT INLINE AUTO}
const InlineMode = 1;
{$ELSE}
const InlineMode = 2;
{$ENDIF}

implementation

end.
'''.strip()
        result = parse(text, 'opt_directive_demo.pas')
        self.assertIn('RangeChecks = 2', result.preprocessed.text)
        self.assertIn('InlineMode = 1', result.preprocessed.text)
        self.assertNotIn('RangeChecks = 1', result.preprocessed.text)
        self.assertNotIn('InlineMode = 2', result.preprocessed.text)

    def test_modern_delphi_defaults_enable_conditional_expressions(self) -> None:
        text = '''
unit ModernCompilerDefaultsDemo;

interface

{$IFDEF CONDITIONALEXPRESSIONS}
const Supported = 1;
{$ELSE}
Unsupported Compiler Version
{$ENDIF}

implementation

end.
'''.strip()

        result = parse(text, 'modern_compiler_defaults_demo.pas')

        self.assertIn('Supported = 1', result.preprocessed.text)
        self.assertNotIn('Unsupported Compiler Version', result.preprocessed.text)

    def test_modern_delphi_defaults_define_compiler_version_symbol(self) -> None:
        text = '''
unit ModernCompilerVersionSymbolDemo;

interface

{$IFDEF VER360}
const Supported = 1;
{$ELSE}
Unsupported Compiler Version
{$ENDIF}

implementation

end.
'''.strip()

        result = parse(text, 'modern_compiler_version_symbol_demo.pas')

        self.assertIn('Supported = 1', result.preprocessed.text)
        self.assertNotIn('Unsupported Compiler Version', result.preprocessed.text)

    def test_modern_delphi_defaults_include_unicode(self) -> None:
        text = '''
unit ModernUnicodeDefaultsDemo;

interface

{$IFDEF UNICODE}
type TText = string;
{$ELSE}
type TText = AnsiString;
{$ENDIF}

implementation

end.
'''.strip()

        result = parse(text, 'modern_unicode_defaults_demo.pas')

        self.assertIn('TText = string', result.preprocessed.text)
        self.assertNotIn('TText = AnsiString', result.preprocessed.text)


if __name__ == '__main__':
    unittest.main()
