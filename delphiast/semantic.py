from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


def normalize_name(name: str) -> str:
    return name.casefold()


@dataclass(frozen=True)
class SourceRange:
    file_name: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def contains(self, line: int, col: int) -> bool:
        if line < self.start_line or line > self.end_line:
            return False
        if line == self.start_line and col < self.start_col:
            return False
        if line == self.end_line and col > self.end_col:
            return False
        return True


class Visibility(Enum):
    UNKNOWN = 'unknown'
    PRIVATE = 'private'
    PROTECTED = 'protected'
    PUBLIC = 'public'
    PUBLISHED = 'published'
    STRICT_PRIVATE = 'strict_private'
    STRICT_PROTECTED = 'strict_protected'


class SymbolKind(Enum):
    UNIT = 'unit'
    MODULE = 'module'
    NAMESPACE = 'namespace'
    PACKAGE = 'package'
    TYPE = 'type'
    TYPE_PARAMETER = 'type_parameter'
    CLASS = 'class'
    RECORD = 'record'
    INTERFACE = 'interface'
    ENUM = 'enum'
    ENUM_VALUE = 'enum_value'
    FIELD = 'field'
    PROPERTY = 'property'
    METHOD = 'method'
    FUNCTION = 'function'
    PROCEDURE = 'procedure'
    CONSTRUCTOR = 'constructor'
    DESTRUCTOR = 'destructor'
    VARIABLE = 'variable'
    PARAMETER = 'parameter'
    CONSTANT = 'constant'
    LABEL = 'label'


class ScopeKind(Enum):
    UNIT = 'unit'
    TYPE = 'type'
    ROUTINE = 'routine'
    BLOCK = 'block'
    WITH = 'with'


class Modifier(Enum):
    ABSTRACT = 'abstract'
    SEALED = 'sealed'
    FINAL = 'final'
    VIRTUAL = 'virtual'
    OVERRIDE = 'override'
    OVERLOAD = 'overload'
    REINTRODUCE = 'reintroduce'
    INLINE = 'inline'
    STATIC = 'static'
    CLASS = 'class'
    EXTERNAL = 'external'
    FORWARD = 'forward'
    DEPRECATED = 'deprecated'
    NORETURN = 'noreturn'


class ReferenceKind(Enum):
    UNKNOWN = 'unknown'
    VALUE = 'value'
    TYPE = 'type'
    CALL = 'call'
    PROPERTY = 'property'
    UNIT = 'unit'
    LABEL = 'label'


@dataclass(frozen=True)
class TypeRef:
    def display_name(self) -> str:
        return '<unknown>'


@dataclass(frozen=True)
class UnknownTypeRef(TypeRef):
    reason: str = ''

    def display_name(self) -> str:
        return '<unknown>' if not self.reason else f'<unknown:{self.reason}>'


@dataclass(frozen=True)
class NamedTypeRef(TypeRef):
    name: str
    unit_name: str | None = None

    def display_name(self) -> str:
        if self.unit_name:
            return f'{self.unit_name}.{self.name}'
        return self.name


@dataclass(frozen=True)
class PointerTypeRef(TypeRef):
    target: TypeRef

    def display_name(self) -> str:
        return f'^{self.target.display_name()}'


@dataclass(frozen=True)
class ClassOfTypeRef(TypeRef):
    target: TypeRef

    def display_name(self) -> str:
        return f'class of {self.target.display_name()}'


@dataclass(frozen=True)
class ReferenceTypeRef(TypeRef):
    target: TypeRef

    def display_name(self) -> str:
        return f'reference to {self.target.display_name()}'


@dataclass(frozen=True)
class FileTypeRef(TypeRef):
    element_type: TypeRef | None = None

    def display_name(self) -> str:
        if self.element_type is None:
            return 'file'
        return f'file of {self.element_type.display_name()}'


@dataclass(frozen=True)
class GenericInstanceTypeRef(TypeRef):
    base: NamedTypeRef
    args: tuple[TypeRef, ...]

    def display_name(self) -> str:
        arg_list = ', '.join(arg.display_name() for arg in self.args)
        return f'{self.base.display_name()}<{arg_list}>'


@dataclass(frozen=True)
class ArrayTypeRef(TypeRef):
    element_type: TypeRef
    dimensions: tuple[TypeRef, ...] = ()
    is_dynamic: bool = False

    def display_name(self) -> str:
        if self.is_dynamic or not self.dimensions:
            return f'array of {self.element_type.display_name()}'
        dims = ', '.join(dim.display_name() for dim in self.dimensions)
        return f'array[{dims}] of {self.element_type.display_name()}'


@dataclass(frozen=True)
class SetTypeRef(TypeRef):
    element_type: TypeRef

    def display_name(self) -> str:
        return f'set of {self.element_type.display_name()}'


@dataclass(frozen=True)
class SubrangeTypeRef(TypeRef):
    lower: str
    upper: str

    def display_name(self) -> str:
        return f'{self.lower}..{self.upper}'


