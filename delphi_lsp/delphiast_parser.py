"""Tolerant pure-Python structural parser inspired by DelphiAST/SimpleParser.

This file is a clean Python port of the parser architecture used by DelphiAST.
DelphiAST is licensed under MPL-2.0:
https://github.com/RomanYankovsky/DelphiAST
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .consts import AttributeName, SyntaxNodeType
from .delphiast_lexer import DelphiAstLexer
from .delphiast_tokens import Token, TokenKind
from .nodes import CompoundSyntaxNode, SyntaxNode, ValuedSyntaxNode


_ROUTINES = frozenset({"procedure", "function", "constructor", "destructor", "operator"})
_SECTIONS = frozenset(
    {
        "interface",
        "implementation",
        "initialization",
        "finalization",
        "type",
        "const",
        "resourcestring",
        "var",
        "threadvar",
        "label",
        "exports",
        "uses",
        "contains",
        "requires",
        "begin",
        "end",
    }
)
_VISIBILITIES = {
    "private": SyntaxNodeType.ntPrivate,
    "protected": SyntaxNodeType.ntProtected,
    "public": SyntaxNodeType.ntPublic,
    "published": SyntaxNodeType.ntPublished,
}
_METHOD_DIRECTIVES = {
    "abstract": AttributeName.anAbstract,
    "assembler": AttributeName.anAssembler,
    "deprecated": AttributeName.anDeprecated,
    "external": AttributeName.anExternal,
    "final": AttributeName.anFinal,
    "forward": AttributeName.anForwarded,
    "inline": AttributeName.anInline,
    "overload": AttributeName.anOverload,
    "reintroduce": AttributeName.anReintroduce,
    "sealed": AttributeName.anSealed,
    "static": AttributeName.anStatic,
    "unsafe": AttributeName.anUnsafe,
    "noreturn": AttributeName.anNoReturn,
}
_CALLING_CONVENTIONS = frozenset(
    {
        "cdecl",
        "msfastcall",
        "pascal",
        "register",
        "safecall",
        "softfloat",
        "stdcall",
        "syscall",
        "vectorcall",
        "winapi",
    }
)
_STATEMENT_TERMINATORS = frozenset({"end", "else", "until", "except", "finally"})
_PROPERTY_ACCESSORS = frozenset(
    {"read", "write", "readonly", "writeonly", "add", "remove"}
)
_PROPERTY_STORAGE = frozenset({"default", "nodefault", "stored"})
_PROPERTY_SPECIFIERS = frozenset(
    {
        "index",
        "dispid",
        "implements",
        *_PROPERTY_ACCESSORS,
        *_PROPERTY_STORAGE,
    }
)


@dataclass(frozen=True)
class DelphiAstParseProblem:
    message: str
    line: int
    column: int
    category: str = "syntax"


@dataclass
class DelphiAstParseOutput:
    root: SyntaxNode
    problems: list[DelphiAstParseProblem]
    requires_legacy: bool = False


class DelphiAstParser:
    """Single-pass, recovery-oriented Object Pascal parser.

    The parser deliberately extracts the structures consumed by indexing,
    navigation, relations, and metrics. It does not reject an entire unit when
    one dialect-specific declaration is unsupported.
    """

    def __init__(self, source: str, file_name: str = "") -> None:
        self.source = source
        self.file_name = file_name
        self.tokens = [
            token
            for token in DelphiAstLexer(source, file_name).tokenize(include_trivia=True)
            if token.kind not in {TokenKind.COMMENT, TokenKind.DIRECTIVE}
        ]
        self.index = 0
        self.problems: list[DelphiAstParseProblem] = []

    def parse(self) -> DelphiAstParseOutput:
        root = self._node(SyntaxNodeType.ntUnit, self._peek(), compound=True)
        header = self._normalized()
        if header in {"unit", "program", "library", "package"}:
            root.set_attribute(AttributeName.anKind, header)
            self._advance()
            name = self._read_qualified_name()
            if name:
                root.set_attribute(AttributeName.anName, name)
            else:
                self._problem("Expected module name")
            while not self._eof() and not self._at(";"):
                if self._accept("deprecated"):
                    value = "true"
                    if self._peek().kind is TokenKind.STRING:
                        value = self._dequote_string(self._advance().value)
                    root.set_attribute(AttributeName.anDeprecated, value)
                elif self._accept("experimental"):
                    root.set_attribute(AttributeName.anKind, "experimental")
                else:
                    self._advance()
            self._consume_through(";")
        else:
            self._problem("Expected unit, program, library, or package header")

        current = root
        while not self._eof():
            before = self.index
            word = self._normalized()
            if word == "interface":
                current = self._section(root, SyntaxNodeType.ntInterface)
            elif word == "implementation":
                current = self._section(root, SyntaxNodeType.ntImplementation)
            elif word == "initialization":
                current = self._section(root, SyntaxNodeType.ntInitialization)
                self._parse_statements(current, {"finalization", "end"})
            elif word == "finalization":
                current = self._section(root, SyntaxNodeType.ntFinalization)
                self._parse_statements(current, {"end"})
            elif word in {"uses", "contains", "requires"}:
                current.add_child(self._parse_dependencies(word))
            elif word == "type":
                current.add_child(self._parse_type_section())
            elif word in {"const", "resourcestring"}:
                current.add_child(self._parse_constants(word))
            elif word in {"var", "threadvar"}:
                current.add_child(self._parse_variables(word))
            elif word == "exports":
                current.add_child(self._parse_exports())
            elif word in _ROUTINES or (word == "class" and self._normalized(1) in _ROUTINES):
                current.add_child(self._parse_routine(parse_body=True))
            elif word == "begin":
                statements = self._parse_begin_block()
                current.add_child(statements)
            elif word == "end":
                self._advance()
                self._accept(".")
                self._accept(";")
            elif self._peek().kind is TokenKind.UNKNOWN:
                self._problem(f"Unsupported character {self._peek().value!r}")
                self._advance()
            else:
                self._advance()
            self._ensure_progress(before)

        if isinstance(root, CompoundSyntaxNode):
            last = self.tokens[max(0, len(self.tokens) - 1)]
            root.end_line = last.line
            root.end_col = last.column
        return DelphiAstParseOutput(root=root, problems=self.problems)

    def _section(self, root: SyntaxNode, typ: SyntaxNodeType) -> SyntaxNode:
        token = self._advance()
        section = self._node(typ, token, compound=True)
        root.add_child(section)
        return section

    def _parse_dependencies(self, keyword: str) -> SyntaxNode:
        token = self._advance()
        typ = {
            "uses": SyntaxNodeType.ntUses,
            "contains": SyntaxNodeType.ntContains,
            "requires": SyntaxNodeType.ntRequires,
        }[keyword]
        section = self._node(typ, token)
        while not self._eof() and not self._at(";"):
            before = self.index
            name_token = self._peek()
            name = self._read_qualified_name()
            if name:
                unit = self._node(SyntaxNodeType.ntUnit, name_token)
                unit.set_attribute(AttributeName.anName, name)
                if self._accept("in"):
                    path = self._advance()
                    unit.set_attribute(AttributeName.anPath, path.value.strip("'"))
                section.add_child(unit)
            else:
                self._advance()
            if not self._accept(",") and not self._at(";") and name:
                self._problem("Expected ',' or ';' in dependency clause")
            self._ensure_progress(before)
        self._accept(";")
        return section

    def _parse_exports(self) -> SyntaxNode:
        section = self._node(SyntaxNodeType.ntExports, self._advance())
        while not self._eof() and not self._at(";"):
            before = self.index
            token = self._peek()
            name = self._read_qualified_name(allow_keywords=True)
            if name:
                element = self._node(SyntaxNodeType.ntElement, token)
                name_node = self._node(SyntaxNodeType.ntName, token)
                name_node.set_attribute(AttributeName.anName, name)
                element.add_child(name_node)
                if self._normalized() in {"name", "index", "resident"}:
                    self._skip_balanced_until({",", ";"})
                section.add_child(element)
            else:
                self._advance()
            self._accept(",")
            self._ensure_progress(before)
        self._accept(";")
        return section

    def _parse_type_section(self) -> SyntaxNode:
        section = self._node(SyntaxNodeType.ntTypeSection, self._advance())
        while not self._eof():
            before = self.index
            if self._normalized() in _SECTIONS or self._normalized() in _ROUTINES:
                break
            if not self._is_name():
                self._problem("Expected type declaration")
                self._synchronize({";"})
                self._accept(";")
                self._ensure_progress(before)
                continue
            name_token = self._peek()
            name = self._read_qualified_name(allow_keywords=True)
            declaration = self._node(SyntaxNodeType.ntTypeDecl, name_token)
            declaration.set_attribute(AttributeName.anName, name)
            self._parse_type_parameters(declaration)
            if not self._accept("="):
                self._problem(f"Expected '=' after type {name}")
                self._synchronize({";"})
                self._accept(";")
                section.add_child(declaration)
                self._ensure_progress(before)
                continue
            type_node = self._parse_type()
            if self._accept("align"):
                type_node.set_attribute(AttributeName.anAlign, "align")
                if self._peek().kind is TokenKind.NUMBER:
                    self._advance()
            while self._normalized() in {
                "deprecated",
                "experimental",
                "platform",
                "library",
            }:
                directive = self._advance().normalized
                if directive == "deprecated":
                    value = "true"
                    if self._peek().kind is TokenKind.STRING:
                        value = self._dequote_string(self._advance().value)
                    declaration.set_attribute(AttributeName.anDeprecated, value)
                    type_node.set_attribute(AttributeName.anDeprecated, value)
                else:
                    declaration.set_attribute(AttributeName.anKind, directive)
            declaration.add_child(type_node)
            section.add_child(declaration)
            self._accept(";")
            self._ensure_progress(before)
        return section

    def _parse_type_parameters(self, declaration: SyntaxNode) -> None:
        if not self._accept("<"):
            return
        params = self._node(SyntaxNodeType.ntTypeParams, self._peek(-1))
        while not self._eof() and not self._at(">"):
            before = self.index
            group = self._collect_until({";", ">"})
            colon = self._top_level_token_index(group, ":")
            name_tokens = group if colon is None else group[:colon]
            constraint_tokens = [] if colon is None else group[colon + 1 :]
            names = self._split_top_level_tokens(name_tokens, {","})
            constraints = self._parse_constraint_tokens(constraint_tokens)
            for name_part in names:
                if not name_part:
                    continue
                name_token = name_part[0]
                parameter = self._node(SyntaxNodeType.ntTypeParam, name_token)
                parameter.add_child(
                    self._valued(
                        SyntaxNodeType.ntName,
                        name_token,
                        self._tokens_text(name_part).strip(),
                    )
                )
                if constraints is not None:
                    parameter.add_child(constraints.clone())
                params.add_child(parameter)
            self._accept(";")
            self._ensure_progress(before)
        self._accept(">")
        declaration.add_child(params)

    def _parse_constraint_tokens(self, values: list[Token]) -> Optional[SyntaxNode]:
        parts = self._split_top_level_tokens(values, {","})
        if not any(parts):
            return None
        constraints = self._node(SyntaxNodeType.ntConstraints, values[0])
        special = {
            "class": SyntaxNodeType.ntClassConstraint,
            "record": SyntaxNodeType.ntRecordConstraint,
            "constructor": SyntaxNodeType.ntConstructorConstraint,
            "interface": SyntaxNodeType.ntInterfaceConstraint,
            "unmanaged": SyntaxNodeType.ntUnmanagedConstraint,
        }
        for part in parts:
            if not part:
                continue
            text = self._tokens_text(part).strip()
            typ = special.get(text.casefold())
            if typ is not None:
                constraints.add_child(self._node(typ, part[0]))
                continue
            constraint = self._node(SyntaxNodeType.ntType, part[0])
            constraint.set_attribute(AttributeName.anName, text)
            constraints.add_child(constraint)
        return constraints

    def _parse_type(self) -> SyntaxNode:
        token = self._peek()
        packed = self._accept("packed")
        kind = self._normalized()
        if kind in {"class", "record", "object", "interface", "dispinterface"}:
            self._advance()
            type_node = self._node(SyntaxNodeType.ntType, token)
            type_node.set_attribute(AttributeName.anType, "record" if kind == "object" else kind)
            if kind == "class" and self._accept("of"):
                type_node.set_attribute(AttributeName.anType, "classref")
                type_node.add_child(self._parse_type_reference({";"}))
            else:
                if kind == "class" and self._accept("helper"):
                    self._accept("for")
                    target_token = self._peek()
                    target_name = self._read_qualified_name(allow_keywords=True)
                    target = self._node(SyntaxNodeType.ntType, target_token)
                    target.set_attribute(AttributeName.anName, target_name)
                    type_node.add_child(target)
                if self._accept("abstract"):
                    type_node.set_attribute(AttributeName.anAbstract, "true")
                self._parse_base_types(type_node)
                if not self._at(";"):
                    self._parse_type_body(type_node)
        elif self._at("("):
            type_node = self._parse_enum()
        elif kind == "set":
            self._advance()
            self._accept("of")
            type_node = self._node(SyntaxNodeType.ntType, token)
            type_node.set_attribute(AttributeName.anType, "set")
            type_node.add_child(self._parse_type_reference({";", ")"}))
        elif kind == "array":
            type_node = self._parse_array_type()
        elif kind in _ROUTINES:
            type_node = self._parse_procedural_type()
        elif self._at("^"):
            self._advance()
            type_node = self._node(SyntaxNodeType.ntType, token)
            type_node.set_attribute(AttributeName.anType, "pointer")
            type_node.add_child(self._parse_type_reference({";", ")"}))
        else:
            type_node = self._parse_type_reference({";"})
        if packed:
            wrapper = self._node(SyntaxNodeType.ntType, token)
            wrapper.set_attribute(AttributeName.anType, "packed")
            wrapper.add_child(type_node)
            return wrapper
        return type_node

    def _parse_base_types(self, type_node: SyntaxNode) -> None:
        if not self._accept("("):
            return
        while not self._eof() and not self._at(")"):
            type_node.add_child(self._parse_type_reference({",", ")"}))
            if not self._accept(","):
                break
        self._accept(")")

    def _parse_type_body(self, type_node: SyntaxNode) -> None:
        active_parent = type_node
        while not self._eof() and not self._at("end"):
            before = self.index
            word = self._normalized()
            if word in {"of", "helper"}:
                break
            if word == "strict" and self._normalized(1) in {"private", "protected"}:
                first = self._advance()
                visibility = self._advance().normalized
                typ = (
                    SyntaxNodeType.ntStrictPrivate
                    if visibility == "private"
                    else SyntaxNodeType.ntStrictProtected
                )
                active_parent = self._node(typ, first)
                type_node.add_child(active_parent)
            elif word in _VISIBILITIES:
                active_parent = self._node(_VISIBILITIES[word], self._advance())
                type_node.add_child(active_parent)
            elif word == "type":
                active_parent.add_child(self._parse_type_section())
            elif word == "case":
                self._parse_variant_part(type_node)
            elif word in _ROUTINES or (word == "class" and self._normalized(1) in _ROUTINES):
                active_parent.add_child(self._parse_routine(parse_body=False))
            elif word == "property":
                active_parent.add_child(self._parse_property())
            elif word == "class" and self._normalized(1) == "property":
                self._advance()
                prop = self._parse_property()
                prop.set_attribute(AttributeName.anClass, "true")
                active_parent.add_child(prop)
            elif self._at("[") and self._peek(1).kind is TokenKind.STRING:
                start = self._advance()
                guid_token = self._advance()
                guid = self._node(SyntaxNodeType.ntGuid, start)
                guid.set_attribute(
                    AttributeName.anName,
                    self._dequote_string(guid_token.value),
                )
                type_node.add_child(guid)
                self._accept("]")
            elif self._at("["):
                attributes = self._parse_attributes()
                if self._is_name(allow_keywords=True):
                    fields = self._parse_named_declaration(
                        SyntaxNodeType.ntField,
                        attributes=attributes,
                    )
                    for field in fields:
                        active_parent.add_child(field)
            elif self._is_name(allow_keywords=True):
                fields = self._parse_named_declaration(SyntaxNodeType.ntField)
                for field in fields:
                    active_parent.add_child(field)
            else:
                if self._peek().kind is TokenKind.UNKNOWN:
                    self._problem(f"Unsupported type member token {self._peek().value!r}")
                self._advance()
            self._ensure_progress(before)
        self._accept("end")

    def _parse_variant_part(self, type_node: SyntaxNode) -> None:
        self._advance()
        selector_tokens = self._collect_until({"of"})
        self._accept("of")
        colon = self._top_level_token_index(selector_tokens, ":")
        selector_type = selector_tokens if colon is None else selector_tokens[colon + 1 :]
        if selector_type:
            selector = self._node(SyntaxNodeType.ntType, selector_type[0])
            selector.set_attribute(
                AttributeName.anName,
                self._tokens_text(selector_type).strip(),
            )
            type_node.add_child(selector)

        while not self._eof() and not self._at("end"):
            before = self.index
            label_tokens = self._collect_until({":", ";", "end"})
            if not label_tokens:
                self._accept(";")
                self._ensure_progress(before)
                continue
            labels = self._node(SyntaxNodeType.ntCaseLabels, label_tokens[0])
            for part in self._split_top_level_tokens(label_tokens, {","}):
                expression = self._expression_direct(part)
                if expression is None:
                    continue
                label = self._node(SyntaxNodeType.ntCaseLabel, part[0])
                label.add_child(expression)
                labels.add_child(label)
            type_node.add_child(labels)
            self._accept(":")
            if self._accept("("):
                self._parse_variant_fields(type_node)
                self._accept(")")
            self._accept(";")
            self._ensure_progress(before)

    def _parse_variant_fields(self, type_node: SyntaxNode) -> None:
        while not self._eof() and not self._at(")"):
            before = self.index
            attributes = self._parse_attributes() if self._at("[") else None
            if self._at("case"):
                self._parse_variant_part(type_node)
            elif self._is_name(allow_keywords=True):
                fields = self._parse_named_declaration(
                    SyntaxNodeType.ntField,
                    attributes=attributes,
                )
                for field in fields:
                    type_node.add_child(field)
            else:
                self._advance()
            self._accept(";")
            self._ensure_progress(before)

    def _parse_attributes(self) -> SyntaxNode:
        attributes = self._node(SyntaxNodeType.ntAttributes, self._peek())
        while self._accept("["):
            while not self._eof() and not self._at("]"):
                before = self.index
                token = self._peek()
                name = self._read_qualified_name(allow_keywords=True)
                if name:
                    attribute = self._node(SyntaxNodeType.ntAttribute, token)
                    attribute.set_attribute(AttributeName.anName, name)
                    if self._accept("("):
                        argument_tokens = self._collect_until({")"})
                        arguments = self._node(SyntaxNodeType.ntArguments, self._peek(-1))
                        for part in self._split_top_level_tokens(argument_tokens, {","}):
                            expression = self._expression_direct(part)
                            if expression is not None:
                                argument = self._node(
                                    SyntaxNodeType.ntPositionalArgument,
                                    part[0],
                                )
                                argument.add_child(expression)
                                arguments.add_child(argument)
                        self._accept(")")
                        attribute.add_child(arguments)
                    attributes.add_child(attribute)
                else:
                    self._advance()
                self._accept(",")
                self._ensure_progress(before)
            self._accept("]")
        return attributes

    def _parse_enum(self) -> SyntaxNode:
        token = self._advance()
        type_node = self._node(SyntaxNodeType.ntType, token)
        type_node.set_attribute(AttributeName.anName, "enum")
        while not self._eof() and not self._at(")"):
            before = self.index
            item_token = self._peek()
            name = self._read_name()
            if name:
                item = self._node(SyntaxNodeType.ntEnum, item_token)
                item.add_child(self._valued(SyntaxNodeType.ntName, item_token, name))
                if self._accept("="):
                    value_tokens = self._collect_until({",", ")"})
                    if value_tokens:
                        value = self._node(SyntaxNodeType.ntValue, value_tokens[0])
                        expression = self._expression_direct(value_tokens)
                        if expression is not None:
                            value.add_child(expression)
                        item.add_child(value)
                    else:
                        self._problem(
                            "Expected enum value after '='",
                            item_token,
                        )
                type_node.add_child(item)
            else:
                self._advance()
            self._accept(",")
            self._ensure_progress(before)
        self._accept(")")
        return type_node

    def _parse_array_type(self) -> SyntaxNode:
        token = self._advance()
        type_node = self._node(SyntaxNodeType.ntType, token)
        type_node.set_attribute(AttributeName.anType, "array")
        if self._accept("["):
            bounds = self._node(SyntaxNodeType.ntBounds, self._peek(-1))
            while not self._eof() and not self._at("]"):
                bound_token = self._peek()
                values = self._collect_until({",", "]"})
                bound = self._valued(
                    SyntaxNodeType.ntDimension,
                    bound_token,
                    self._tokens_text(values),
                )
                bounds.add_child(bound)
                self._accept(",")
            self._accept("]")
            type_node.add_child(bounds)
        self._accept("of")
        type_node.add_child(self._parse_type_reference({";", ")", ","}))
        return type_node

    def _parse_procedural_type(self) -> SyntaxNode:
        token = self._advance()
        type_node = self._node(SyntaxNodeType.ntType, token)
        type_node.set_attribute(AttributeName.anType, token.normalized)
        if self._at("("):
            type_node.add_child(self._parse_parameters())
        if token.normalized == "function" and self._accept(":"):
            result = self._node(SyntaxNodeType.ntReturnType, self._peek())
            result.add_child(self._parse_type_reference({";"}))
            type_node.add_child(result)
        return type_node

    def _parse_type_reference(self, stops: set[str]) -> SyntaxNode:
        token = self._peek()
        values = self._collect_until(stops)
        type_node = self._node(SyntaxNodeType.ntType, token)
        name = self._tokens_text(values).strip()
        if name:
            type_node.set_attribute(AttributeName.anName, name)
        return type_node

    def _parse_constants(self, kind: str) -> SyntaxNode:
        section = self._node(SyntaxNodeType.ntConstants, self._advance())
        section.set_attribute(AttributeName.anKind, kind)
        while not self._eof():
            before = self.index
            if self._normalized() in _SECTIONS or self._normalized() in _ROUTINES:
                break
            token = self._peek()
            name = self._read_name()
            if not name:
                self._advance()
                self._ensure_progress(before)
                continue
            constant = self._node(SyntaxNodeType.ntConstant, token)
            constant.add_child(self._valued(SyntaxNodeType.ntName, token, name))
            if self._accept(":"):
                constant.add_child(self._parse_type_annotation({"=", ";"}))
            if self._accept("="):
                value_token = self._peek()
                value = self._collect_until({";"}, max_retained=256)
                value_node = self._node(SyntaxNodeType.ntValue, token)
                expression = self._expression_direct(value)
                if expression is not None:
                    value_node.add_child(expression)
                elif value:
                    value_node.add_child(
                        self._valued(
                            SyntaxNodeType.ntLiteral,
                            value_token,
                            self._tokens_text(value),
                        )
                    )
                constant.add_child(value_node)
            section.add_child(constant)
            self._consume_through(";")
            self._ensure_progress(before)
        return section

    def _parse_type_annotation(self, stops: set[str]) -> SyntaxNode:
        token = self._peek()
        if self._accept("set"):
            type_node = self._node(SyntaxNodeType.ntType, token)
            type_node.set_attribute(AttributeName.anType, "set")
            self._accept("of")
            type_node.add_child(self._parse_type_reference(stops))
            return type_node
        if self._at("array"):
            return self._parse_array_type()
        return self._parse_type_reference(stops)

    def _parse_variables(self, kind: str) -> SyntaxNode:
        section = self._node(SyntaxNodeType.ntVariables, self._advance())
        section.set_attribute(AttributeName.anKind, kind)
        while not self._eof():
            before = self.index
            word = self._normalized()
            if word in _SECTIONS or word in _ROUTINES:
                break
            if not self._is_name(allow_keywords=True):
                self._advance()
                self._ensure_progress(before)
                continue
            for variable in self._parse_named_declaration(SyntaxNodeType.ntVariable):
                section.add_child(variable)
            self._ensure_progress(before)
        return section

    def _parse_named_declaration(
        self,
        typ: SyntaxNodeType,
        *,
        attributes: Optional[SyntaxNode] = None,
    ) -> list[SyntaxNode]:
        names: list[tuple[Token, str]] = []
        while not self._eof():
            token = self._peek()
            name = self._read_name()
            if not name:
                break
            names.append((token, name))
            if not self._accept(","):
                break
        if not self._accept(":"):
            self._problem("Expected ':' in declaration")
            self._consume_through(";")
            result: list[SyntaxNode] = []
            for token, name in names:
                node = self._node(typ, token)
                node.add_child(self._valued(SyntaxNodeType.ntName, token, name))
                if attributes is not None:
                    node.add_child(attributes.clone())
                result.append(node)
            return result
        type_node = self._parse_type_annotation({";", ")", "=", "absolute"})
        if self._accept("="):
            self._skip_balanced_until({";"})
        elif self._accept("absolute"):
            self._skip_balanced_until({";"})
        self._accept(";")
        result: list[SyntaxNode] = []
        for token, name in names:
            node = self._node(typ, token)
            node.add_child(self._valued(SyntaxNodeType.ntName, token, name))
            node.add_child(type_node.clone())
            if attributes is not None:
                node.add_child(attributes.clone())
            result.append(node)
        return result

    def _parse_property(self) -> SyntaxNode:
        token = self._advance()
        node = self._node(SyntaxNodeType.ntProperty, token)
        name = self._read_name()
        node.set_attribute(AttributeName.anName, name)
        if self._at("["):
            for parameter in self._parse_property_parameters():
                node.add_child(parameter)
        if self._accept(":"):
            node.add_child(self._parse_type_reference({";", *_PROPERTY_SPECIFIERS}))

        while not self._eof():
            before = self.index
            word = self._normalized()
            if word == ";":
                self._advance()
                if self._normalized() not in _PROPERTY_SPECIFIERS:
                    break
                continue
            if word == "index":
                specifier = self._node(SyntaxNodeType.ntIndex, self._advance())
                expression = self._property_expression()
                if expression is not None:
                    specifier.add_child(expression)
                node.add_child(specifier)
            elif word in _PROPERTY_ACCESSORS:
                specifier_token = self._advance()
                if word == "read":
                    typ = SyntaxNodeType.ntRead
                elif word == "write":
                    typ = SyntaxNodeType.ntWrite
                else:
                    typ = SyntaxNodeType.ntUnknown
                specifier = self._node(typ, specifier_token)
                if word not in {"read", "write"}:
                    specifier.set_attribute(AttributeName.anKind, word)
                if word not in {"readonly", "writeonly"}:
                    expression = self._property_expression()
                    if expression is not None:
                        specifier.add_child(expression)
                node.add_child(specifier)
            elif word in _PROPERTY_STORAGE:
                specifier_token = self._advance()
                specifier = self._node(
                    SyntaxNodeType.ntDefault
                    if word in {"default", "nodefault"}
                    else SyntaxNodeType.ntUnknown,
                    specifier_token,
                )
                if word == "nodefault":
                    specifier.set_attribute(AttributeName.anKind, word)
                elif word == "stored":
                    specifier.set_attribute(AttributeName.anKind, word)
                if word != "nodefault" and not self._at(";"):
                    expression = self._property_expression()
                    if expression is not None:
                        specifier.add_child(expression)
                node.add_child(specifier)
            elif word in {"dispid", "implements"}:
                specifier_token = self._advance()
                typ = (
                    SyntaxNodeType.ntImplements
                    if word == "implements"
                    else SyntaxNodeType.ntUnknown
                )
                specifier = self._node(typ, specifier_token)
                if word == "dispid":
                    specifier.set_attribute(AttributeName.anKind, word)
                expression = self._property_expression()
                if expression is not None:
                    specifier.add_child(expression)
                node.add_child(specifier)
            else:
                self._problem(f"Unsupported property specifier {self._peek().value!r}")
                self._skip_balanced_until({";"})
            self._ensure_progress(before)
        return node

    def _parse_property_parameters(self) -> list[SyntaxNode]:
        self._advance()
        parameters: list[SyntaxNode] = []
        while not self._eof() and not self._at("]"):
            before = self.index
            modifier = ""
            if self._normalized() in {"const", "var", "out", "constref"}:
                modifier = self._advance().normalized
            names: list[tuple[Token, str]] = []
            while self._is_name(allow_keywords=True):
                name_token = self._peek()
                names.append((name_token, self._read_name()))
                if not self._accept(","):
                    break
            type_node = None
            if self._accept(":"):
                type_node = self._parse_type_reference({";", "]", "="})
            default_tokens: list[Token] = []
            if self._accept("="):
                default_tokens = self._collect_until({";", "]"})
            for name_token, name in names:
                parameter = self._node(SyntaxNodeType.ntParameter, name_token)
                if modifier:
                    parameter.set_attribute(AttributeName.anKind, modifier)
                parameter.add_child(
                    self._valued(SyntaxNodeType.ntName, name_token, name)
                )
                if type_node is not None:
                    parameter.add_child(type_node.clone())
                if default_tokens:
                    value = self._node(SyntaxNodeType.ntValue, default_tokens[0])
                    expression = self._expression_direct(default_tokens)
                    if expression is not None:
                        value.add_child(expression)
                    parameter.add_child(value)
                parameters.append(parameter)
            self._accept(";")
            self._ensure_progress(before)
        self._accept("]")
        return parameters

    def _property_expression(self) -> Optional[SyntaxNode]:
        values = self._collect_until({";", *_PROPERTY_SPECIFIERS})
        return self._expression_direct(values)

    def _expression_direct(self, values: list[Token]) -> Optional[SyntaxNode]:
        expression = self._expression_node(values)
        if expression is not None and len(expression.child_nodes) == 1:
            child = expression.child_nodes[0]
            expression.extract_child(child)
            return child
        return expression

    def _parse_routine(self, *, parse_body: bool) -> SyntaxNode:
        class_token: Optional[Token] = None
        if self._at("class"):
            class_token = self._advance()
        kind_token = self._advance()
        node = self._node(SyntaxNodeType.ntMethod, class_token or kind_token, compound=True)
        node.set_attribute(AttributeName.anKind, kind_token.normalized)
        if class_token is not None:
            node.set_attribute(AttributeName.anClass, "true")
        name = self._read_routine_name()
        node.set_attribute(AttributeName.anName, name)
        self._parse_type_parameters(node)
        if self._at("("):
            node.add_child(self._parse_parameters())
        if kind_token.normalized in {"function", "operator"} and self._accept(":"):
            result = self._node(SyntaxNodeType.ntReturnType, self._peek())
            result.add_child(self._parse_type_reference({";"}))
            node.add_child(result)
        if self._accept("="):
            target_token = self._peek()
            target = self._read_routine_name()
            resolution = self._node(
                SyntaxNodeType.ntResolutionClause,
                target_token,
            )
            resolution.set_attribute(AttributeName.anName, target)
            node.add_child(resolution)
            self._consume_through(";")
            return node
        self._accept(";")
        self._parse_method_directives(node)
        if not parse_body:
            return node

        while (
            self._normalized() in {"label", "const", "type", "var", "threadvar"}
            or self._normalized() in _ROUTINES
            or (
                self._normalized() == "class"
                and self._normalized(1) in _ROUTINES
            )
        ):
            word = self._normalized()
            if word in {"var", "threadvar"}:
                node.add_child(self._parse_variables(word))
            elif word in {"const"}:
                node.add_child(self._parse_constants(word))
            elif word == "type":
                node.add_child(self._parse_type_section())
            elif word in _ROUTINES or (
                word == "class"
                and self._normalized(1) in _ROUTINES
            ):
                node.add_child(self._parse_routine(parse_body=True))
            else:
                self._consume_through(";")
        if self._at("begin"):
            node.add_child(self._parse_begin_block())
            self._accept(";")
        elif self._at("asm"):
            asm = self._node(SyntaxNodeType.ntStatements, self._advance(), compound=True)
            self._synchronize({"end"})
            self._accept("end")
            self._accept(";")
            node.add_child(asm)
        return node

    def _parse_parameters(self) -> SyntaxNode:
        start = self._advance()
        params = self._node(SyntaxNodeType.ntParameters, start)
        while not self._eof() and not self._at(")"):
            before = self.index
            modifier = ""
            if self._normalized() in {"const", "var", "out", "constref"}:
                modifier = self._advance().normalized
            names: list[tuple[Token, str]] = []
            while self._is_name(allow_keywords=True):
                token = self._peek()
                name = self._read_name()
                names.append((token, name))
                if not self._accept(","):
                    break
            type_node = None
            if self._accept(":"):
                type_node = self._parse_type_annotation({";", ")", "="})
            if self._accept("="):
                self._skip_balanced_until({";", ")"})
            for token, name in names:
                param = self._node(SyntaxNodeType.ntParameter, token)
                if modifier:
                    param.set_attribute(AttributeName.anKind, modifier)
                param.add_child(
                    self._valued(SyntaxNodeType.ntName, token, name)
                )
                if type_node is not None:
                    param.add_child(type_node.clone())
                params.add_child(param)
            self._accept(";")
            self._ensure_progress(before)
        self._accept(")")
        return params

    def _parse_method_directives(self, node: SyntaxNode) -> None:
        while not self._eof():
            word = self._normalized()
            if word in _METHOD_DIRECTIVES:
                self._advance()
                attr = _METHOD_DIRECTIVES[word]
                value = "true"
                if word == "deprecated" and self._peek().kind is TokenKind.STRING:
                    value = self._dequote_string(self._advance().value)
                node.set_attribute(attr, value)
                if word == "external":
                    self._skip_balanced_until({";"})
                self._accept(";")
            elif word in {"virtual", "dynamic", "override"}:
                node.set_attribute(AttributeName.anMethodBinding, self._advance().normalized)
                self._accept(";")
            elif word in _CALLING_CONVENTIONS:
                node.set_attribute(AttributeName.anCallingConvention, self._advance().normalized)
                self._accept(";")
            elif word in {"message", "dispid"}:
                token = self._advance()
                message = self._node(SyntaxNodeType.ntMessage, token)
                expression = self._expression_direct(self._collect_until({";"}))
                if expression is not None:
                    message.add_child(expression)
                node.add_child(message)
                self._accept(";")
            elif word in {"experimental", "platform", "library"}:
                self._advance()
                self._accept(";")
            else:
                break

    def _parse_begin_block(self) -> SyntaxNode:
        token = self._advance()
        statements = self._node(SyntaxNodeType.ntStatements, token, compound=True)
        self._parse_statements(statements, {"end"})
        end = self._peek()
        self._accept("end")
        if isinstance(statements, CompoundSyntaxNode):
            statements.end_line = end.line
            statements.end_col = end.column
        return statements

    def _parse_statements(self, parent: SyntaxNode, stops: set[str]) -> None:
        while not self._eof() and self._normalized() not in stops:
            before = self.index
            statement = self._parse_statement()
            if statement is not None:
                parent.add_child(statement)
            self._accept(";")
            self._ensure_progress(before)

    def _parse_statement(self) -> Optional[SyntaxNode]:
        word = self._normalized()
        if word == "begin":
            return self._parse_begin_block()
        control_types = {
            "if": SyntaxNodeType.ntIf,
            "for": SyntaxNodeType.ntFor,
            "while": SyntaxNodeType.ntWhile,
            "with": SyntaxNodeType.ntWith,
        }
        if word in control_types:
            token = self._advance()
            node = self._node(control_types[word], token, compound=True)
            separator = "then" if word == "if" else "do"
            if word == "for":
                separator = "do"
            expression_tokens = self._collect_until(
                {separator, ";", "end"},
                track_angles=False,
            )
            expression = self._expression_direct(expression_tokens)
            if expression is not None:
                node.add_child(expression)
            self._accept(separator)
            child = self._parse_statement()
            if child is not None:
                node.add_child(child)
            if word == "if" and self._accept("else"):
                otherwise = self._node(SyntaxNodeType.ntElse, self._peek(-1))
                child = self._parse_statement()
                if child is not None:
                    otherwise.add_child(child)
                node.add_child(otherwise)
            return node
        if word == "repeat":
            token = self._advance()
            node = self._node(SyntaxNodeType.ntRepeat, token, compound=True)
            self._parse_statements(node, {"until"})
            self._accept("until")
            expression = self._expression_direct(
                self._collect_until({";", "end"}, track_angles=False)
            )
            if expression is not None:
                node.add_child(expression)
            return node
        if word == "try":
            return self._parse_try_statement()
        if word == "case":
            token = self._advance()
            node = self._node(SyntaxNodeType.ntCase, token, compound=True)
            selector = self._node(SyntaxNodeType.ntCaseSelector, token)
            expression = self._expression_direct(
                self._collect_until({"of"}, track_angles=False)
            )
            if expression is not None:
                selector.add_child(expression)
            node.add_child(selector)
            self._accept("of")
            self._parse_statements(node, {"end"})
            self._accept("end")
            return node
        if word == "asm":
            node = self._node(
                SyntaxNodeType.ntStatements,
                self._advance(),
                compound=True,
            )
            self._synchronize({"end"})
            self._accept("end")
            return node
        if word in _STATEMENT_TERMINATORS:
            return None
        token = self._peek()
        values = self._collect_until(
            {";", "end", "else", "until", "except", "finally"},
            track_angles=False,
        )
        if not values:
            return None
        assignment_index = next(
            (index for index, value in enumerate(values) if value.normalized == ":="),
            -1,
        )
        if assignment_index >= 0:
            node = self._node(SyntaxNodeType.ntAssign, token)
            lhs = self._node(SyntaxNodeType.ntLHS, token)
            rhs_token = values[min(assignment_index + 1, len(values) - 1)]
            rhs = self._node(SyntaxNodeType.ntRHS, rhs_token)
            left = self._expression_node(values[:assignment_index])
            right = self._expression_node(values[assignment_index + 1 :])
            if left is not None:
                lhs.add_child(left)
            if right is not None:
                rhs.add_child(right)
            node.add_child(lhs)
            node.add_child(rhs)
            return node
        return self._expression_direct(values)

    def _parse_try_statement(self) -> SyntaxNode:
        token = self._advance()
        node = self._node(SyntaxNodeType.ntTry, token, compound=True)
        self._parse_statements(node, {"except", "finally", "end"})
        if self._accept("except"):
            except_node = self._node(SyntaxNodeType.ntExcept, self._peek(-1))
            while not self._eof() and not self._at("end"):
                before = self.index
                if self._accept("on"):
                    handler = self._node(
                        SyntaxNodeType.ntExceptionHandler,
                        self._peek(-1),
                    )
                    self._collect_until(
                        {"do", ";", "end"},
                        track_angles=False,
                    )
                    self._accept("do")
                    statement = self._parse_statement()
                    if statement is not None:
                        handler.add_child(statement)
                    except_node.add_child(handler)
                elif self._accept("else"):
                    statement = self._parse_statement()
                    if statement is not None:
                        except_node.add_child(statement)
                else:
                    statement = self._parse_statement()
                    if statement is not None:
                        except_node.add_child(statement)
                self._accept(";")
                self._ensure_progress(before)
            node.add_child(except_node)
        elif self._accept("finally"):
            finally_node = self._node(SyntaxNodeType.ntFinally, self._peek(-1))
            self._parse_statements(finally_node, {"end"})
            node.add_child(finally_node)
        self._accept("end")
        return node

    def _expression_node(self, values: list[Token]) -> Optional[SyntaxNode]:
        if not values:
            return None
        if values[0].value == "[":
            close = self._matching_token(values, 0, "[", "]")
            if close == len(values) - 1:
                set_node = self._node(SyntaxNodeType.ntSet, values[0])
                for element_tokens in self._split_top_level_tokens(
                    values[1:close],
                    {","},
                ):
                    element = self._expression_direct(element_tokens)
                    if element is not None:
                        set_node.add_child(element)
                return set_node
        expression = self._node(SyntaxNodeType.ntExpression, values[0])
        index = 0
        while index < len(values):
            token = values[index]
            if token.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD}:
                current: SyntaxNode = self._identifier(token)
                index += 1
                while (
                    index + 1 < len(values)
                    and values[index].value == "."
                    and values[index + 1].kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD}
                ):
                    dot = self._node(SyntaxNodeType.ntDot, values[index])
                    dot.add_child(current)
                    dot.add_child(self._identifier(values[index + 1]))
                    current = dot
                    index += 2
                if index < len(values) and values[index].value == "(":
                    close = self._matching_token(values, index, "(", ")")
                    if close is not None:
                        call = self._node(SyntaxNodeType.ntCall, token)
                        call.add_child(current)
                        arguments = self._node(SyntaxNodeType.ntArguments, values[index])
                        for argument_tokens in self._split_arguments(values[index + 1 : close]):
                            argument_expression = self._expression_direct(argument_tokens)
                            if argument_expression is None:
                                continue
                            argument = self._node(
                                SyntaxNodeType.ntPositionalArgument,
                                argument_tokens[0],
                            )
                            argument.add_child(argument_expression)
                            arguments.add_child(argument)
                        call.add_child(arguments)
                        current = call
                        index = close + 1
                elif index < len(values) and values[index].value == "[":
                    close = self._matching_token(values, index, "[", "]")
                    if close is not None:
                        indexed = self._node(SyntaxNodeType.ntIndexed, values[index])
                        indexed.add_child(current)
                        indices = self._node(SyntaxNodeType.ntExpressions, values[index])
                        for index_tokens in self._split_top_level_tokens(
                            values[index + 1 : close],
                            {","},
                        ):
                            index_expression = self._expression_direct(index_tokens)
                            if index_expression is not None:
                                indices.add_child(index_expression)
                        indexed.add_child(indices)
                        current = indexed
                        index = close + 1
                while index < len(values) and values[index].value == "^":
                    deref = self._node(SyntaxNodeType.ntDeref, values[index])
                    deref.add_child(current)
                    current = deref
                    index += 1
                expression.add_child(current)
                continue
            elif token.kind in {TokenKind.NUMBER, TokenKind.STRING, TokenKind.CHAR_CODE}:
                expression.add_child(self._literal(token))
            index += 1
        return expression

    def _literal(self, token: Token) -> ValuedSyntaxNode:
        value = token.value
        if token.kind is TokenKind.STRING:
            value = self._dequote_string(value)
            literal_type = "string"
        elif token.kind is TokenKind.CHAR_CODE:
            literal_type = "char"
        else:
            literal_type = "numeric"
        node = self._valued(SyntaxNodeType.ntLiteral, token, value)
        node.set_attribute(AttributeName.anType, literal_type)
        return node

    @staticmethod
    def _dequote_string(value: str) -> str:
        if value.startswith("'"):
            value = value[1:-1] if value.endswith("'") else value[1:]
            value = value.replace("''", "'")
        return value

    def _identifier(self, token: Token) -> SyntaxNode:
        node = self._node(SyntaxNodeType.ntIdentifier, token)
        node.set_attribute(AttributeName.anName, token.value)
        return node

    def _matching_token(
        self,
        values: list[Token],
        start: int,
        opening: str,
        closing: str,
    ) -> Optional[int]:
        depth = 0
        for index in range(start, len(values)):
            value = values[index].value
            if value == opening:
                depth += 1
            elif value == closing:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _top_level_token_index(
        self,
        values: list[Token],
        expected: str,
    ) -> Optional[int]:
        round_depth = 0
        square_depth = 0
        angle_depth = 0
        for index, token in enumerate(values):
            if (
                token.value == expected
                and round_depth == square_depth == angle_depth == 0
            ):
                return index
            if token.value == "(":
                round_depth += 1
            elif token.value == ")":
                round_depth = max(0, round_depth - 1)
            elif token.value == "[":
                square_depth += 1
            elif token.value == "]":
                square_depth = max(0, square_depth - 1)
            elif token.value == "<":
                angle_depth += 1
            elif token.value == ">":
                angle_depth = max(0, angle_depth - 1)
        return None

    def _split_top_level_tokens(
        self,
        values: list[Token],
        separators: set[str],
    ) -> list[list[Token]]:
        if not values:
            return []
        result: list[list[Token]] = []
        current: list[Token] = []
        round_depth = 0
        square_depth = 0
        angle_depth = 0
        for token in values:
            if (
                token.value in separators
                and round_depth == square_depth == angle_depth == 0
            ):
                result.append(current)
                current = []
                continue
            current.append(token)
            if token.value == "(":
                round_depth += 1
            elif token.value == ")":
                round_depth = max(0, round_depth - 1)
            elif token.value == "[":
                square_depth += 1
            elif token.value == "]":
                square_depth = max(0, square_depth - 1)
            elif token.value == "<":
                angle_depth += 1
            elif token.value == ">":
                angle_depth = max(0, angle_depth - 1)
        result.append(current)
        return result

    def _split_arguments(self, values: list[Token]) -> list[list[Token]]:
        return [
            part
            for part in self._split_top_level_tokens(values, {","})
            if part
        ]

    def _read_qualified_name(self, *, allow_keywords: bool = False) -> str:
        if not self._is_name(allow_keywords=allow_keywords):
            return ""
        parts = [self._advance().value]
        while self._at(".") and self._is_name(1, allow_keywords=allow_keywords):
            self._advance()
            parts.append(self._advance().value)
        return ".".join(parts)

    def _read_routine_name(self) -> str:
        if not self._is_name(allow_keywords=True):
            return ""
        parts = [self._advance().value]
        while not self._eof():
            if self._at("<"):
                close = self._matching_stream_token("<", ">")
                if close is None or self._normalized_at(close + 1) != ".":
                    break
                self.index = close + 1
            if not self._accept("."):
                break
            if not self._is_name(allow_keywords=True):
                break
            parts.append(self._advance().value)
        return ".".join(parts)

    def _matching_stream_token(
        self,
        opening: str,
        closing: str,
    ) -> Optional[int]:
        if not self._at(opening):
            return None
        depth = 0
        for index in range(self.index, len(self.tokens)):
            value = self.tokens[index].normalized
            if value == opening:
                depth += 1
            elif value == closing:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _normalized_at(self, index: int) -> str:
        if 0 <= index < len(self.tokens):
            return self.tokens[index].normalized
        return ""

    def _read_name(self) -> str:
        if not self._is_name(allow_keywords=True):
            return ""
        return self._advance().value

    def _collect_until(
        self,
        stops: set[str],
        *,
        max_retained: Optional[int] = None,
        track_angles: bool = True,
    ) -> list[Token]:
        values: list[Token] = []
        round_depth = 0
        square_depth = 0
        angle_depth = 0
        previous: Optional[Token] = None
        while not self._eof():
            word = self._normalized()
            if round_depth == square_depth == angle_depth == 0 and word in stops:
                break
            token = self._advance()
            if max_retained is None or len(values) < max_retained:
                values.append(token)
            if token.value == "(":
                round_depth += 1
            elif token.value == ")":
                round_depth = max(0, round_depth - 1)
            elif token.value == "[":
                square_depth += 1
            elif token.value == "]":
                square_depth = max(0, square_depth - 1)
            elif (
                track_angles
                and token.value == "<"
                and previous is not None
                and previous.kind is TokenKind.IDENTIFIER
                and previous.end == token.start
            ):
                angle_depth += 1
            elif track_angles and token.value == ">":
                angle_depth = max(0, angle_depth - 1)
            if token.normalized in {";", "then", "do", "begin", "end"}:
                angle_depth = 0
            previous = token
        return values

    def _skip_balanced_until(self, stops: set[str]) -> None:
        self._collect_until(stops)

    def _tokens_text(self, values: Iterable[Token]) -> str:
        result: list[str] = []
        previous: Optional[Token] = None
        for token in values:
            if previous is not None and (
                previous.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD, TokenKind.NUMBER}
                and token.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD, TokenKind.NUMBER}
            ):
                result.append(" ")
            result.append(token.value)
            previous = token
        return "".join(result)

    def _synchronize(self, stops: set[str]) -> None:
        while not self._eof() and self._normalized() not in stops:
            self._advance()

    def _consume_through(self, value: str) -> None:
        self._synchronize({value})
        self._accept(value)

    def _problem(self, message: str, token: Optional[Token] = None) -> None:
        if len(self.problems) >= 128:
            return
        current = token or self._peek()
        self.problems.append(
            DelphiAstParseProblem(
                message=message[:240],
                line=current.line,
                column=current.column,
            )
        )

    def _node(
        self,
        typ: SyntaxNodeType,
        token: Token,
        *,
        compound: bool = False,
    ) -> SyntaxNode:
        node: SyntaxNode = CompoundSyntaxNode(typ) if compound else SyntaxNode(typ)
        node.file_name = self.file_name
        node.line = token.line
        node.col = token.column
        return node

    def _valued(self, typ: SyntaxNodeType, token: Token, value: str) -> ValuedSyntaxNode:
        node = ValuedSyntaxNode(typ)
        node.file_name = self.file_name
        node.line = token.line
        node.col = token.column
        node.value = value
        return node

    def _peek(self, offset: int = 0) -> Token:
        position = min(max(self.index + offset, 0), len(self.tokens) - 1)
        return self.tokens[position]

    def _normalized(self, offset: int = 0) -> str:
        return self._peek(offset).normalized

    def _at(self, value: str, offset: int = 0) -> bool:
        return self._normalized(offset) == value.casefold()

    def _accept(self, value: str) -> bool:
        if not self._at(value):
            return False
        self._advance()
        return True

    def _advance(self) -> Token:
        token = self._peek()
        if token.kind is not TokenKind.EOF:
            self.index += 1
        return token

    def _eof(self) -> bool:
        return self._peek().kind is TokenKind.EOF

    def _is_name(self, offset: int = 0, *, allow_keywords: bool = False) -> bool:
        kind = self._peek(offset).kind
        return kind is TokenKind.IDENTIFIER or (allow_keywords and kind is TokenKind.KEYWORD)

    def _ensure_progress(self, before: int) -> None:
        if self.index <= before and not self._eof():
            self._problem("Parser recovery forced forward progress")
            self._advance()


__all__ = [
    "DelphiAstParseOutput",
    "DelphiAstParseProblem",
    "DelphiAstParser",
]
