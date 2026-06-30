from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .consts import ATTRIBUTE_NAME_STRINGS, AttributeName, SyntaxNodeType
from .nodes import CompoundSyntaxNode, SyntaxNode, ValuedSyntaxNode
from .semantic import (
    ArrayTypeRef,
    ClassOfTypeRef,
    ClassTypeRef,
    EnumTypeRef,
    FileTypeRef,
    GenericInstanceTypeRef,
    InterfaceTypeRef,
    Modifier,
    NamedTypeRef,
    PointerTypeRef,
    ProcTypeRef,
    RecordTypeRef,
    ReferenceKind,
    ReferenceTypeRef,
    Scope,
    ScopeKind,
    SetTypeRef,
    SourceRange,
    SubrangeTypeRef,
    Symbol,
    SymbolIndex,
    SymbolKind,
    SymbolReference,
    TypeParameterRef,
    TypeRef,
    UnknownTypeRef,
    Visibility,
)


@dataclass
class SemanticProblem:
    message: str
    range: SourceRange


@dataclass
class SemanticModel:
    unit_scope: Scope
    index: SymbolIndex
    problems: list[SemanticProblem] = field(default_factory=list)
    references: list[SymbolReference] = field(default_factory=list)


class SemanticBuilder:
    def __init__(self, *, collect_references: bool = True) -> None:
        self._collect_references_enabled = collect_references
        self._problems: list[SemanticProblem] = []
        self._references: list[SymbolReference] = []
        self._index: SymbolIndex = SymbolIndex()
        self._node_scopes: dict[int, Scope] = {}
        self._builtin_types = {
            'string', 'ansistring', 'widestring', 'unicodestring',
            'char', 'widechar',
            'integer', 'smallint', 'shortint', 'longint', 'int64', 'uint64',
            'byte', 'word', 'cardinal', 'nativeint', 'nativeuint',
            'boolean', 'bytebool', 'wordbool', 'longbool',
            'single', 'double', 'extended', 'currency', 'real', 'real48',
            'variant', 'olevariant', 'pointer', 'pchar', 'tobject', 'tclass',
        }

    def build(self, root: SyntaxNode, *, index: SymbolIndex | None = None) -> SemanticModel:
        self._reset_state(index=index, clear_scopes=True)
        unit_scope = self.declare(root, index=self._index)
        return self.resolve(root, unit_scope)

    def declare(
        self,
        root: SyntaxNode,
        *,
        index: SymbolIndex | None = None,
        unit_scope: Scope | None = None,
        reset_state: bool = False,
    ) -> Scope:
        if reset_state:
            self._reset_state(index=index, clear_scopes=True)
        elif index is not None:
            self._index = index

        unit_name = self._attr(root, AttributeName.anName) or 'unit'
        if unit_scope is None:
            unit_scope = Scope(kind=ScopeKind.UNIT, name=unit_name)
        if unit_scope.owner is None:
            unit_symbol = Symbol(
                name=unit_name,
                kind=SymbolKind.UNIT,
                decl_range=self._node_range(root),
                name_range=self._node_range(root),
                scope=unit_scope,
            )
            unit_scope.owner = unit_symbol
            self._define_symbol(unit_scope, unit_symbol)

        self._declare_symbols(root, unit_scope, Visibility.PUBLIC)
        self._index.register_unit(unit_name, unit_scope)
        return unit_scope

    def resolve(self, root: SyntaxNode, unit_scope: Scope) -> SemanticModel:
        self._references = []
        self._resolve_type_refs(unit_scope)

        if self._collect_references_enabled:
            self._collect_references_pass(root, unit_scope, Visibility.PUBLIC)

        return SemanticModel(
            unit_scope=unit_scope,
            index=self._index,
            problems=list(self._problems),
            references=list(self._references),
        )

    def _reset_state(self, *, index: SymbolIndex | None, clear_scopes: bool) -> None:
        self._problems = []
        self._references = []
        if clear_scopes:
            self._node_scopes = {}
        if index is not None:
            self._index = index
        elif self._index is None:
            self._index = SymbolIndex()

    def _declare_symbols(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        if node.typ == SyntaxNodeType.ntUses:
            self._handle_uses(node, scope)
            return
        if node.typ == SyntaxNodeType.ntTypeSection:
            self._handle_type_section(node, scope)
            return
        if node.typ == SyntaxNodeType.ntVariables:
            self._handle_variable_section(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntVariable:
            self._handle_inline_variable(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntConstants:
            self._handle_constant_section(node, scope)
            return
        if node.typ == SyntaxNodeType.ntMethod:
            self._handle_method(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntProperty:
            self._handle_property(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntField:
            self._handle_field(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntLabel:
            self._handle_label(node, scope)
            return

        for child in node.child_nodes:
            self._declare_symbols(child, scope, visibility)

    def _handle_uses(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntUnit:
                continue
            name = self._attr(child, AttributeName.anName)
            if not name:
                continue
            imported_scope = self._index.lookup_unit(name)
            if imported_scope is None:
                imported_scope = Scope(kind=ScopeKind.UNIT, name=name)
                self._add_problem(f'Unresolved unit {name}', child)
            scope.add_import(imported_scope)

    def _handle_type_section(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntTypeDecl:
                continue
            self._handle_type_decl(child, scope)

    def _handle_type_decl(self, node: SyntaxNode, scope: Scope) -> None:
        name = self._attr(node, AttributeName.anName)
        if not name:
            return
        type_node = node.find_node(SyntaxNodeType.ntType)
        type_ref, kind = self._infer_type_ref_and_kind(name, type_node)
        type_symbol = Symbol(
            name=name,
            kind=kind,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
            type_ref=type_ref,
        )
        type_scope = Scope(kind=ScopeKind.TYPE, name=name, parent=scope, owner=type_symbol)
        type_symbol.member_scope = type_scope
        type_symbol.base_types = self._type_bases(type_node)
        self._define_symbol(scope, type_symbol)
        self._node_scopes[id(node)] = type_scope

        type_params = node.find_node(SyntaxNodeType.ntTypeParams)
        if type_params is not None:
            self._define_type_parameters(type_params, type_scope)

        if kind == SymbolKind.ENUM and type_node is not None:
            self._define_enum_values(type_node, scope, type_symbol)

        if type_node is not None:
            self._visit_type_members(type_node, type_scope)

    def _define_enum_values(self, type_node: SyntaxNode, scope: Scope, type_symbol: Symbol) -> None:
        for child in type_node.child_nodes:
            if child.typ != SyntaxNodeType.ntEnum:
                continue
            name = self._child_name(child)
            if not name:
                continue
            symbol = Symbol(
                name=name,
                kind=SymbolKind.ENUM_VALUE,
                decl_range=self._node_range(child),
                name_range=self._node_range(child),
                scope=scope,
                type_ref=NamedTypeRef(name=type_symbol.name),
            )
            self._define_symbol(scope, symbol)

    def _visit_type_members(self, node: SyntaxNode, scope: Scope) -> None:
        visibility = Visibility.PUBLIC
        visibility_map = self._visibility_map()
        for child in node.child_nodes:
            if child.typ in visibility_map:
                visibility = visibility_map[child.typ]
                for member in child.child_nodes:
                    self._declare_symbols(member, scope, visibility)
                continue
            self._declare_symbols(child, scope, visibility)

    def _handle_variable_section(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        section_kind = self._attr(node, AttributeName.anKind)
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntVariable:
                continue
            name = self._child_name(child)
            if not name:
                continue
            type_ref = self._type_from_node(child.find_node(SyntaxNodeType.ntType))
            symbol = Symbol(
                name=name,
                kind=SymbolKind.VARIABLE,
                decl_range=self._node_range(child),
                name_range=self._node_range(child),
                scope=scope,
                visibility=visibility,
                type_ref=type_ref,
            )
            if section_kind:
                symbol.attributes['section'] = section_kind
            self._define_symbol(scope, symbol)

    def _handle_constant_section(self, node: SyntaxNode, scope: Scope) -> None:
        section_kind = self._attr(node, AttributeName.anKind)
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntConstant:
                continue
            name = self._child_name(child)
            if not name:
                continue
            type_ref = self._type_from_node(child.find_node(SyntaxNodeType.ntType))
            symbol = Symbol(
                name=name,
                kind=SymbolKind.CONSTANT,
                decl_range=self._node_range(child),
                name_range=self._node_range(child),
                scope=scope,
                type_ref=type_ref,
            )
            if section_kind:
                symbol.attributes['section'] = section_kind
            self._define_symbol(scope, symbol)

    def _handle_field(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        name = self._child_name(node)
        if not name:
            return
        type_ref = self._type_from_node(node.find_node(SyntaxNodeType.ntType))
        symbol = Symbol(
            name=name,
            kind=SymbolKind.FIELD,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
            visibility=visibility,
            type_ref=type_ref,
        )
        self._define_symbol(scope, symbol)

    def _handle_inline_variable(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        name = self._child_name(node)
        if not name:
            return
        type_ref = self._type_from_node(node.find_node(SyntaxNodeType.ntType))
        if isinstance(type_ref, UnknownTypeRef):
            type_ref = UnknownTypeRef('inferred')
        symbol = Symbol(
            name=name,
            kind=SymbolKind.VARIABLE,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
            visibility=visibility,
            type_ref=type_ref,
        )
        symbol.attributes['section'] = 'inline'
        self._define_symbol(scope, symbol)

    def _handle_label(self, node: SyntaxNode, scope: Scope) -> None:
        name = self._attr(node, AttributeName.anName)
        if not name and isinstance(node, ValuedSyntaxNode):
            name = node.value
        if not name:
            return
        symbol = Symbol(
            name=name,
            kind=SymbolKind.LABEL,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
        )
        self._define_symbol(scope, symbol)

    def _handle_method(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        name = self._attr(node, AttributeName.anName)
        if not name:
            return
        kind = self._method_kind(node)
        type_ref = self._method_type_ref(node, name)
        symbol = Symbol(
            name=name,
            kind=kind,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
            visibility=visibility,
            type_ref=type_ref,
            modifiers=self._method_modifiers(node),
            attributes=self._symbol_attributes(node),
        )
        routine_scope = Scope(kind=ScopeKind.ROUTINE, name=name, parent=scope, owner=symbol)
        symbol.member_scope = routine_scope
        self._define_symbol(scope, symbol)
        self._node_scopes[id(node)] = routine_scope

        params = node.find_node(SyntaxNodeType.ntParameters)
        if params is not None:
            self._define_parameters(params, routine_scope)

        type_params = node.find_node(SyntaxNodeType.ntTypeParams)
        if type_params is not None:
            self._define_type_parameters(type_params, routine_scope)

        for child in node.child_nodes:
            if child.typ in {SyntaxNodeType.ntParameters, SyntaxNodeType.ntReturnType, SyntaxNodeType.ntTypeParams}:
                continue
            self._declare_symbols(child, routine_scope, visibility)

    def _handle_property(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        name = self._attr(node, AttributeName.anName)
        if not name:
            return
        type_ref = self._type_from_node(node.find_node(SyntaxNodeType.ntType))
        symbol = Symbol(
            name=name,
            kind=SymbolKind.PROPERTY,
            decl_range=self._node_range(node),
            name_range=self._node_range(node),
            scope=scope,
            visibility=visibility,
            type_ref=type_ref,
            attributes=self._symbol_attributes(node),
        )
        self._define_symbol(scope, symbol)

    def _define_parameters(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntParameter:
                continue
            name = self._child_name(child)
            if not name:
                continue
            type_ref = self._type_from_node(child.find_node(SyntaxNodeType.ntType))
            symbol = Symbol(
                name=name,
                kind=SymbolKind.PARAMETER,
                decl_range=self._node_range(child),
                name_range=self._node_range(child),
                scope=scope,
                type_ref=type_ref,
            )
            kind = self._attr(child, AttributeName.anKind)
            if kind:
                symbol.attributes['modifier'] = kind
            self._define_symbol(scope, symbol)

    def _define_type_parameters(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntTypeParam:
                continue
            name = self._child_name(child)
            if not name:
                continue
            symbol = Symbol(
                name=name,
                kind=SymbolKind.TYPE_PARAMETER,
                decl_range=self._node_range(child),
                name_range=self._node_range(child),
                scope=scope,
                type_ref=TypeParameterRef(name=name),
            )
            constraints = self._constraint_names(child)
            if constraints:
                symbol.attributes['constraints'] = ', '.join(constraints)
            self._define_symbol(scope, symbol)

    def _method_kind(self, node: SyntaxNode) -> SymbolKind:
        kind = self._attr(node, AttributeName.anKind)
        if kind == 'function':
            return SymbolKind.FUNCTION
        if kind == 'procedure':
            return SymbolKind.PROCEDURE
        if kind == 'constructor':
            return SymbolKind.CONSTRUCTOR
        if kind == 'destructor':
            return SymbolKind.DESTRUCTOR
        return SymbolKind.METHOD

    def _method_modifiers(self, node: SyntaxNode) -> set[Modifier]:
        modifiers: set[Modifier] = set()
        if self._attr(node, AttributeName.anClass) == 'true':
            modifiers.add(Modifier.CLASS)
        binding = self._attr(node, AttributeName.anMethodBinding)
        if binding in {'virtual', 'dynamic'}:
            modifiers.add(Modifier.VIRTUAL)
        if binding == 'override':
            modifiers.add(Modifier.OVERRIDE)
        if self._attr(node, AttributeName.anReintroduce) == 'true':
            modifiers.add(Modifier.REINTRODUCE)
        if self._attr(node, AttributeName.anOverload) == 'true':
            modifiers.add(Modifier.OVERLOAD)
        if self._attr(node, AttributeName.anAbstract) == 'true':
            modifiers.add(Modifier.ABSTRACT)
        if self._attr(node, AttributeName.anInline) == 'true':
            modifiers.add(Modifier.INLINE)
        if self._attr(node, AttributeName.anExternal) == 'true':
            modifiers.add(Modifier.EXTERNAL)
        if self._attr(node, AttributeName.anForwarded) == 'true':
            modifiers.add(Modifier.FORWARD)
        if self._attr(node, AttributeName.anDeprecated):
            modifiers.add(Modifier.DEPRECATED)
        if self._attr(node, AttributeName.anNoReturn) == 'true':
            modifiers.add(Modifier.NORETURN)
        if self._attr(node, AttributeName.anStatic) == 'true':
            modifiers.add(Modifier.STATIC)
        if self._attr(node, AttributeName.anFinal) == 'true':
            modifiers.add(Modifier.FINAL)
        if self._attr(node, AttributeName.anSealed) == 'true':
            modifiers.add(Modifier.SEALED)
        return modifiers

    def _method_type_ref(self, node: SyntaxNode, name: str) -> TypeRef:
        params = node.find_node(SyntaxNodeType.ntParameters)
        param_types = tuple(
            self._type_from_node(p.find_node(SyntaxNodeType.ntType))
            for p in params.child_nodes
            if p.typ == SyntaxNodeType.ntParameter
        ) if params else ()
        return_type_node = node.find_node(SyntaxNodeType.ntReturnType)
        return_type = None
        if return_type_node is not None:
            return_type = self._type_from_node(return_type_node.find_node(SyntaxNodeType.ntType))
        return ProcTypeRef(name=name, params=param_types, return_type=return_type)

    def _infer_type_ref_and_kind(self, name: str, type_node: Optional[SyntaxNode]) -> tuple[TypeRef, SymbolKind]:
        if type_node is None:
            return UnknownTypeRef('missing type'), SymbolKind.TYPE
        type_tag = self._attr(type_node, AttributeName.anType)
        name_tag = self._attr(type_node, AttributeName.anName)
        if type_tag == 'class':
            return ClassTypeRef(name=name), SymbolKind.CLASS
        if type_tag == 'record':
            return RecordTypeRef(name=name), SymbolKind.RECORD
        if type_tag in {'interface', 'dispinterface'}:
            return InterfaceTypeRef(name=name), SymbolKind.INTERFACE
        if name_tag == 'enum':
            members = tuple(self._enum_members(type_node))
            return EnumTypeRef(name=name, members=members), SymbolKind.ENUM
        if type_tag == 'set':
            elem = self._type_from_node(type_node.find_node(SyntaxNodeType.ntType))
            return SetTypeRef(element_type=elem), SymbolKind.TYPE
        if type_tag == 'array':
            elem = self._type_from_node(type_node.find_node(SyntaxNodeType.ntType))
            return ArrayTypeRef(element_type=elem, is_dynamic=True), SymbolKind.TYPE
        if name_tag == 'subrange':
            lower, upper = self._subrange_bounds(type_node)
            return SubrangeTypeRef(lower=lower, upper=upper), SymbolKind.TYPE
        if name_tag:
            return NamedTypeRef(name=name_tag), SymbolKind.TYPE
        return UnknownTypeRef('unresolved type'), SymbolKind.TYPE

    def _type_bases(self, type_node: Optional[SyntaxNode]) -> tuple[TypeRef, ...]:
        if type_node is None:
            return ()
        bases = []
        for child in type_node.child_nodes:
            if child.typ == SyntaxNodeType.ntType:
                bases.append(self._type_from_node(child))
        return tuple(bases)

    def _resolve_type_refs(self, scope: Scope) -> None:
        for symbols in scope.symbols.values():
            for symbol in symbols:
                symbol.type_ref = self._resolve_type_ref(symbol.type_ref, scope)
                if symbol.member_scope is not None:
                    self._resolve_type_refs(symbol.member_scope)

    def _resolve_type_ref(self, type_ref: TypeRef, scope: Scope) -> TypeRef:
        if isinstance(type_ref, GenericInstanceTypeRef):
            base = self._resolve_type_ref(type_ref.base, scope)
            args = tuple(self._resolve_type_ref(arg, scope) for arg in type_ref.args)
            if isinstance(base, NamedTypeRef):
                return GenericInstanceTypeRef(base=base, args=args)
            return GenericInstanceTypeRef(base=NamedTypeRef(name=type_ref.base.name), args=args)
        if isinstance(type_ref, ArrayTypeRef):
            elem = self._resolve_type_ref(type_ref.element_type, scope)
            dims = tuple(self._resolve_type_ref(dim, scope) for dim in type_ref.dimensions)
            return ArrayTypeRef(element_type=elem, dimensions=dims, is_dynamic=type_ref.is_dynamic)
        if isinstance(type_ref, SetTypeRef):
            elem = self._resolve_type_ref(type_ref.element_type, scope)
            return SetTypeRef(element_type=elem)
        if isinstance(type_ref, PointerTypeRef):
            return PointerTypeRef(self._resolve_type_ref(type_ref.target, scope))
        if isinstance(type_ref, ClassOfTypeRef):
            return ClassOfTypeRef(self._resolve_type_ref(type_ref.target, scope))
        if isinstance(type_ref, ReferenceTypeRef):
            return ReferenceTypeRef(self._resolve_type_ref(type_ref.target, scope))
        if isinstance(type_ref, FileTypeRef) and type_ref.element_type is not None:
            return FileTypeRef(self._resolve_type_ref(type_ref.element_type, scope))
        if isinstance(type_ref, NamedTypeRef):
            return self._normalize_named_type(type_ref, scope)
        return type_ref

    def _normalize_named_type(self, type_ref: NamedTypeRef, scope: Scope) -> NamedTypeRef:
        if type_ref.unit_name:
            return type_ref
        if '.' in type_ref.name:
            unit_name, name = self._split_qualified_name(type_ref.name)
            return NamedTypeRef(name=name, unit_name=unit_name)
        resolved = scope.resolve(type_ref.name)
        if resolved:
            symbol = resolved[0]
            unit_scope = self._unit_scope_for_symbol(symbol)
            if unit_scope is not None and unit_scope.name != scope.name:
                return NamedTypeRef(name=type_ref.name, unit_name=unit_scope.name)
        return type_ref

    def _type_from_node(self, node: Optional[SyntaxNode]) -> TypeRef:
        if node is None:
            return UnknownTypeRef('missing type')
        name = self._attr(node, AttributeName.anName)
        type_tag = self._attr(node, AttributeName.anType)
        if type_tag == 'pointer':
            target = node.find_node(SyntaxNodeType.ntType)
            return PointerTypeRef(self._type_from_node(target))
        if type_tag == 'array':
            element = self._type_from_node(node.find_node(SyntaxNodeType.ntType))
            bounds_node = node.find_node(SyntaxNodeType.ntBounds)
            if bounds_node is None or not bounds_node.child_nodes:
                return ArrayTypeRef(element_type=element, is_dynamic=True)
            dims = tuple(self._bound_type_ref(child) for child in bounds_node.child_nodes)
            return ArrayTypeRef(element_type=element, dimensions=dims, is_dynamic=False)
        if type_tag == 'set':
            elem = self._type_from_node(node.find_node(SyntaxNodeType.ntType))
            return SetTypeRef(element_type=elem)
        if type_tag == 'class of':
            target = node.find_node(SyntaxNodeType.ntType)
            return ClassOfTypeRef(self._type_from_node(target))
        if type_tag == 'reference':
            target = node.find_node(SyntaxNodeType.ntType)
            return ReferenceTypeRef(self._type_from_node(target))
        if type_tag == 'file':
            target = node.find_node(SyntaxNodeType.ntType)
            if target is None:
                return FileTypeRef(None)
            return FileTypeRef(self._type_from_node(target))
        if type_tag == 'packed':
            target = node.find_node(SyntaxNodeType.ntType)
            return self._type_from_node(target)
        if type_tag in {'procedure', 'function'}:
            return self._proc_type_from_node(node, type_tag)
        if name == 'subrange':
            lower, upper = self._subrange_bounds(node)
            return SubrangeTypeRef(lower=lower, upper=upper)

        type_args_node = node.find_node(SyntaxNodeType.ntTypeArgs)
        if name:
            base = self._named_type_ref(name)
            if type_args_node is not None:
                args = tuple(
                    self._type_from_node(child)
                    for child in type_args_node.child_nodes
                    if child.typ == SyntaxNodeType.ntType
                )
                if isinstance(base, NamedTypeRef):
                    return GenericInstanceTypeRef(base=base, args=args)
            return base
        return UnknownTypeRef('unresolved type')

    def _proc_type_from_node(self, node: SyntaxNode, kind: str) -> TypeRef:
        params = node.find_node(SyntaxNodeType.ntParameters)
        param_types = tuple(
            self._type_from_node(p.find_node(SyntaxNodeType.ntType))
            for p in params.child_nodes
            if p.typ == SyntaxNodeType.ntParameter
        ) if params else ()
        return_type = None
        return_type_node = node.find_node(SyntaxNodeType.ntReturnType)
        if return_type_node is not None:
            return_type = self._type_from_node(return_type_node.find_node(SyntaxNodeType.ntType))
        return ProcTypeRef(name=kind, params=param_types, return_type=return_type)

    def _bound_type_ref(self, node: SyntaxNode) -> TypeRef:
        if node.typ == SyntaxNodeType.ntSubrange:
            lower, upper = self._subrange_bounds(node)
            return SubrangeTypeRef(lower=lower, upper=upper)
        text = self._expr_to_text(node)
        if text:
            return NamedTypeRef(name=text)
        return UnknownTypeRef('bound')

    def _subrange_bounds(self, node: SyntaxNode) -> tuple[str, str]:
        bounds = node.find_node(SyntaxNodeType.ntBounds)
        if bounds is None:
            children = node.child_nodes
        else:
            children = bounds.child_nodes
        values = [self._expr_to_text(child) for child in children if self._expr_to_text(child)]
        if len(values) >= 2:
            return (values[0], values[1])
        return ('', '')

    def _enum_members(self, node: SyntaxNode) -> Iterable[str]:
        for child in node.child_nodes:
            if child.typ == SyntaxNodeType.ntEnum:
                name = self._child_name(child)
                if name:
                    yield name

    def _collect_references_pass(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        if node.typ in {SyntaxNodeType.ntCall, SyntaxNodeType.ntDot, SyntaxNodeType.ntIdentifier}:
            self._collect_expr_references(node, scope)
            return
        if node.typ == SyntaxNodeType.ntUses:
            self._collect_uses_references(node, scope)
            return
        if node.typ == SyntaxNodeType.ntTypeSection:
            for child in node.child_nodes:
                if child.typ == SyntaxNodeType.ntTypeDecl:
                    self._collect_type_decl_references(child, scope)
            return
        if node.typ == SyntaxNodeType.ntVariables:
            for child in node.child_nodes:
                if child.typ == SyntaxNodeType.ntVariable:
                    self._collect_variable_references(child, scope)
            return
        if node.typ == SyntaxNodeType.ntConstants:
            for child in node.child_nodes:
                if child.typ == SyntaxNodeType.ntConstant:
                    self._collect_constant_references(child, scope)
            return
        if node.typ == SyntaxNodeType.ntMethod:
            self._collect_method_references(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntProperty:
            self._collect_property_references(node, scope)
            return
        if node.typ == SyntaxNodeType.ntField:
            self._collect_field_references(node, scope)
            return
        if node.typ == SyntaxNodeType.ntWith:
            self._collect_with_references(node, scope, visibility)
            return
        if node.typ == SyntaxNodeType.ntGoto:
            label_name = self._attr(node, AttributeName.anName)
            self._add_reference(label_name, ReferenceKind.LABEL, node, scope)
            return
        if node.typ in {SyntaxNodeType.ntStatements, SyntaxNodeType.ntStatement}:
            for child in node.child_nodes:
                self._collect_references_pass(child, scope, visibility)
            return

        for child in node.child_nodes:
            self._collect_references_pass(child, scope, visibility)

    def _collect_uses_references(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntUnit:
                continue
            name = self._attr(child, AttributeName.anName)
            if name:
                self._add_reference(name, ReferenceKind.UNIT, child, scope)

    def _collect_type_decl_references(self, node: SyntaxNode, scope: Scope) -> None:
        type_node = node.find_node(SyntaxNodeType.ntType)
        type_scope = self._node_scopes.get(id(node), scope)
        type_params = node.find_node(SyntaxNodeType.ntTypeParams)
        if type_params is not None:
            self._collect_type_param_references(type_params, scope)
        if type_node is not None:
            for child in type_node.child_nodes:
                if child.typ == SyntaxNodeType.ntType:
                    self._record_type_reference(child, scope)
            self._collect_type_members_references(type_node, type_scope)

    def _collect_type_members_references(self, node: SyntaxNode, scope: Scope) -> None:
        visibility = Visibility.PUBLIC
        visibility_map = self._visibility_map()
        for child in node.child_nodes:
            if child.typ in visibility_map:
                visibility = visibility_map[child.typ]
                for member in child.child_nodes:
                    self._collect_references_pass(member, scope, visibility)
                continue
            self._collect_references_pass(child, scope, visibility)

    def _collect_variable_references(self, node: SyntaxNode, scope: Scope) -> None:
        type_node = node.find_node(SyntaxNodeType.ntType)
        if type_node is not None:
            self._record_type_reference(type_node, scope)
        value_node = node.find_node(SyntaxNodeType.ntValue)
        if value_node is not None:
            self._collect_expr_references(value_node, scope)

    def _collect_constant_references(self, node: SyntaxNode, scope: Scope) -> None:
        type_node = node.find_node(SyntaxNodeType.ntType)
        if type_node is not None:
            self._record_type_reference(type_node, scope)
        value_node = node.find_node(SyntaxNodeType.ntValue)
        if value_node is not None:
            self._collect_expr_references(value_node, scope)

    def _collect_field_references(self, node: SyntaxNode, scope: Scope) -> None:
        type_node = node.find_node(SyntaxNodeType.ntType)
        if type_node is not None:
            self._record_type_reference(type_node, scope)

    def _collect_with_references(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        expr_nodes: list[SyntaxNode] = []
        body_nodes: list[SyntaxNode] = []
        for child in node.child_nodes:
            if child.typ == SyntaxNodeType.ntExpressions:
                expr_nodes.extend(child.child_nodes)
            else:
                body_nodes.append(child)

        for expr in expr_nodes:
            self._collect_expr_references(expr, scope)

        with_scopes: list[Scope] = []
        for expr in expr_nodes:
            with_scopes.extend(self._with_scopes_for_expr(expr, scope))

        target_scope = scope
        if with_scopes:
            target_scope = Scope(kind=ScopeKind.WITH, name='with', parent=scope)
            for with_scope in with_scopes:
                target_scope.add_with_scope(with_scope)

        for child in body_nodes:
            self._collect_references_pass(child, target_scope, visibility)

    def _with_scopes_for_expr(self, node: SyntaxNode, scope: Scope) -> list[Scope]:
        name = self._expr_name(node)
        if not name:
            return []
        symbol = self._resolve_reference(name, ReferenceKind.VALUE, scope)
        if symbol is None:
            return []
        return list(self._iter_member_scopes(symbol, scope))

    def _collect_method_references(self, node: SyntaxNode, scope: Scope, visibility: Visibility) -> None:
        method_scope = self._node_scopes.get(id(node), scope)
        params = node.find_node(SyntaxNodeType.ntParameters)
        if params is not None:
            for param in params.child_nodes:
                if param.typ != SyntaxNodeType.ntParameter:
                    continue
                type_node = param.find_node(SyntaxNodeType.ntType)
                if type_node is not None:
                    self._record_type_reference(type_node, method_scope)
                value_node = param.find_node(SyntaxNodeType.ntValue)
                if value_node is not None:
                    self._collect_expr_references(value_node, method_scope)
        type_params = node.find_node(SyntaxNodeType.ntTypeParams)
        if type_params is not None:
            self._collect_type_param_references(type_params, method_scope)
        return_type_node = node.find_node(SyntaxNodeType.ntReturnType)
        if return_type_node is not None:
            type_node = return_type_node.find_node(SyntaxNodeType.ntType)
            if type_node is not None:
                self._record_type_reference(type_node, method_scope)
        for child in node.child_nodes:
            if child.typ in {SyntaxNodeType.ntParameters, SyntaxNodeType.ntReturnType, SyntaxNodeType.ntTypeParams}:
                continue
            self._collect_references_pass(child, method_scope, visibility)

    def _collect_property_references(self, node: SyntaxNode, scope: Scope) -> None:
        type_node = node.find_node(SyntaxNodeType.ntType)
        if type_node is not None:
            self._record_type_reference(type_node, scope)
        params = node.find_node(SyntaxNodeType.ntParameters)
        if params is not None:
            for param in params.child_nodes:
                if param.typ != SyntaxNodeType.ntParameter:
                    continue
                type_node = param.find_node(SyntaxNodeType.ntType)
                if type_node is not None:
                    self._record_type_reference(type_node, scope)
        for child in node.child_nodes:
            if child.typ in {SyntaxNodeType.ntRead, SyntaxNodeType.ntWrite, SyntaxNodeType.ntImplements}:
                self._collect_expr_references(child, scope)
            if child.typ == SyntaxNodeType.ntUnknown and self._attr(child, AttributeName.anKind) in {'add', 'remove', 'stored'}:
                self._collect_expr_references(child, scope)

    def _collect_type_param_references(self, node: SyntaxNode, scope: Scope) -> None:
        for child in node.child_nodes:
            if child.typ != SyntaxNodeType.ntTypeParam:
                continue
            constraints = child.find_node(SyntaxNodeType.ntConstraints)
            if constraints is None:
                continue
            for constraint in constraints.child_nodes:
                if constraint.typ == SyntaxNodeType.ntType:
                    self._record_type_reference(constraint, scope)

    def _record_type_reference(self, node: SyntaxNode, scope: Scope) -> None:
        name = self._attr(node, AttributeName.anName)
        if not name:
            return
        self._add_reference(name, ReferenceKind.TYPE, node, scope)
        type_args_node = node.find_node(SyntaxNodeType.ntTypeArgs)
        if type_args_node is not None:
            for child in type_args_node.child_nodes:
                if child.typ == SyntaxNodeType.ntType:
                    self._record_type_reference(child, scope)

    def _collect_expr_references(self, node: SyntaxNode, scope: Scope) -> None:
        if node.typ == SyntaxNodeType.ntCall:
            target = node.child_nodes[0] if node.child_nodes else None
            name = self._expr_name(target) if target is not None else ''
            if name:
                self._add_reference(name, ReferenceKind.CALL, target or node, scope)
            for child in node.child_nodes[1:]:
                self._collect_expr_references(child, scope)
            return
        if node.typ == SyntaxNodeType.ntDot:
            name = self._expr_name(node)
            if name:
                self._add_reference(name, ReferenceKind.VALUE, node, scope)
            return
        if node.typ == SyntaxNodeType.ntIdentifier:
            name = self._attr(node, AttributeName.anName)
            self._add_reference(name, ReferenceKind.VALUE, node, scope)
            return

        for child in node.child_nodes:
            self._collect_expr_references(child, scope)

    def _expr_name(self, node: Optional[SyntaxNode]) -> str:
        if node is None:
            return ''
        if node.typ == SyntaxNodeType.ntIdentifier:
            name = self._attr(node, AttributeName.anName)
            generic_args = self._attr(node, AttributeName.anGenericArgs)
            if generic_args:
                return f'{name}{generic_args}'
            return name
        if node.typ == SyntaxNodeType.ntDot and len(node.child_nodes) == 2:
            left = self._expr_name(node.child_nodes[0])
            right = self._expr_name(node.child_nodes[1])
            if left and right:
                return f'{left}.{right}'
            return left or right
        if node.typ == SyntaxNodeType.ntCall and node.child_nodes:
            return self._expr_name(node.child_nodes[0])
        if node.typ == SyntaxNodeType.ntIndexed and node.child_nodes:
            return self._expr_name(node.child_nodes[0])
        return ''

    def _add_reference(self, name: str, kind: ReferenceKind, node: SyntaxNode, scope: Scope) -> None:
        if not name:
            return
        resolved = self._resolve_reference(name, kind, scope)
        if resolved is None and kind == ReferenceKind.TYPE and not self._is_builtin_type(name):
            self._add_problem(f'Unresolved type {name}', node)
        self._references.append(
            SymbolReference(
                name=name,
                kind=kind,
                ref_range=self._node_range(node),
                scope=scope,
                resolved=resolved,
            )
        )

    def _resolve_reference(self, name: str, kind: ReferenceKind, scope: Scope) -> Optional[Symbol]:
        parts = self._normalized_reference_parts(name)
        if not parts:
            return None
        if len(parts) > 1:
            resolved = self._resolve_qualified_reference(parts, scope)
            if resolved is not None:
                return resolved
            symbols = self._index.resolve_qualified('.'.join(parts))
            return symbols[0] if symbols else None
        symbols = scope.resolve(parts[0])
        if symbols:
            return symbols[0]
        return None

    def _add_problem(self, message: str, node: SyntaxNode) -> None:
        self._problems.append(
            SemanticProblem(
                message=message,
                range=self._node_range(node),
            )
        )

    def _is_builtin_type(self, name: str) -> bool:
        base = name.split('.')[-1]
        base = self._strip_generic_args(base)
        return base.casefold() in self._builtin_types

    def _normalized_reference_parts(self, name: str) -> list[str]:
        if not name:
            return []
        parts = self._split_reference_parts(name)
        normalized = [self._strip_generic_args(part) for part in parts]
        return [part for part in normalized if part]

    def _split_reference_parts(self, name: str) -> list[str]:
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        for ch in name:
            if ch == '<':
                depth += 1
            elif ch == '>':
                if depth:
                    depth -= 1
            elif ch == '.' and depth == 0:
                part = ''.join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
                continue
            buf.append(ch)
        part = ''.join(buf).strip()
        if part:
            parts.append(part)
        return parts

    def _strip_generic_args(self, name: str) -> str:
        if '<' in name:
            return name.split('<', 1)[0]
        return name

    def _resolve_qualified_reference(self, parts: list[str], scope: Scope) -> Optional[Symbol]:
        unit_scope, unit_len = self._match_unit_prefix(parts)
        if unit_scope is not None and unit_len > 0:
            symbol = self._resolve_scope_chain(unit_scope, parts[unit_len:], allow_imports=False, origin_scope=scope)
            if symbol is not None:
                return symbol
        return self._resolve_scope_chain(scope, parts, allow_imports=True, origin_scope=scope)

    def _match_unit_prefix(self, parts: list[str]) -> tuple[Optional[Scope], int]:
        if len(parts) < 2:
            return None, 0
        for idx in range(len(parts) - 1, 0, -1):
            unit_name = '.'.join(parts[:idx])
            unit_scope = self._index.lookup_unit(unit_name)
            if unit_scope is not None:
                return unit_scope, idx
        return None, 0

    def _resolve_scope_chain(
        self,
        scope: Scope,
        parts: list[str],
        *,
        allow_imports: bool,
        origin_scope: Scope,
    ) -> Optional[Symbol]:
        if not parts:
            return None
        symbol = self._resolve_in_scope(scope, parts[0], allow_imports=allow_imports)
        if symbol is None:
            return None
        for part in parts[1:]:
            member_symbol = self._resolve_member_symbol(symbol, part, origin_scope)
            if member_symbol is None:
                return None
            symbol = member_symbol
        return symbol

    def _resolve_in_scope(self, scope: Scope, name: str, *, allow_imports: bool) -> Optional[Symbol]:
        symbols = scope.resolve(name) if allow_imports else scope.lookup_local(name)
        return symbols[0] if symbols else None

    def _member_scope_for_symbol(self, symbol: Symbol, scope: Scope) -> Optional[Scope]:
        scopes = list(self._iter_member_scopes(symbol, scope))
        return scopes[0] if scopes else None

    def _resolve_member_symbol(self, symbol: Symbol, name: str, scope: Scope) -> Optional[Symbol]:
        for member_scope in self._iter_member_scopes(symbol, scope):
            members = member_scope.lookup_local(name)
            if members:
                return members[0]
        return None

    def _iter_member_scopes(self, symbol: Symbol, scope: Scope) -> Iterable[Scope]:
        type_symbol = self._type_owner_for_symbol(symbol, scope)
        if type_symbol is None:
            return
        yield from self._iter_type_member_scopes(type_symbol, scope)

    def _type_owner_for_symbol(self, symbol: Symbol, scope: Scope) -> Optional[Symbol]:
        if symbol.member_scope is not None and symbol.kind in {
            SymbolKind.TYPE,
            SymbolKind.CLASS,
            SymbolKind.RECORD,
            SymbolKind.INTERFACE,
            SymbolKind.ENUM,
        }:
            return symbol
        if symbol.scope.kind == ScopeKind.TYPE and symbol.scope.owner is not None:
            return symbol.scope.owner
        return self._type_symbol_from_ref(symbol.type_ref, scope)

    def _iter_type_member_scopes(self, type_symbol: Symbol, scope: Scope) -> Iterable[Scope]:
        seen: set[int] = set()
        queue: list[Symbol] = [type_symbol]
        while queue:
            current = queue.pop(0)
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            if current.member_scope is not None:
                yield current.member_scope
            for base_ref in current.base_types:
                base_symbol = self._type_symbol_from_ref(base_ref, scope)
                if base_symbol is not None:
                    queue.append(base_symbol)

    def _type_symbol_from_ref(self, type_ref: TypeRef, scope: Scope) -> Optional[Symbol]:
        if isinstance(type_ref, GenericInstanceTypeRef):
            return self._type_symbol_from_ref(type_ref.base, scope)
        if isinstance(type_ref, NamedTypeRef):
            if type_ref.unit_name:
                unit_scope = self._index.lookup_unit(type_ref.unit_name)
                if unit_scope is not None:
                    symbols = unit_scope.lookup_local(type_ref.name)
                    return symbols[0] if symbols else None
            if '.' in type_ref.name:
                symbols = self._index.resolve_qualified(type_ref.name)
                if symbols:
                    return symbols[0]
            symbols = scope.resolve(type_ref.name)
            return symbols[0] if symbols else None
        return None

    def _define_symbol(self, scope: Scope, symbol: Symbol) -> None:
        existing = scope.lookup_local(symbol.name)
        scope.define(symbol)
        for prev in existing:
            if self._is_overloadable(prev, symbol):
                prev.add_overload(symbol)
                symbol.add_overload(prev)

    def _is_overloadable(self, left: Symbol, right: Symbol) -> bool:
        overload_kinds = {
            SymbolKind.METHOD,
            SymbolKind.FUNCTION,
            SymbolKind.PROCEDURE,
            SymbolKind.CONSTRUCTOR,
            SymbolKind.DESTRUCTOR,
        }
        return left.kind in overload_kinds and right.kind in overload_kinds

    def _symbol_attributes(self, node: SyntaxNode) -> dict[str, str]:
        attrs: dict[str, str] = {}
        skip = {
            AttributeName.anName,
            AttributeName.anKind,
            AttributeName.anType,
            AttributeName.anClass,
            AttributeName.anVisibility,
            AttributeName.anPath,
        }
        for key, value in node.attributes:
            if key in skip:
                continue
            attrs[ATTRIBUTE_NAME_STRINGS[key]] = value
        return attrs

    def _constraint_names(self, node: SyntaxNode) -> tuple[str, ...]:
        constraints_node = node.find_node(SyntaxNodeType.ntConstraints)
        if constraints_node is None:
            return ()
        names: list[str] = []
        for child in constraints_node.child_nodes:
            if child.typ == SyntaxNodeType.ntClassConstraint:
                names.append('class')
            elif child.typ == SyntaxNodeType.ntRecordConstraint:
                names.append('record')
            elif child.typ == SyntaxNodeType.ntConstructorConstraint:
                names.append('constructor')
            elif child.typ == SyntaxNodeType.ntInterfaceConstraint:
                names.append('interface')
            elif child.typ == SyntaxNodeType.ntUnmanagedConstraint:
                names.append('unmanaged')
            elif child.typ == SyntaxNodeType.ntType:
                names.append(self._type_from_node(child).display_name())
        return tuple(names)

    def _named_type_ref(self, name: str) -> NamedTypeRef:
        if '.' in name:
            unit_name, type_name = self._split_qualified_name(name)
            return NamedTypeRef(name=type_name, unit_name=unit_name)
        return NamedTypeRef(name=name)

    def _split_qualified_name(self, name: str) -> tuple[str, str]:
        parts = name.split('.')
        return ('.'.join(parts[:-1]), parts[-1])

    def _unit_scope_for_symbol(self, symbol: Symbol) -> Optional[Scope]:
        scope = symbol.scope
        while scope is not None:
            if scope.kind == ScopeKind.UNIT:
                return scope
            scope = scope.parent
        return None

    def _attr(self, node: SyntaxNode, name: AttributeName) -> str:
        return node.get_attribute(name)

    def _child_name(self, node: Optional[SyntaxNode]) -> str:
        if node is None:
            return ''
        if isinstance(node, ValuedSyntaxNode):
            return node.value
        name = self._attr(node, AttributeName.anName)
        if name:
            return name
        for child in node.child_nodes:
            if child.typ == SyntaxNodeType.ntName:
                if isinstance(child, ValuedSyntaxNode):
                    return child.value
                return self._attr(child, AttributeName.anName)
        return ''

    def _expr_to_text(self, node: SyntaxNode) -> str:
        if isinstance(node, ValuedSyntaxNode):
            return node.value
        name = node.get_attribute(AttributeName.anName)
        if name:
            return name
        return ''

    def _node_range(self, node: SyntaxNode) -> SourceRange:
        if isinstance(node, CompoundSyntaxNode):
            return SourceRange(
                file_name=node.file_name,
                start_line=node.line,
                start_col=node.col,
                end_line=node.end_line,
                end_col=node.end_col,
            )
        return SourceRange(
            file_name=node.file_name,
            start_line=node.line,
            start_col=node.col,
            end_line=node.line,
            end_col=node.col,
        )

    def _visibility_map(self) -> dict[SyntaxNodeType, Visibility]:
        return {
            SyntaxNodeType.ntPrivate: Visibility.PRIVATE,
            SyntaxNodeType.ntProtected: Visibility.PROTECTED,
            SyntaxNodeType.ntPublic: Visibility.PUBLIC,
            SyntaxNodeType.ntPublished: Visibility.PUBLISHED,
            SyntaxNodeType.ntStrictPrivate: Visibility.STRICT_PRIVATE,
            SyntaxNodeType.ntStrictProtected: Visibility.STRICT_PROTECTED,
        }
