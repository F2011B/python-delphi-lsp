import pathlib
import tempfile
import unittest

from delphiast.consts import AttributeName, SyntaxNodeType
from delphiast.parser import parse


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


class ParserTests(unittest.TestCase):
    def test_parse_fixtures(self) -> None:
        fixtures = sorted(FIXTURE_DIR.glob('*.*'))
        self.assertTrue(fixtures, 'no fixtures found')
        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                text = fixture.read_text(encoding='utf-8')
                result = parse(text, fixture.name, build_semantic=True)
                self.assertTrue(result.root.child_nodes)
                unit_name = result.root.get_attribute(AttributeName.anName)
                self.assertTrue(unit_name)

    def test_sample_unit_structure(self) -> None:
        text = (FIXTURE_DIR / 'unit_basic.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_basic.pas', build_semantic=True)
        node_types = {child.typ for child in result.root.child_nodes}
        self.assertIn(SyntaxNodeType.ntInterface, node_types)
        self.assertIn(SyntaxNodeType.ntImplementation, node_types)
        self.assertIsNotNone(result.semantic)
        self.assertEqual(result.semantic.unit_scope.name, 'UnitBasic')

    def test_interface_only_mode(self) -> None:
        text = '''
unit BrokenImpl;

interface

procedure Foo;

implementation

procedure Foo;
begin
  Result := ;
end;

end.
'''.strip()
        with self.assertRaises(Exception):
            parse(text, 'broken_impl.pas', build_semantic=False)

        result = parse(text, 'broken_impl.pas', build_semantic=False, interface_only=True)
        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'BrokenImpl')
        self.assertIsNotNone(result.root.find_node(SyntaxNodeType.ntInterface))

    def test_include_loader_forwarding(self) -> None:
        calls: list[tuple[str, str]] = []

        def include_loader(parent_file: str, include_name: str):
            calls.append((parent_file, include_name))
            if include_name.casefold() == 'extra.inc':
                return ('const IncludedFromLoader = 42;', '/virtual/extra.inc')
            return None

        text = '''
unit IncludeLoaderDemo;

interface
{$I 'extra.inc'}

implementation

end.
'''.strip()
        result = parse(text, 'include_loader_demo.pas', include_loader=include_loader)
        self.assertTrue(calls)
        self.assertIn('IncludedFromLoader', result.preprocessed.text)

    def test_backslash_relative_include_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            src_dir = root / 'src' / 'core'
            src_dir.mkdir(parents=True)
            (root / 'src' / 'version.inc').write_text("'2.4.15507'", encoding='utf-8')
            source_path = src_dir / 'VersionDemo.pas'
            text = '''
unit VersionDemo;

interface

const
  VersionText = {$I ..\\version.inc};

implementation

end.
'''.strip()
            source_path.write_text(text, encoding='utf-8')

            result = parse(text, str(source_path))

            self.assertIn("VersionText = '2.4.15507'", result.preprocessed.text)

    def test_missing_include_inside_const_expression_keeps_semantic_parse(self) -> None:
        text = '''
unit MissingIncludeConstDemo;

interface

const
  VersionText = {$I ..\\missing-version.inc};
  NextValue = 42;

implementation

end.
'''.strip()

        result = parse(text, 'MissingIncludeConstDemo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'MissingIncludeConstDemo')
        self.assertIsNotNone(result.semantic)
        self.assertTrue(result.semantic.index.lookup('VersionText'))
        self.assertTrue(result.semantic.index.lookup('NextValue'))
        self.assertEqual(result.preprocessed.problems[0].kind, 'include')
        self.assertIn('include not found: ..\\missing-version.inc', result.preprocessed.problems[0].message)
        self.assertIn('VersionText =', result.preprocessed.text)
        self.assertIn('NextValue = 42', result.preprocessed.text)

    def test_pointer_type_alias(self) -> None:
        text = '''
unit PointerAliasDemo;

interface

type
  TThing = record
    Value: Integer;
  end;
  PThing = ^TThing;

implementation

end.
'''.strip()

        result = parse(text, 'pointer_alias_demo.pas')

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'PointerAliasDemo')

    def test_pointer_to_single_letter_generic_type_parameter(self) -> None:
        text = '''
unit PointerGenericTypeParameterDemo;

interface

type
  TEnumerator<T> = record
  public
    type
      PT = ^T;
    function CurrentPtr: PT;
  end;

implementation

procedure UsePointer<T>;
var
  P: ^T;
begin
end;

end.
'''.strip()

        result = parse(text, 'pointer_generic_type_parameter_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'PointerGenericTypeParameterDemo')
        self.assertTrue(result.semantic.index.lookup('TEnumerator'))

    def test_forward_class_without_heritage(self) -> None:
        text = '''
unit ForwardClassDemo;

interface

type
  TFoo = class;
  TBar = class(TFoo)
  end;

implementation

end.
'''.strip()

        result = parse(text, 'forward_class_demo.pas')

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ForwardClassDemo')

    def test_forward_interface_declaration(self) -> None:
        text = '''
unit ForwardInterfaceDemo;

interface

type
  IFoo = interface;
  IBar = interface(IFoo)
  end;

implementation

end.
'''.strip()

        result = parse(text, 'forward_interface_demo.pas')

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ForwardInterfaceDemo')

    def test_abstract_forward_class_with_heritage(self) -> None:
        text = '''
unit AbstractForwardClassDemo;

interface

type
  TBase = class
  end;
  TDerived = class abstract(TBase);

implementation

end.
'''.strip()

        result = parse(text, 'abstract_forward_class_demo.pas')

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'AbstractForwardClassDemo')

    def test_distinct_type_alias(self) -> None:
        text = '''
unit DistinctTypeAliasDemo;

interface

type
  RawUtf8 = string;
  SpiUtf8 = type RawUtf8;

implementation

end.
'''.strip()

        result = parse(text, 'distinct_type_alias_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'DistinctTypeAliasDemo')
        symbols = result.semantic.index.lookup('SpiUtf8')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].type_ref.display_name(), 'RawUtf8')

    def test_codepage_string_type_alias(self) -> None:
        text = '''
unit CodepageStringAliasDemo;

interface

const
  CP_WINANSI = 1252;

type
  WinAnsiString = type AnsiString(CP_WINANSI);

implementation

end.
'''.strip()

        result = parse(text, 'codepage_string_alias_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'CodepageStringAliasDemo')
        symbols = result.semantic.index.lookup('WinAnsiString')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].type_ref.display_name(), 'AnsiString')

    def test_array_bound_constant_expression(self) -> None:
        text = '''
unit ArrayBoundExpressionDemo;

interface

type
  PUtf8Char = ^Char;
  TPUtf8CharArray = array[0..MaxInt div SizeOf(PUtf8Char) - 1] of PUtf8Char;

implementation

end.
'''.strip()

        result = parse(text, 'array_bound_expression_demo.pas', build_semantic=True)

        symbols = result.semantic.index.lookup('TPUtf8CharArray')
        self.assertTrue(symbols)
        self.assertEqual(
            symbols[0].type_ref.display_name(),
            'array[0..MaxInt div SizeOf(PUtf8Char) - 1] of PUtf8Char',
        )

    def test_on_handle_string_transform(self) -> None:
        text = '''
unit UnitCamelCase;

interface

type
  TSample = class
  end;

implementation

end.
'''.strip()
        result = parse(text, 'unit_camel_case.pas', on_handle_string=lambda s: s.lower())
        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'unitcamelcase')

    def test_if_expression_operator(self) -> None:
        text = '''
unit IfExpressionDemo;

interface

implementation

procedure Demo;
var
  Value: Integer;
begin
  Value := if 10 > 5 then 1 else 2;
end;

end.
'''.strip()
        result = parse(text, 'if_expression_demo.pas', build_semantic=False)
        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'IfExpressionDemo')

    def test_is_not_and_not_in_operators(self) -> None:
        text = '''
unit RelationalOperatorsDemo;

interface

implementation

procedure Demo(const Obj: TObject; Number: Integer);
begin
  if Obj is not TObject then
    Exit;
  if Number not in [1, 2, 3] then
    Exit;
end;

end.
'''.strip()
        result = parse(text, 'relational_operators_demo.pas', build_semantic=False)
        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RelationalOperatorsDemo')

    def test_not_equal_without_left_spacing(self) -> None:
        text = '''
unit NoSpaceNotEqualDemo;

interface

procedure CheckChar;

implementation

procedure CheckChar;
var
  Ch: Char;
begin
  if Ch<> '}' then
    exit;
end;

end.
'''.strip()
        result = parse(text, 'no_space_not_equal_demo.pas', build_semantic=True)
        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'NoSpaceNotEqualDemo')
        self.assertTrue(result.semantic.index.lookup('CheckChar'))

    def test_noreturn_and_winapi_directives(self) -> None:
        text = '''
unit DirectiveDemo;

interface

procedure Fatal; noreturn;
procedure Callback; winapi;

implementation

procedure Fatal;
begin
end;

procedure Callback;
begin
end;

end.
'''.strip()
        result = parse(text, 'directive_demo.pas', build_semantic=False)
        interface_node = result.root.find_node(SyntaxNodeType.ntInterface)
        self.assertIsNotNone(interface_node)
        methods = [node for node in interface_node.child_nodes if node.typ == SyntaxNodeType.ntMethod]
        self.assertEqual(len(methods), 2)
        method_by_name = {node.get_attribute(AttributeName.anName): node for node in methods}
        self.assertEqual(method_by_name['Fatal'].get_attribute(AttributeName.anNoReturn), 'true')
        self.assertEqual(method_by_name['Callback'].get_attribute(AttributeName.anCallingConvention), 'winapi')

    def test_const_ref_parameter_decorator(self) -> None:
        text = '''
unit ParameterDecoratorDemo;

interface

procedure Process(const [Ref] Value: string);

implementation

procedure Process(const [Ref] Value: string);
begin
end;

end.
'''.strip()
        result = parse(text, 'parameter_decorator_demo.pas', build_semantic=False)
        interface_node = result.root.find_node(SyntaxNodeType.ntInterface)
        self.assertIsNotNone(interface_node)
        method_node = next(
            node for node in interface_node.child_nodes
            if node.typ == SyntaxNodeType.ntMethod and node.get_attribute(AttributeName.anName) == 'Process'
        )
        params = method_node.find_node(SyntaxNodeType.ntParameters)
        self.assertIsNotNone(params)
        param = next(node for node in params.child_nodes if node.typ == SyntaxNodeType.ntParameter)
        attrs = param.find_node(SyntaxNodeType.ntAttributes)
        self.assertIsNotNone(attrs)
        attr_names = [
            child.get_attribute(AttributeName.anName)
            for child in attrs.child_nodes
            if child.typ == SyntaxNodeType.ntAttribute
        ]
        self.assertIn('Ref', attr_names)

    def test_untyped_var_parameter(self) -> None:
        text = '''
unit UntypedVarParameterDemo;

interface

procedure DynArrayFakeDelete(var Values; Index, Last, ValueSize: PtrUInt);

implementation

procedure DynArrayFakeDelete(var Values; Index, Last, ValueSize: PtrUInt);
begin
end;

end.
'''.strip()

        result = parse(text, 'untyped_var_parameter_demo.pas', build_semantic=True)

        symbols = result.semantic.index.lookup('DynArrayFakeDelete')
        self.assertTrue(symbols)
        interface_node = result.root.find_node(SyntaxNodeType.ntInterface)
        method_node = next(
            node for node in interface_node.child_nodes
            if node.typ == SyntaxNodeType.ntMethod and node.get_attribute(AttributeName.anName) == 'DynArrayFakeDelete'
        )
        params = method_node.find_node(SyntaxNodeType.ntParameters)
        values_param = next(
            node for node in params.child_nodes
            if node.typ == SyntaxNodeType.ntParameter and node.child_nodes[0].value == 'Values'
        )
        self.assertEqual(values_param.get_attribute(AttributeName.anKind), 'var')
        self.assertIsNone(values_param.find_node(SyntaxNodeType.ntType))

    def test_nested_variant_record_with_named_tag_field(self) -> None:
        text = '''
unit NestedVariantRecordDemo;

interface

type
  TSynVarData = packed record
    case Integer of
      0: (
        VType: Cardinal;
        case padding: Cardinal of
          varInteger: (VInteger: Integer);
          varDouble: (VDouble: Double);
      );
      1: (
        Data: Pointer);
  end;

implementation

end.
'''.strip()

        result = parse(text, 'nested_variant_record_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'NestedVariantRecordDemo')
        self.assertTrue(result.semantic.index.lookup('TSynVarData'))

    def test_record_constant_semicolon_fields(self) -> None:
        text = '''
unit RecordConstantSemicolonDemo;

interface

type
  TVarData = record
    VType: Integer;
    VInteger: Integer;
  end;

const
  varBoolean = 11;
  TrueVarData: TVarData = (VType: varBoolean; VInteger: -1);

implementation

end.
'''.strip()

        result = parse(text, 'record_constant_semicolon_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RecordConstantSemicolonDemo')
        self.assertTrue(result.semantic.index.lookup('TrueVarData'))

    def test_empty_case_selector(self) -> None:
        text = '''
unit EmptyCaseSelectorDemo;

interface

procedure Process(Value: Integer);

implementation

procedure Process(Value: Integer);
begin
  case Value of
    0:
      ;
    1:
      Process(0);
  end;
end;

end.
'''.strip()

        result = parse(text, 'empty_case_selector_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyCaseSelectorDemo')
        self.assertTrue(result.semantic.index.lookup('Process'))

    def test_empty_label_statement_before_block_end(self) -> None:
        text = '''
unit EmptyLabelStatementDemo;

interface

function First: Integer;
function Second: Integer;

implementation

function First: Integer;
label
  quit;
begin
  Result := 1;
quit:
end;

function Second: Integer;
begin
  Result := 2;
end;

end.
'''.strip()

        result = parse(text, 'empty_label_statement_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyLabelStatementDemo')
        self.assertTrue(result.semantic.index.lookup('Second'))

    def test_exit_keyword_can_be_label_name(self) -> None:
        text = '''
unit ExitKeywordLabelDemo;

interface

procedure Process;

implementation

procedure Process;
label
  Exit;
begin
  goto Exit;
Exit:
end;

end.
'''.strip()

        result = parse(text, 'exit_keyword_label_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ExitKeywordLabelDemo')
        self.assertTrue(result.semantic.index.lookup('Process'))

    def test_inherited_call_expression(self) -> None:
        text = '''
unit InheritedCallExpressionDemo;

interface

type
  TBase = class
    function Seek(Offset: Int64): Int64;
  end;
  TChild = class(TBase)
    function Seek(Offset: Int64): Int64;
  end;

implementation

function TBase.Seek(Offset: Int64): Int64;
begin
  Result := Offset;
end;

function TChild.Seek(Offset: Int64): Int64;
begin
  Result := inherited Seek(Offset);
end;

end.
'''.strip()

        result = parse(text, 'inherited_call_expression_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'InheritedCallExpressionDemo')
        self.assertTrue(result.semantic.index.lookup('TChild.Seek'))

    def test_inherited_contextual_keyword_method_expression(self) -> None:
        text = '''
unit InheritedContextualKeywordMethodExpressionDemo;

interface

type
  TBaseList = class
    function Add: TObject;
  end;
  TEventDef = class
  end;
  TEventDefs = class(TBaseList)
    function Add: TEventDef;
  end;

implementation

function TBaseList.Add: TObject;
begin
  Result := nil;
end;

function TEventDefs.Add: TEventDef;
begin
  Result := TEventDef(inherited Add);
end;

end.
'''.strip()

        result = parse(text, 'inherited_contextual_keyword_method_expression_demo.pas', build_semantic=True)

        self.assertEqual(
            result.root.get_attribute(AttributeName.anName),
            'InheritedContextualKeywordMethodExpressionDemo',
        )
        self.assertTrue(result.semantic.index.lookup('TEventDefs.Add'))

    def test_string_literal_adjacent_char_codes_in_call_argument(self) -> None:
        text = '''
unit AdjacentStringCharCodeDemo;

interface

procedure WriteLog;

implementation

function Format(const Fmt: string): string;
begin
  Result := Fmt;
end;

procedure WriteLog;
var
  Line: string;
begin
  Line := Format('%s x%d'#13#10);
end;

end.
'''.strip()

        result = parse(text, 'adjacent_string_char_code_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'AdjacentStringCharCodeDemo')
        self.assertTrue(result.semantic.index.lookup('WriteLog'))

    def test_five_quote_string_literal_does_not_swallow_following_code(self) -> None:
        text = """
unit FiveQuoteStringLiteralDemo;

interface

procedure CheckValue;

implementation

const
  Names: array[0..3] of string = (
    'RawUtf8',
    'SpiUtf8',
    'string',
    'RawByteString');
  Defaults: array[0..3] of string = (
    '''''',  // raw utf8
    '''''',  // spi utf8
    '''''',  // string
    ''''''); // raw byte string

procedure CheckValue;
begin
  if Defaults[0] = '' then
    EOpenApi.RaiseUtf8('Unexpected %.CreateBuiltin(%)');
end;

end.
""".strip()

        result = parse(text, 'five_quote_string_literal_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'FiveQuoteStringLiteralDemo')
        self.assertTrue(result.semantic.index.lookup('CheckValue'))

    def test_include_asm_routine_allows_local_vars_before_asm_body(self) -> None:
        text = '''
function SynLZcompress1(src: PAnsiChar; size: integer; dst: PAnsiChar): integer;
var
  off: TOffsets;
  cache: array[0..4095] of cardinal;
asm
  mov r8, rdx
end;
'''.strip()

        result = parse(text, 'asm_local_vars.inc', build_semantic=True)

        self.assertTrue(result.semantic.index.lookup('SynLZcompress1'))

    def test_include_asm_statement_allows_semicolon_items(self) -> None:
        text = '''
function IntegerScanIndex(P: PCardinalArray; Count: PtrInt; Value: cardinal): PtrInt;
asm
  nop;nop;nop;nop
end;
'''.strip()

        result = parse(text, 'asm_semicolon_items.inc', build_semantic=True)

        self.assertTrue(result.semantic.index.lookup('IntegerScanIndex'))

    def test_generic_method_reference_field_suffix_without_call_args(self) -> None:
        text = '''
unit GenericMethodReferenceFieldSuffixDemo;

interface

type
  TValue = record
  end;

procedure UseValue<T>;

implementation

procedure UseValue<T>;
var
  Target: T;
begin
  Target := TValue.FromVariant(true).AsType<T>;
end;

end.
'''.strip()

        result = parse(text, 'generic_method_reference_field_suffix_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'GenericMethodReferenceFieldSuffixDemo')
        self.assertTrue(result.semantic.index.lookup('UseValue'))

    def test_spaced_generic_constructor_call(self) -> None:
        text = '''
unit SpacedGenericConstructorCallDemo;

interface

procedure CreateIt;

implementation

procedure CreateIt;
begin
  FActiveRunners := TDictionary < TThreadID, IWeakReference<ITestRunner> > .Create;
end;

end.
'''.strip()

        result = parse(text, 'spaced_generic_constructor_call_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'SpacedGenericConstructorCallDemo')
        self.assertTrue(result.semantic.index.lookup('CreateIt'))

    def test_named_assignment_call_arguments(self) -> None:
        text = '''
unit NamedAssignmentCallArgumentsDemo;

interface

procedure UseArgs;

implementation

procedure UseArgs;
begin
  BuiltinModule.dict(a := 1, b := 2);
  MainModule.MakeList(1, d:=3, c:=4, b:=2);
end;

end.
'''.strip()

        result = parse(text, 'named_assignment_call_arguments_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'NamedAssignmentCallArgumentsDemo')
        self.assertTrue(result.semantic.index.lookup('UseArgs'))

    def test_typed_inline_for_loop_variable(self) -> None:
        text = '''
unit TypedInlineForLoopVariableDemo;

interface

procedure AddItems;

implementation

procedure AddItems;
begin
  for var I: Integer := 0 to 3 do
    AddItems;
end;

end.
'''.strip()

        result = parse(text, 'typed_inline_for_loop_variable_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'TypedInlineForLoopVariableDemo')
        self.assertTrue(result.semantic.index.lookup('AddItems'))

    def test_format_argument_width_and_precision(self) -> None:
        text = '''
unit FormatArgumentWidthPrecisionDemo;

interface

procedure PrintValue(Value: Double);

implementation

procedure PrintValue(Value: Double);
begin
  WriteLn(Value:0:2);
end;

end.
'''.strip()

        result = parse(text, 'format_argument_width_precision_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'FormatArgumentWidthPrecisionDemo')
        self.assertTrue(result.semantic.index.lookup('PrintValue'))

    def test_standalone_include_fragment_with_const_and_function(self) -> None:
        text = '''
const
  IncludeValue = 42;

function IncludeFunction: Integer;
begin
  Result := IncludeValue;
end;
'''.strip()

        result = parse(text, 'standalone_fragment.inc', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'standalone_fragment')
        self.assertTrue(result.semantic.index.lookup('IncludeValue'))
        self.assertTrue(result.semantic.index.lookup('IncludeFunction'))

    def test_standalone_include_ignores_stray_calling_convention_directive(self) -> None:
        text = '''
const
  Before = 1;

{ TODO : Cannot convert original type "int (*)(BIO *, const char *, int)" }; cdecl;

function After: Integer;
begin
  Result := Before;
end;
'''.strip()

        result = parse(text, 'stray_calling_convention_fragment.inc', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'stray_calling_convention_fragment')
        self.assertTrue(result.semantic.index.lookup('Before'))
        self.assertTrue(result.semantic.index.lookup('After'))

    def test_standalone_include_literal_fragment(self) -> None:
        text = "'2.4.15507'"

        result = parse(text, 'standalone_literal_fragment.inc', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'standalone_literal_fragment')

    def test_absolute_variable_spec(self) -> None:
        text = '''
unit AbsoluteVariableSpecDemo;

interface

var
  FalseVarData: Integer;
  VarFalse: Variant absolute FalseVarData;

implementation

end.
'''.strip()

        result = parse(text, 'absolute_variable_spec_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'AbsoluteVariableSpecDemo')
        self.assertTrue(result.semantic.index.lookup('VarFalse'))

    def test_proc_type_calling_convention_after_semicolon(self) -> None:
        text = '''
unit ProcTypeCallingConventionDemo;

interface

type
  PyCFunction = function(Self, Args: Pointer): Pointer; cdecl;

implementation

end.
'''.strip()

        result = parse(text, 'proc_type_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ProcTypeCallingConventionDemo')
        symbols = result.semantic.index.lookup('PyCFunction')
        self.assertTrue(symbols)
        type_node = next(
            node.child_nodes[0]
            for node in result.root.find_node(SyntaxNodeType.ntInterface).find_node(SyntaxNodeType.ntTypeSection).child_nodes
            if node.get_attribute(AttributeName.anName) == 'PyCFunction'
        )
        self.assertEqual(type_node.get_attribute(AttributeName.anCallingConvention), 'cdecl')

    def test_record_proc_field_calling_convention_after_semicolon(self) -> None:
        text = '''
unit RecordProcFieldCallingConventionDemo;

interface

type
  TCallbackRecord = record
    Init: function(): Pointer; cdecl;
    Next: function(): Pointer;
      cdecl;
    Index: Integer;
  end;

implementation

end.
'''.strip()

        result = parse(text, 'record_proc_field_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RecordProcFieldCallingConventionDemo')
        self.assertTrue(result.semantic.index.lookup('TCallbackRecord'))

    def test_nested_array_constant(self) -> None:
        text = '''
unit NestedArrayConstantDemo;

interface

type
  TConfigOffsets = array[0..1, 0..2] of Integer;

const
  ConfigOffsets: TConfigOffsets = ((1, 2, 3), (4, 5, 6));

implementation

end.
'''.strip()

        result = parse(text, 'nested_array_constant_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'NestedArrayConstantDemo')
        self.assertTrue(result.semantic.index.lookup('ConfigOffsets'))

    def test_proc_type_calling_convention_with_varargs(self) -> None:
        text = '''
unit ProcTypeVarargsDemo;

interface

type
  TPyArg_Parse = function(Args: Pointer; Format: PAnsiChar): Integer; cdecl varargs;

implementation

end.
'''.strip()

        result = parse(text, 'proc_type_varargs_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ProcTypeVarargsDemo')
        self.assertTrue(result.semantic.index.lookup('TPyArg_Parse'))

    def test_routine_varargs_before_semicolon(self) -> None:
        text = '''
unit RoutineVarargsBeforeSemicolonDemo;

interface

implementation

function BioPrintf(Format: PAnsiChar): Integer varargs; cdecl;
begin
  Result := 0;
end;

end.
'''.strip()

        result = parse(text, 'routine_varargs_before_semicolon_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RoutineVarargsBeforeSemicolonDemo')
        symbols = result.semantic.index.lookup('BioPrintf')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].attributes.get('varargs'), 'true')
        self.assertEqual(symbols[0].attributes.get('callingconvention'), 'cdecl')

    def test_external_routine_with_calling_convention_and_varargs_directives(self) -> None:
        text = '''
unit ExternalRoutineVarargsDemo;

interface

implementation

function IoctlSocket(Socket: Integer; Cmd: Cardinal): Integer; cdecl; varargs;
  external clib name 'ioctl';

end.
'''.strip()

        result = parse(text, 'external_routine_varargs_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ExternalRoutineVarargsDemo')
        symbols = result.semantic.index.lookup('IoctlSocket')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].attributes.get('callingconvention'), 'cdecl')
        self.assertEqual(symbols[0].attributes.get('varargs'), 'true')
        self.assertEqual(symbols[0].attributes.get('external'), 'true')

    def test_external_routine_after_calling_convention_directive(self) -> None:
        text = '''
unit ExternalAfterCallingConventionDemo;

interface

implementation

function SetDllDirectoryW(PathName: PWideChar): BOOL;
  stdcall; external kernel32;

end.
'''.strip()

        result = parse(text, 'external_after_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ExternalAfterCallingConventionDemo')
        symbols = result.semantic.index.lookup('SetDllDirectoryW')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].attributes.get('callingconvention'), 'stdcall')
        self.assertEqual(symbols[0].attributes.get('external'), 'true')

    def test_external_routine_after_calling_convention_without_separator(self) -> None:
        text = '''
unit ExternalAfterCallingConventionNoSeparatorDemo;

interface

implementation

function ClockGetTime(ClockId: Cardinal; TimeSpec: Pointer): Integer;
  cdecl external clib name 'clock_gettime';

end.
'''.strip()

        result = parse(text, 'external_after_calling_convention_no_separator_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ExternalAfterCallingConventionNoSeparatorDemo')
        symbols = result.semantic.index.lookup('ClockGetTime')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].attributes.get('callingconvention'), 'cdecl')
        self.assertEqual(symbols[0].attributes.get('external'), 'true')

    def test_while_statement_allows_empty_body(self) -> None:
        text = '''
unit EmptyWhileBodyDemo;

interface

procedure WaitForIt;

implementation

procedure WaitForIt;
begin
  while False do ;
end;

end.
'''.strip()

        result = parse(text, 'empty_while_body_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyWhileBodyDemo')
        self.assertTrue(result.semantic.index.lookup('WaitForIt'))

    def test_address_of_routine_pointer_assignment(self) -> None:
        text = '''
unit AddressOfRoutinePointerAssignmentDemo;

interface

procedure ResetPointer;

implementation

procedure ResetPointer;
begin
  @GetNativeSystemInfo := nil;
end;

end.
'''.strip()

        result = parse(text, 'address_of_routine_pointer_assignment_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'AddressOfRoutinePointerAssignmentDemo')
        self.assertTrue(result.semantic.index.lookup('ResetPointer'))

    def test_if_statement_allows_empty_then_branch_before_else(self) -> None:
        text = '''
unit EmptyThenBranchDemo;

interface

procedure CheckIt;

implementation

procedure CheckIt;
begin
  if False then
  else if True then
    CheckIt;
end;

end.
'''.strip()

        result = parse(text, 'empty_then_branch_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyThenBranchDemo')
        self.assertTrue(result.semantic.index.lookup('CheckIt'))

    def test_if_statement_allows_empty_else_branch_before_outer_else(self) -> None:
        text = '''
unit EmptyElseBranchDemo;

interface

procedure CheckContract;

implementation

procedure CheckContract;
begin
  if HasContract then
    if IsArray then
      CheckContract
    else if IsObject then
      CheckContract else
  else
    RaiseWrongContract;
end;

end.
'''.strip()

        result = parse(text, 'empty_else_branch_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyElseBranchDemo')
        self.assertTrue(result.semantic.index.lookup('CheckContract'))

    def test_repeat_until_allows_empty_body(self) -> None:
        text = '''
unit EmptyRepeatUntilBodyDemo;

interface

procedure WaitDone;

implementation

procedure WaitDone;
var
  Done: Boolean;
begin
  repeat
  until Done or (Done = False);
end;

end.
'''.strip()

        result = parse(text, 'empty_repeat_until_body_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyRepeatUntilBodyDemo')
        self.assertTrue(result.semantic.index.lookup('WaitDone'))

    def test_statement_list_allows_empty_statements(self) -> None:
        text = '''
unit EmptyStatementsDemo;

interface

procedure IgnoreIt;

implementation

procedure IgnoreIt;
var
  Value: Integer;
begin
  try
    Value := 1;;
  except
    ;
  end;
  ;
end;

end.
'''.strip()

        result = parse(text, 'empty_statements_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyStatementsDemo')
        self.assertTrue(result.semantic.index.lookup('IgnoreIt'))

    def test_bare_unit_initialization_block(self) -> None:
        text = '''
unit BareUnitInitializationBlockDemo;

interface

var
  DataPath: string;

implementation

uses
  SysUtils;

begin
  DataPath := ExpandFileName('Data');
end.
'''.strip()

        result = parse(text, 'bare_unit_initialization_block_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'BareUnitInitializationBlockDemo')
        self.assertIsNotNone(result.root.find_node(SyntaxNodeType.ntInitialization))

    def test_contextual_keyword_as_record_field_name(self) -> None:
        text = '''
unit ContextualKeywordRecordFieldDemo;

interface

type
  TData = record
    strict, Valid: Boolean;
  end;

implementation

end.
'''.strip()

        result = parse(text, 'contextual_keyword_record_field_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ContextualKeywordRecordFieldDemo')
        self.assertTrue(result.semantic.index.lookup('TData'))

    def test_record_incomplete_conditional_type_alias_continues_at_visibility(self) -> None:
        text = '''
unit ConditionalTypeAliasRecordDemo;

interface

type
  TStopWatch = record
  private
    type
      TBaseMeasure =
        {$IFDEF WINDOWS}
        Int64;
        {$ENDIF}
        {$IFDEF LINUX}
        TTimeSpec;
        {$ENDIF}
  strict private
    FStartPosition: TBaseMeasure;
  end;

implementation

end.
'''.strip()

        result = parse(text, 'conditional_type_alias_record_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ConditionalTypeAliasRecordDemo')
        self.assertTrue(result.semantic.index.lookup('TStopWatch'))
        type_decls = []

        def walk(node):
            if node.typ == SyntaxNodeType.ntTypeDecl:
                type_decls.append(node)
            for child in node.child_nodes:
                walk(child)

        walk(result.root)
        self.assertTrue(
            any(node.get_attribute(AttributeName.anName) == 'TBaseMeasure' for node in type_decls)
        )

    def test_empty_conditional_uses_clause_before_implementation(self) -> None:
        text = '''
unit EmptyConditionalUsesDemo;

interface

uses
{$IFDEF WINDOWS}
  Windows;
{$ENDIF}

implementation

end.
'''.strip()

        result = parse(text, 'empty_conditional_uses_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyConditionalUsesDemo')

    def test_interface_ignores_raw_conditional_compiler_error_text(self) -> None:
        text = '''
unit RawConditionalCompilerErrorTextDemo;

interface

{$IFNDEF WINDOWS}
This unit should not be included in your project, it works on windows only
{$ENDIF}

uses
  SysUtils;

implementation

end.
'''.strip()

        result = parse(text, 'raw_conditional_compiler_error_text_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RawConditionalCompilerErrorTextDemo')

    def test_bare_equality_expression_statement_in_anonymous_method(self) -> None:
        text = '''
unit BareEqualityExpressionStatementDemo;

interface

procedure RegisterOptions;

implementation

procedure RegisterOptions;
begin
  TOptionsRegistry.RegisterOption<string>('xml',
    procedure(value: string)
    begin
      TDUnitXOptions.XMLOutputFile = value;
    end);
end;

end.
'''.strip()

        result = parse(text, 'bare_equality_expression_statement_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'BareEqualityExpressionStatementDemo')
        self.assertTrue(result.semantic.index.lookup('RegisterOptions'))

    def test_record_final_field_allows_missing_semicolon(self) -> None:
        text = '''
unit RecordFinalFieldNoSemicolonDemo;

interface

type
  TCat = packed record
    Name: RawUtf8
  end;

implementation

end.
'''.strip()

        result = parse(text, 'record_final_field_no_semicolon_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RecordFinalFieldNoSemicolonDemo')
        self.assertTrue(result.semantic.index.lookup('TCat'))

    def test_exit_allows_empty_parentheses(self) -> None:
        text = '''
unit EmptyExitCallDemo;

interface

procedure StopNow;

implementation

procedure StopNow;
begin
  Exit();
end;

end.
'''.strip()

        result = parse(text, 'empty_exit_call_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'EmptyExitCallDemo')
        self.assertTrue(result.semantic.index.lookup('StopNow'))

    def test_routine_decl_calling_convention_compound_token(self) -> None:
        text = '''
unit RoutineDeclCallingConventionDemo;

interface

function Py_CompileString(Str, Filename: PAnsiChar; Start: Integer): Pointer; cdecl;

implementation

end.
'''.strip()

        result = parse(text, 'routine_decl_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RoutineDeclCallingConventionDemo')
        symbols = result.semantic.index.lookup('Py_CompileString')
        self.assertTrue(symbols)

    def test_routine_impl_calling_convention_compound_token(self) -> None:
        text = '''
unit RoutineImplCallingConventionDemo;

interface

implementation

procedure PyObjectDestructor(Self: Pointer); cdecl;
var
  CallFree: Boolean;
begin
  CallFree := Self <> nil;
end;

end.
'''.strip()

        result = parse(text, 'routine_impl_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RoutineImplCallingConventionDemo')
        symbols = result.semantic.index.lookup('PyObjectDestructor')
        self.assertTrue(symbols)
        self.assertEqual(symbols[0].attributes.get('callingconvention'), 'cdecl')

    def test_routine_impl_forward_after_overload_directive(self) -> None:
        text = '''
unit RoutineImplForwardAfterOverloadDemo;

interface

implementation

function RttiCall(AParentAddrIsClass: Boolean = false): Pointer; overload; forward;

end.
'''.strip()

        result = parse(text, 'routine_impl_forward_after_overload_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'RoutineImplForwardAfterOverloadDemo')
        symbols = result.semantic.index.lookup('RttiCall')
        self.assertTrue(symbols)

    def test_class_method_directive_after_virtual_compound_token(self) -> None:
        text = '''
unit ClassMethodVirtualCallingConventionDemo;

interface

type
  TExposedGetSet = class
  public
    function GetterWrapper(AObj: Pointer): Pointer; virtual; cdecl;
  end;

implementation

end.
'''.strip()

        result = parse(text, 'class_method_virtual_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ClassMethodVirtualCallingConventionDemo')
        methods = []

        def walk(node):
            if node.typ == SyntaxNodeType.ntMethod:
                methods.append(node)
            for child in node.child_nodes:
                walk(child)

        walk(result.root)
        method = next(
            node for node in methods
            if node.get_attribute(AttributeName.anName) == 'GetterWrapper'
        )
        self.assertEqual(method.get_attribute(AttributeName.anMethodBinding), 'virtual')
        self.assertEqual(method.get_attribute(AttributeName.anCallingConvention), 'cdecl')

    def test_class_threadvar_section(self) -> None:
        text = '''
unit ClassThreadvarDemo;

interface

type
  TPythonThread = class
  private class threadvar
    SavedThreadState: Pointer;
  public
    class procedure BeginThreads;
  end;

implementation

end.
'''.strip()

        result = parse(text, 'class_threadvar_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'ClassThreadvarDemo')
        self.assertTrue(result.semantic.index.lookup('TPythonThread'))

    def test_var_proc_type_calling_convention_after_semicolon(self) -> None:
        text = '''
unit VarProcTypeCallingConventionDemo;

interface

procedure Load;

implementation

procedure Load;
var
  LPy_GetVersion: function: PAnsiChar; cdecl;
begin
end;

end.
'''.strip()

        result = parse(text, 'var_proc_type_calling_convention_demo.pas', build_semantic=True)

        self.assertEqual(result.root.get_attribute(AttributeName.anName), 'VarProcTypeCallingConventionDemo')
        self.assertTrue(result.semantic.index.lookup('Load'))


if __name__ == '__main__':
    unittest.main()