@dataclass(frozen=True)
class EnumTypeRef(TypeRef):
    name: str
    members: tuple[str, ...]

    def display_name(self) -> str:
        return self.name if self.name else 'enum'


@dataclass(frozen=True)
class RecordTypeRef(TypeRef):
    name: str
    scope: Optional['Scope'] = None
    packed: bool = False

    def display_name(self) -> str:
        return self.name if self.name else 'record'


@dataclass(frozen=True)
class ClassTypeRef(TypeRef):
    name: str
    scope: Optional['Scope'] = None
    bases: tuple[TypeRef, ...] = ()
    is_abstract: bool = False

    def display_name(self) -> str:
        return self.name if self.name else 'class'


@dataclass(frozen=True)
class InterfaceTypeRef(TypeRef):
    name: str
    scope: Optional['Scope'] = None
    bases: tuple[TypeRef, ...] = ()

    def display_name(self) -> str:
        return self.name if self.name else 'interface'


@dataclass(frozen=True)
class TypeParameterRef(TypeRef):
    name: str

    def display_name(self) -> str:
        return self.name


@dataclass(frozen=True)
class ProcTypeRef(TypeRef):
    name: str
    params: tuple[TypeRef, ...] = ()
    return_type: TypeRef | None = None

    def display_name(self) -> str:
        params = ', '.join(p.display_name() for p in self.params)
        if self.return_type is None:
            return f'procedure({params})'
        return f'function({params}): {self.return_type.display_name()}'


@dataclass
class Symbol:
    name: str
    kind: SymbolKind
    decl_range: SourceRange
    name_range: SourceRange
    scope: 'Scope'
    visibility: Visibility = Visibility.UNKNOWN
    type_ref: TypeRef = field(default_factory=UnknownTypeRef)
    modifiers: set[Modifier] = field(default_factory=set)
    attributes: dict[str, str] = field(default_factory=dict)
    doc: str = ''
    overloads: list['Symbol'] = field(default_factory=list)
    member_scope: Optional['Scope'] = None
    base_types: tuple[TypeRef, ...] = ()

    def add_overload(self, symbol: 'Symbol') -> None:
        self.overloads.append(symbol)


@dataclass
class SymbolReference:
    name: str
    kind: ReferenceKind
    ref_range: SourceRange
    scope: 'Scope'
    resolved: Optional[Symbol] = None


@dataclass
class Scope:
    kind: ScopeKind
    name: str
    parent: Optional['Scope'] = None
    owner: Optional[Symbol] = None
    symbols: dict[str, list[Symbol]] = field(default_factory=dict)
    imports: list['Scope'] = field(default_factory=list)
    with_scopes: list['Scope'] = field(default_factory=list)

    def define(self, symbol: Symbol) -> None:
        key = normalize_name(symbol.name)
        if key not in self.symbols:
            self.symbols[key] = []
        self.symbols[key].append(symbol)

    def lookup_local(self, name: str) -> list[Symbol]:
        return list(self.symbols.get(normalize_name(name), []))

    def resolve(self, name: str) -> list[Symbol]:
        results: list[Symbol] = []
        results.extend(self.lookup_local(name))
        if results:
            return results
        for scope in self.with_scopes:
            scoped = scope.lookup_local(name)
            if scoped:
                return scoped
        if self.parent is not None:
            results.extend(self.parent.resolve(name))
            if results:
                return results
        for scope in self.imports:
            scoped = scope.lookup_local(name)
            if scoped:
                results.extend(scoped)
        return results

    def add_import(self, scope: 'Scope') -> None:
        if scope not in self.imports:
            self.imports.append(scope)

    def add_with_scope(self, scope: 'Scope') -> None:
        if scope not in self.with_scopes:
            self.with_scopes.append(scope)


@dataclass
class SymbolIndex:
    units: dict[str, Scope] = field(default_factory=dict)
    name_index: dict[str, list[Symbol]] = field(default_factory=dict)

    def register_unit(self, unit_name: str, unit_scope: Scope, *, index_symbols: bool = True) -> None:
        self.units[normalize_name(unit_name)] = unit_scope
        if index_symbols:
            self._index_scope(unit_scope)

    def lookup(self, name: str) -> list[Symbol]:
        return list(self.name_index.get(normalize_name(name), []))

    def lookup_unit(self, unit_name: str) -> Optional[Scope]:
        return self.units.get(normalize_name(unit_name))

    def resolve_qualified(self, qualified_name: str) -> list[Symbol]:
        parts = qualified_name.split('.')
        if len(parts) < 2:
            return self.lookup(qualified_name)
        unit_name = '.'.join(parts[:-1])
        symbol_name = parts[-1]
        unit_scope = self.lookup_unit(unit_name)
        if unit_scope is None:
            return []
        return unit_scope.lookup_local(symbol_name)

    def _index_scope(self, scope: Scope) -> None:
        for symbols in scope.symbols.values():
            for symbol in symbols:
                key = normalize_name(symbol.name)
                self.name_index.setdefault(key, []).append(symbol)
        for child_scope in scope.imports:
            self._index_scope(child_scope)
