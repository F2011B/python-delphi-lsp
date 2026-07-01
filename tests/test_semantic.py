import pathlib
import unittest

from delphiast.parser import parse
from delphiast.semantic import (
    GenericInstanceTypeRef,
    NamedTypeRef,
    ReferenceKind,
    Scope,
    ScopeKind,
    SourceRange,
    Symbol,
    SymbolIndex,
    SymbolKind,
)


FIXTURE_DIR = pathlib.Path(__file__).parent / 'fixtures'


class SemanticTests(unittest.TestCase):
    def test_symbol_index_handles_cyclic_import_scopes(self) -> None:
        source_range = SourceRange('cycle.pas', 1, 1, 1, 1)
        left = Scope(kind=ScopeKind.UNIT, name='Left')
        right = Scope(kind=ScopeKind.UNIT, name='Right')
        left.add_import(right)
        right.add_import(left)
        left.define(Symbol('LeftValue', SymbolKind.VARIABLE, source_range, source_range, left))
        right.define(Symbol('RightValue', SymbolKind.VARIABLE, source_range, source_range, right))

        index = SymbolIndex()
        index.register_unit('Left', left)

        self.assertEqual([symbol.name for symbol in index.lookup('LeftValue')], ['LeftValue'])
        self.assertEqual([symbol.name for symbol in index.lookup('RightValue')], ['RightValue'])

    def test_resolves_local_variable_references(self) -> None:
        text = (FIXTURE_DIR / 'unit_statements.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_statements.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        references = result.semantic.references
        resolved = [
            ref
            for ref in references
            if ref.name == 'I' and ref.resolved is not None and ref.resolved.kind == SymbolKind.VARIABLE
        ]
        self.assertTrue(resolved)

    def test_resolves_type_references(self) -> None:
        text = (FIXTURE_DIR / 'unit_advanced.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_advanced.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        references = result.semantic.references
        type_refs = [
            ref
            for ref in references
            if ref.name == 'TProcRef'
            and ref.kind == ReferenceKind.TYPE
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.TYPE
        ]
        self.assertTrue(type_refs)

    def test_resolves_inherited_member_calls(self) -> None:
        text = (FIXTURE_DIR / 'unit_inheritance.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_inheritance.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        references = result.semantic.references
        inherited_calls = [
            ref
            for ref in references
            if ref.name == 'Child.Foo'
            and ref.kind == ReferenceKind.CALL
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.PROCEDURE
        ]
        self.assertTrue(inherited_calls)

    def test_with_scope_resolves_members(self) -> None:
        text = (FIXTURE_DIR / 'unit_with.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_with.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        references = result.semantic.references
        value_refs = [
            ref
            for ref in references
            if ref.name == 'Value'
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.FIELD
        ]
        self.assertTrue(value_refs)
        method_refs = [
            ref
            for ref in references
            if ref.name == 'DoIt'
            and ref.kind == ReferenceKind.CALL
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.PROCEDURE
        ]
        self.assertTrue(method_refs)

    def test_resolves_generic_call_references(self) -> None:
        text = (FIXTURE_DIR / 'unit_generics.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_generics.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        references = result.semantic.references
        box_calls = [
            ref
            for ref in references
            if ref.name == 'TBox<string>.Create'
            and ref.kind == ReferenceKind.CALL
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.CONSTRUCTOR
        ]
        self.assertTrue(box_calls)
        nested_calls = [
            ref
            for ref in references
            if ref.name == 'TOuter<string>.TInner<Integer>.Create'
            and ref.kind == ReferenceKind.CALL
            and ref.resolved is not None
            and ref.resolved.kind == SymbolKind.CONSTRUCTOR
        ]
        self.assertTrue(nested_calls)
        nested_generic_calls = [
            ref
            for ref in references
            if ref.name == 'TBox<TOuter<string>.TInner<Integer>>.Create'
            and ref.kind == ReferenceKind.CALL
        ]
        self.assertTrue(nested_generic_calls)
        deep_generic_calls = [
            ref
            for ref in references
            if ref.name == 'TBox<TOuter<TBox<string>>.TInner<TBox<Integer>>>.Create'
            and ref.kind == ReferenceKind.CALL
        ]
        self.assertTrue(deep_generic_calls)

    def test_local_variable_generic_type_ref_is_structured(self) -> None:
        text = (FIXTURE_DIR / 'unit_generics.pas').read_text(encoding='utf-8')
        result = parse(text, 'unit_generics.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)

        box_symbols: list[Symbol] = []

        def walk(scope: Scope, seen: set[int]) -> None:
            if id(scope) in seen:
                return
            seen.add(id(scope))
            for symbols in scope.symbols.values():
                for symbol in symbols:
                    if symbol.name == 'Box':
                        box_symbols.append(symbol)
                    if symbol.member_scope is not None:
                        walk(symbol.member_scope, seen)

        walk(result.semantic.unit_scope, set())

        self.assertEqual(len(box_symbols), 1)
        box_type = box_symbols[0].type_ref
        self.assertIsInstance(box_type, GenericInstanceTypeRef)
        assert isinstance(box_type, GenericInstanceTypeRef)
        self.assertEqual(box_type.base, NamedTypeRef(name='TBox', unit_name='UnitGenerics'))
        self.assertEqual(box_type.args, (NamedTypeRef(name='string'),))

    def test_captures_interface_and_unmanaged_constraints(self) -> None:
        text = '''
unit GenericConstraintDemo;

interface

type
  TBox<T: interface; constructor; unmanaged> = class
  end;

implementation

end.
'''.strip()
        result = parse(text, 'generic_constraint_demo.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        symbols: list[tuple[str, dict[str, str]]] = []

        def walk(scope, seen: set[int]) -> None:
            if id(scope) in seen:
                return
            seen.add(id(scope))
            for symbol_list in scope.symbols.values():
                for symbol in symbol_list:
                    if symbol.kind == SymbolKind.TYPE_PARAMETER:
                        symbols.append((symbol.name, dict(symbol.attributes)))
                    if symbol.member_scope is not None:
                        walk(symbol.member_scope, seen)
            for imported in scope.imports:
                walk(imported, seen)

        walk(result.semantic.unit_scope, set())
        self.assertTrue(symbols)
        by_name = {name: attrs for name, attrs in symbols}
        self.assertIn('T', by_name)
        self.assertEqual(by_name['T'].get('constraints'), 'interface, constructor, unmanaged')

    def test_captures_method_type_parameter_constraints(self) -> None:
        text = '''
unit MethodGenericConstraintDemo;

interface

type
  TRec = class
    procedure Run<T: class; constructor>;
  end;

implementation

procedure TRec.Run<T: class; constructor>;
begin
end;

end.
'''.strip()
        result = parse(text, 'method_generic_constraint_demo.pas', build_semantic=True)
        self.assertIsNotNone(result.semantic)
        type_param_symbols: list[tuple[str, dict[str, str]]] = []

        def walk(scope, seen: set[int]) -> None:
            if id(scope) in seen:
                return
            seen.add(id(scope))
            for symbol_list in scope.symbols.values():
                for symbol in symbol_list:
                    if symbol.kind == SymbolKind.TYPE_PARAMETER:
                        type_param_symbols.append((symbol.name, dict(symbol.attributes)))
                    if symbol.member_scope is not None:
                        walk(symbol.member_scope, seen)
            for imported in scope.imports:
                walk(imported, seen)

        walk(result.semantic.unit_scope, set())
        self.assertTrue(type_param_symbols)
        by_name = {name: attrs for name, attrs in type_param_symbols}
        self.assertIn('T', by_name)
        self.assertEqual(by_name['T'].get('constraints'), 'class, constructor')


if __name__ == '__main__':
    unittest.main()
