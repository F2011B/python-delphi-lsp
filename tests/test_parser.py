import pathlib
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


if __name__ == '__main__':
    unittest.main()
