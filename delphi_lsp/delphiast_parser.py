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
            declaration.add_child(self._parse_type())
            section.add_child(declaration)
            self._accept(";")
            self._ensure_progress(before)
        return section

    def _parse_type_parameters(self, declaration: SyntaxNode) -> None:
        if not self._accept("<"):
            return
        params = self._node(SyntaxNodeType.ntTypeParams, self._peek(-1))
        while not self._eof() and not self._at(">"):
            token = self._peek()
            name = self._read_name()
            if name:
                param = self._valued(SyntaxNodeType.ntTypeParam, token, name)
                params.add_child(param)
            else:
                self._advance()
            self._accept(",")
        self._accept(">")
        declaration.add_child(params)

    def _parse_type(self) -> SyntaxNode:
        token = self._peek()
        packed = self._accept("packed")
        kind = self._normalized()
        if kind in {"class", "record", "object", "interface", "dispinterface"}:
            self._advance()
            type_node = self._node(SyntaxNodeType.ntType, token)
            type_node.set_attribute(AttributeName.anType, "record" if kind == "object" else kind)
            if self._accept("abstract"):
                type_node.set_attribute(AttributeName.anAbstract, "true")
            self._parse_base_types(type_node)
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
            elif word in _ROUTINES or (word == "class" and self._normalized(1) in _ROUTINES):
                active_parent.add_child(self._parse_routine(parse_body=False))
            elif word == "property":
                active_parent.add_child(self._parse_property())
            elif self._is_name():
                fields = self._parse_named_declaration(SyntaxNodeType.ntField)
                for field in fields:
                    active_parent.add_child(field)
            else:
                if self._peek().kind is TokenKind.UNKNOWN:
                    self._problem(f"Unsupported type member token {self._peek().value!r}")
                self._advance()
            self._ensure_progress(before)
        self._accept("end")

    def _parse_enum(self) -> SyntaxNode:
        token = self._advance()
        type_node = self._node(SyntaxNodeType.ntType, token)
        type_node.set_attribute(AttributeName.anName, "enum")
        while not self._eof() and not self._at(")"):
            before = self.index
            item_token = self._peek()
            name = self._read_name()
            if name:
                item = self._valued(SyntaxNodeType.ntEnum, item_token, name)
                if self._accept("="):
                    self._skip_balanced_until({",", ")"})
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
            constant = self._valued(SyntaxNodeType.ntConstant, token, name)
            if self._accept(":"):
                constant.add_child(self._parse_type_reference({"=", ";"}))
            if self._accept("="):
                value_token = self._peek()
                value = self._collect_until({";"})
                constant.add_child(
                    self._valued(SyntaxNodeType.ntValue, value_token, self._tokens_text(value))
                )
            section.add_child(constant)
            self._consume_through(";")
            self._ensure_progress(before)
        return section

    def _parse_variables(self, kind: str) -> SyntaxNode:
        section = self._node(SyntaxNodeType.ntVariables, self._advance())
        section.set_attribute(AttributeName.anKind, kind)
        while not self._eof():
            before = self.index
            word = self._normalized()
            if word in _SECTIONS or word in _ROUTINES:
                break
            if not self._is_name():
                self._advance()
                self._ensure_progress(before)
                continue
            for variable in self._parse_named_declaration(SyntaxNodeType.ntVariable):
                section.add_child(variable)
            self._ensure_progress(before)
        return section

    def _parse_named_declaration(self, typ: SyntaxNodeType) -> list[SyntaxNode]:
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
            return [self._valued(typ, token, name) for token, name in names]
        type_node = self._parse_type_reference({";", "=", "absolute"})
        if self._accept("="):
            self._skip_balanced_until({";"})
        elif self._accept("absolute"):
            self._skip_balanced_until({";"})
        self._accept(";")
        result: list[SyntaxNode] = []
        for token, name in names:
            node = self._valued(typ, token, name)
            node.add_child(type_node.clone())
            result.append(node)
        return result

    def _parse_property(self) -> SyntaxNode:
        token = self._advance()
        node = self._node(SyntaxNodeType.ntProperty, token)
        name = self._read_name()
        node.set_attribute(AttributeName.anName, name)
        self._skip_balanced_until({";"})
        self._accept(";")
        return node

    def _parse_routine(self, *, parse_body: bool) -> SyntaxNode:
        class_token: Optional[Token] = None
        if self._at("class"):
            class_token = self._advance()
        kind_token = self._advance()
        node = self._node(SyntaxNodeType.ntMethod, class_token or kind_token, compound=True)
        node.set_attribute(AttributeName.anKind, kind_token.normalized)
        if class_token is not None:
            node.set_attribute(AttributeName.anClass, "true")
        name = self._read_qualified_name(allow_keywords=True)
        node.set_attribute(AttributeName.anName, name)
        self._parse_type_parameters(node)
        if self._at("("):
            node.add_child(self._parse_parameters())
        if kind_token.normalized in {"function", "operator"} and self._accept(":"):
            result = self._node(SyntaxNodeType.ntReturnType, self._peek())
            result.add_child(self._parse_type_reference({";"}))
            node.add_child(result)
        self._accept(";")
        self._parse_method_directives(node)
        if not parse_body:
            return node

        while self._normalized() in {"label", "const", "type", "var", "threadvar"}:
            word = self._normalized()
            if word in {"var", "threadvar"}:
                node.add_child(self._parse_variables(word))
            elif word in {"const"}:
                node.add_child(self._parse_constants(word))
            elif word == "type":
                node.add_child(self._parse_type_section())
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
            while self._is_name():
                token = self._peek()
                name = self._read_name()
                names.append((token, name))
                if not self._accept(","):
                    break
            type_node = None
            if self._accept(":"):
                type_node = self._parse_type_reference({";", ")", "="})
            if self._accept("="):
                self._skip_balanced_until({";", ")"})
            for token, name in names:
                param = self._valued(SyntaxNodeType.ntParameter, token, name)
                if modifier:
                    param.set_attribute(AttributeName.anKind, modifier)
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
                    value = self._advance().value.strip("'")
                node.set_attribute(attr, value)
                self._consume_through(";")
            elif word in {"virtual", "dynamic", "override"}:
                node.set_attribute(AttributeName.anMethodBinding, self._advance().normalized)
                self._consume_through(";")
            elif word in _CALLING_CONVENTIONS:
                node.set_attribute(AttributeName.anCallingConvention, self._advance().normalized)
                self._consume_through(";")
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
            expression_tokens = self._collect_until({separator, ";", "end"})
            expression = self._expression_node(expression_tokens)
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
            expression = self._expression_node(self._collect_until({";", "end"}))
            if expression is not None:
                node.add_child(expression)
            return node
        if word in {"try", "case"}:
            token = self._advance()
            typ = SyntaxNodeType.ntTry if word == "try" else SyntaxNodeType.ntCase
            node = self._node(typ, token, compound=True)
            if word == "case":
                selector = self._node(SyntaxNodeType.ntCaseSelector, token)
                expression = self._expression_node(self._collect_until({"of"}))
                if expression is not None:
                    selector.add_child(expression)
                node.add_child(selector)
                self._accept("of")
            self._parse_statements(node, {"end"})
            self._accept("end")
            return node
        if word in _STATEMENT_TERMINATORS:
            return None
        token = self._peek()
        values = self._collect_until({";", "end", "else", "until"})
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
        node = self._node(SyntaxNodeType.ntStatement, token)
        expression = self._expression_node(values)
        if expression is not None:
            node.add_child(expression)
        return node

    def _expression_node(self, values: list[Token]) -> Optional[SyntaxNode]:
        if not values:
            return None
        expression = self._node(SyntaxNodeType.ntExpression, values[0])
        for token in values:
            if token.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD}:
                expression.add_child(
                    self._valued(SyntaxNodeType.ntIdentifier, token, token.value)
                )
            elif token.kind in {TokenKind.NUMBER, TokenKind.STRING, TokenKind.CHAR_CODE}:
                expression.add_child(self._valued(SyntaxNodeType.ntLiteral, token, token.value))
        return expression

    def _read_qualified_name(self, *, allow_keywords: bool = False) -> str:
        if not self._is_name(allow_keywords=allow_keywords):
            return ""
        parts = [self._advance().value]
        while self._at(".") and self._is_name(1, allow_keywords=allow_keywords):
            self._advance()
            parts.append(self._advance().value)
        return ".".join(parts)

    def _read_name(self) -> str:
        if not self._is_name(allow_keywords=True):
            return ""
        return self._advance().value

    def _collect_until(self, stops: set[str]) -> list[Token]:
        values: list[Token] = []
        round_depth = 0
        square_depth = 0
        angle_depth = 0
        while not self._eof():
            word = self._normalized()
            if round_depth == square_depth == angle_depth == 0 and word in stops:
                break
            token = self._advance()
            values.append(token)
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
        return values

    def _skip_balanced_until(self, stops: set[str]) -> None:
        self._collect_until(stops)

    def _tokens_text(self, values: Iterable[Token]) -> str:
        result = ""
        previous: Optional[Token] = None
        for token in values:
            if previous is not None and (
                previous.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD, TokenKind.NUMBER}
                and token.kind in {TokenKind.IDENTIFIER, TokenKind.KEYWORD, TokenKind.NUMBER}
            ):
                result += " "
            result += token.value
            previous = token
        return result

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
