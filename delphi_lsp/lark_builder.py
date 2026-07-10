from __future__ import annotations

from typing import Any, Callable, Iterable

from .consts import AttributeName, SyntaxNodeType
from .nodes import CompoundSyntaxNode, SyntaxNode, ValuedSyntaxNode


def build_syntax_tree(
    tree: Any,
    file_name: str,
    *,
    string_transform: Callable[[str], str] | None = None,
) -> SyntaxNode:
    try:
        from lark import Transformer, v_args
        from lark.lexer import Token
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError('lark is required to build the syntax tree') from exc

    def is_token(value: Any) -> bool:
        return isinstance(value, Token)

    def apply_string(value: str, token_type: str | None = None) -> str:
        if string_transform is None:
            return value
        if token_type is None or token_type in {
            'NAME',
            'GENERIC_NAME',
            'SPACED_GENERIC_NAME',
            'STRICT_NAME',
            'STRING_LITERAL',
            'STRING_BLOCK3',
            'STRING_BLOCK5',
        }:
            return string_transform(value)
        return value

    def token_value(value: Any) -> str:
        if is_token(value):
            return apply_string(value.value, value.type)
        return apply_string(str(value))

    @v_args(meta=True, inline=True)
    class Builder(Transformer):
        def __init__(self) -> None:
            super().__init__()
            self.file_name = file_name

        def __default__(self, data: str, children: list[Any], meta: Any) -> Any:
            items: list[Any] = []
            for child in children:
                items.extend(self._flatten(child))
            nodes = [child for child in items if isinstance(child, SyntaxNode)]
            if not nodes:
                return None
            if len(nodes) == 1:
                return nodes[0]
            return nodes

        def __default_token__(self, token: Any) -> Any:
            return token

        def unit_file(self, meta: Any, *children: Any) -> SyntaxNode:
            unit_name = 'unit'
            directives: dict[str, str] = {}
            for item in self._flatten(children):
                if self._is_text(item):
                    unit_name = item
                    break
            for item in self._flatten(children):
                if isinstance(item, dict):
                    directives.update(item)
            root = self._make_node(SyntaxNodeType.ntUnit, meta)
            root.set_attribute(AttributeName.anName, unit_name)
            self._apply_decl_directives(root, directives)
            for child in children:
                if isinstance(child, SyntaxNode):
                    root.add_child(child)
            return root

        def include_file(self, meta: Any, *children: Any) -> SyntaxNode:
            name = file_name.replace('\\', '/').rsplit('/', 1)[-1].rsplit('.', 1)[0] or 'include'
            root = self._make_node(SyntaxNodeType.ntUnit, meta)
            root.set_attribute(AttributeName.anName, name)
            for child in self._flatten(children):
                if isinstance(child, SyntaxNode):
                    root.add_child(child)
            return root

        def program_file(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_program_root(meta, 'program', children)

        def library_file(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_program_root(meta, 'library', children)

        def package_file(self, meta: Any, *children: Any) -> SyntaxNode:
            unit_name = self._extract_name(children, default='package')
            root = self._make_node(SyntaxNodeType.ntPackage, meta)
            root.set_attribute(AttributeName.anName, unit_name)
            for child in children:
                if isinstance(child, SyntaxNode):
                    root.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            root.add_child(item)
            return root

        def unit_header(self, meta: Any, *children: Any) -> list[Any]:
            name = 'unit'
            directives: dict[str, str] = {}
            for child in children:
                if self._is_text(child):
                    name = child
                    break
            for child in children:
                if isinstance(child, dict):
                    directives.update(child)
            return [name, directives]

        def program_header(self, meta: Any, *children: Any) -> list[Any]:
            name = self._extract_name(children, default='program')
            nodes = [child for child in children if isinstance(child, SyntaxNode)]
            return [name, *nodes]

        def library_header(self, meta: Any, *children: Any) -> list[Any]:
            name = self._extract_name(children, default='library')
            nodes = [child for child in children if isinstance(child, SyntaxNode)]
            return [name, *nodes]

        def package_header(self, meta: Any, *children: Any) -> str:
            return self._extract_name(children, default='package')

        def unit_directive_list(self, meta: Any, *children: Any) -> dict[str, str]:
            attrs: dict[str, str] = {}
            for child in children:
                if isinstance(child, tuple):
                    key, value = child
                    attrs[key] = value
            return attrs

        def unit_directive(self, meta: Any, *children: Any) -> tuple[str, str]:
            return self.decl_directive(meta, *children)

        def program_body(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in self._flatten(children) if isinstance(child, SyntaxNode)]

        def package_body(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in self._flatten(children) if isinstance(child, SyntaxNode)]

        def interface_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntInterface, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def interface_preamble(self, meta: Any, *_: Any) -> None:
            return None

        def implementation_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntImplementation, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def initialization_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntInitialization, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def bare_initialization_section(self, meta: Any, *children: Any) -> SyntaxNode:
            return self.initialization_section(meta, *children)

        def finalization_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntFinalization, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def decl_sections(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            nodes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            nodes.append(item)
                elif isinstance(child, SyntaxNode):
                    nodes.append(child)
            return nodes

        def interface_decl_sections(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return self.decl_sections(meta, *children)

        def implementation_decl_sections(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return self.decl_sections(meta, *children)

        def ignored_proc_type_directive(self, meta: Any, *_: Any) -> None:
            return None

        def ignored_compiler_error_line(self, meta: Any, *_: Any) -> None:
            return None

        def block_decl_sections(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return self.decl_sections(meta, *children)

        def uses_clause(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntUses, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def uses_item(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            path = None
            for child in children:
                if self._is_text(child):
                    name = child
                elif is_token(child) and child.type == 'STRING_LITERAL':
                    path = self._dequote_string(child.value)
                elif isinstance(child, ValuedSyntaxNode):
                    path = child.value
            if name is None:
                name = 'unit'
            node = self._make_node(SyntaxNodeType.ntUnit, meta)
            node.set_attribute(AttributeName.anName, name)
            if path:
                node.set_attribute(AttributeName.anPath, path)
            return node

        def label_section(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            labels: list[SyntaxNode] = []
            for child in self._flatten(children):
                if isinstance(child, str):
                    node = self._make_node(SyntaxNodeType.ntLabel, meta)
                    node.set_attribute(AttributeName.anName, child)
                    labels.append(node)
            return labels

        def label_list(self, meta: Any, *children: Any) -> list[str]:
            values: list[str] = []
            for child in children:
                if self._is_text(child):
                    values.append(child)
                elif is_token(child):
                    values.append(child.value)
            return values

        def label(self, meta: Any, token: Any) -> str:
            return token_value(token)

        def const_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntConstants, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def resourcestring_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntResourceString, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def const_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            attributes: list[SyntaxNode] = []
            directives: dict[str, str] = {}
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, dict):
                    directives.update(child)
                elif self._is_text(child):
                    name = child
                elif is_token(child) and child.type == 'NAME':
                    name = child.value
            if name is None:
                name = 'const'
            node = self._make_node(SyntaxNodeType.ntConstant, meta)
            node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
            for attr in attributes:
                node.add_child(attr)
            for child in children:
                if isinstance(child, SyntaxNode):
                    if child.typ == SyntaxNodeType.ntType:
                        node.add_child(child)
                    else:
                        value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                        value_node.add_child(child)
                        node.add_child(value_node)
            self._apply_decl_directives(node, directives)
            return node

        def var_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntVariables, meta)
            for child in children:
                if isinstance(child, list):
                    for var_node in child:
                        node.add_child(var_node)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def threadvar_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self.var_section(meta, *children)
            node.set_attribute(AttributeName.anKind, 'threadvar')
            return node

        def class_var_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self.var_section(meta, *children)
            node.set_attribute(AttributeName.anKind, 'class var')
            return node

        def class_threadvar_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self.var_section(meta, *children)
            node.set_attribute(AttributeName.anKind, 'class threadvar')
            return node

        def var_decl(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            names: list[str] = []
            type_node = None
            value_expr = None
            absolute_expr = None
            attributes: list[SyntaxNode] = []
            directives: dict[str, str] = {}
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, list):
                    names = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    type_node = child
                elif isinstance(child, dict):
                    directives.update(child)
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAbsolute:
                    absolute_expr = child
                elif isinstance(child, SyntaxNode):
                    value_expr = child
            variables: list[SyntaxNode] = []
            if type_node is None:
                return variables
            if directives:
                self._apply_directives(type_node, directives)
            for name in names:
                node = self._make_node(SyntaxNodeType.ntVariable, meta)
                node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                node.add_child(type_node.clone())
                for attr in attributes:
                    node.add_child(attr)
                if absolute_expr is not None:
                    node.add_child(absolute_expr.clone())
                if value_expr is not None:
                    value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                    value_node.add_child(value_expr)
                    node.add_child(value_node)
                variables.append(node)
            return variables

        def absolute_spec(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntAbsolute, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def type_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntTypeSection, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def type_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            type_node = None
            type_params = None
            attributes: list[SyntaxNode] = []
            directives: dict[str, str] = {}
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, dict):
                    directives.update(child)
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntTypeParams:
                    type_params = child
                elif self._is_text(child):
                    name = child
                elif is_token(child) and child.type in {'NAME', 'GENERIC_NAME'}:
                    name = child.value
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    type_node = child
            if name is None or type_node is None:
                return self._make_compound(SyntaxNodeType.ntTypeDecl, meta)
            if '<' in name:
                base_name, generic_args = self._split_generic_name(name)
                name = base_name
                if type_params is None and generic_args:
                    type_params = self._type_params_from_generic_name(meta, generic_args)
            node = self._make_compound(SyntaxNodeType.ntTypeDecl, meta)
            node.set_attribute(AttributeName.anName, name)
            for attr in attributes:
                node.add_child(attr)
            if type_params is not None:
                node.add_child(type_params)
            if directives:
                self._apply_directives(type_node, directives)
            node.add_child(type_node)
            self._apply_decl_directives(node, directives)
            return node

        def incomplete_type_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            type_params = None
            attributes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntTypeParams:
                    type_params = child
                elif self._is_text(child):
                    name = child
                elif is_token(child) and child.type in {'NAME', 'GENERIC_NAME'}:
                    name = child.value
            node = self._make_compound(SyntaxNodeType.ntTypeDecl, meta)
            if name is not None:
                node.set_attribute(AttributeName.anName, name)
            for attr in attributes:
                node.add_child(attr)
            if type_params is not None:
                node.add_child(type_params)
            unknown = self._make_node(SyntaxNodeType.ntType, meta)
            unknown.set_attribute(AttributeName.anType, 'unknown')
            node.add_child(unknown)
            return node

        def type_decl_name(self, meta: Any, token: Any) -> str:
            return token_value(token)

        def decl_directive_list(self, meta: Any, *children: Any) -> dict[str, str]:
            attrs: dict[str, str] = {}
            for child in children:
                if isinstance(child, tuple):
                    key, value = child
                    attrs[key] = value
            return attrs

        def decl_directive(self, meta: Any, *children: Any) -> tuple[str, str]:
            token = None
            str_token = None
            for child in children:
                if is_token(child) and child.type in {'DEPRECATED', 'PLATFORM', 'EXPERIMENTAL', 'LIBRARY'}:
                    token = child
                elif is_token(child) and child.type == 'STRING_LITERAL':
                    str_token = child
            if token is None:
                return ('', '')
            key = token.type.lower()
            if key == 'deprecated' and str_token is not None:
                return ('deprecated', self._dequote_string(str_token.value))
            return (key, 'true')

        def simple_type(self, meta: Any, *children: Any) -> SyntaxNode:
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    return child
                if self._is_text(child):
                    node = self._make_node(SyntaxNodeType.ntType, meta)
                    node.set_attribute(AttributeName.anName, child)
                    return node
            return self._make_node(SyntaxNodeType.ntType, meta)

        def type_name(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            type_args = None
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntTypeArgs:
                    type_args = child
                elif self._is_text(child):
                    name = child
            node = self._make_node(SyntaxNodeType.ntType, meta)
            if name is not None:
                node.set_attribute(AttributeName.anName, name)
            if type_args is not None:
                node.add_child(type_args)
            return node

        def pointer_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'pointer')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif is_token(child) and child.type == 'POINTER_CHAR':
                    target = self._make_node(SyntaxNodeType.ntType, meta)
                    target.set_attribute(AttributeName.anName, child.value[1:])
                    node.add_child(target)
            return node

        def distinct_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'distinct')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def array_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'array')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def array_of_const_type(self, meta: Any, *_: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'array')
            const_node = self._make_node(SyntaxNodeType.ntType, meta)
            const_node.set_attribute(AttributeName.anName, 'const')
            node.add_child(const_node)
            return node

        def array_bounds(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntBounds, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def array_bound(self, meta: Any, *children: Any) -> SyntaxNode:
            nodes = [child for child in children if isinstance(child, SyntaxNode)]
            if len(nodes) == 2:
                subrange = self._make_node(SyntaxNodeType.ntSubrange, meta)
                subrange.add_child(nodes[0])
                subrange.add_child(nodes[1])
                return subrange
            if nodes:
                return nodes[0]
            return self._make_node(SyntaxNodeType.ntExpression, meta)

        def set_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'set')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def enum_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anName, 'enum')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def enum_item(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            value_expr = None
            for child in children:
                if self._is_text(child):
                    name = child
                elif isinstance(child, SyntaxNode):
                    value_expr = child
            node = self._make_node(SyntaxNodeType.ntEnum, meta)
            if name:
                node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
            if value_expr is not None:
                value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                value_node.add_child(value_expr)
                node.add_child(value_node)
            return node

        def string_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anName, 'string')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def file_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'file')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def class_of_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'class of')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def reference_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'reference')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def packed_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'packed')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def subrange_type(self, meta: Any, lower: Any, upper: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anName, 'subrange')
            bounds = self._make_node(SyntaxNodeType.ntBounds, meta)
            bounds.add_child(self._ensure_expr_node(meta, lower))
            bounds.add_child(self._ensure_expr_node(meta, upper))
            node.add_child(bounds)
            return node

        def class_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'class')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def forward_class_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'class')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def forward_interface_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'interface')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def forward_dispinterface_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'dispinterface')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def record_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'record')
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, tuple) and item and item[0] == 'align':
                            node.set_attribute(AttributeName.anAlign, item[1])
                        else:
                            node.add_child(item)
                elif isinstance(child, tuple) and child and child[0] == 'align':
                    node.set_attribute(AttributeName.anAlign, child[1])
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def record_align(self, meta: Any, *children: Any) -> tuple[str, str]:
            for child in children:
                if isinstance(child, SyntaxNode):
                    value = self._expr_to_text(child)
                    if value:
                        return ('align', value)
                if self._is_text(child) or is_token(child):
                    return ('align', token_value(child))
            return ('align', 'true')

        def object_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'object')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        node.add_child(item)
            return node

        def interface_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            token = children[0] if children else None
            if is_token(token) and token.type == 'DISPINTERFACE':
                node.set_attribute(AttributeName.anType, 'dispinterface')
                start_index = 1
            else:
                node.set_attribute(AttributeName.anType, 'interface')
                start_index = 0
            for child in children[start_index:]:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        node.add_child(item)
            return node

        def helper_type(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, 'helper')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        node.add_child(item)
            return node

        def interface_guid(self, meta: Any, token: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntGuid, meta)
            if is_token(token) and token.type == 'STRING_LITERAL':
                node.set_attribute(AttributeName.anName, self._dequote_string(token.value))
            return node

        def type_heritage(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            bases = []
            for name in children:
                if isinstance(name, SyntaxNode) and name.typ == SyntaxNodeType.ntType:
                    bases.append(name)
                elif self._is_text(name):
                    base = self._make_node(SyntaxNodeType.ntType, meta)
                    base.set_attribute(AttributeName.anName, name)
                    bases.append(base)
            return bases

        def type_params(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntTypeParams, meta)
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def type_param(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            names: list[str] = []
            constraints = None
            for child in children:
                if isinstance(child, tuple):
                    tuple_names, tuple_constraints = child
                    names = list(tuple_names)
                    constraints = tuple_constraints
                elif isinstance(child, list):
                    names = [token_value(item) for item in child]
                elif self._is_text(child):
                    names.append(child)
                elif is_token(child) and child.type == 'NAME':
                    names.append(token_value(child))
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntConstraints:
                    constraints = child
            params: list[SyntaxNode] = []
            for name in names:
                node = self._make_node(SyntaxNodeType.ntTypeParam, meta)
                node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                if constraints is not None:
                    node.add_child(constraints.clone())
                params.append(node)
            return params

        def constrained_name_list(self, meta: Any, *children: Any) -> list[str]:
            return [token_value(child) for child in children]

        def constrained_type_param(self, meta: Any, names: Any, constraints: Any) -> tuple[list[str], SyntaxNode | None]:
            resolved_names: list[str] = []
            if isinstance(names, list):
                resolved_names = [token_value(name) for name in names]
            elif self._is_text(names):
                resolved_names = [names]
            elif is_token(names):
                resolved_names = [token_value(names)]
            constraint_node = constraints if isinstance(constraints, SyntaxNode) else None
            return (resolved_names, constraint_node)

        def type_constraints(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntConstraints, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def type_constraint(self, meta: Any, *children: Any) -> SyntaxNode:
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    return child
                if is_token(child):
                    if child.type == 'CLASS':
                        return self._make_node(SyntaxNodeType.ntClassConstraint, meta)
                    if child.type == 'RECORD':
                        return self._make_node(SyntaxNodeType.ntRecordConstraint, meta)
                    if child.type == 'CONSTRUCTOR':
                        return self._make_node(SyntaxNodeType.ntConstructorConstraint, meta)
                    if child.type == 'INTERFACE':
                        return self._make_node(SyntaxNodeType.ntInterfaceConstraint, meta)
                    if child.type == 'UNMANAGED':
                        return self._make_node(SyntaxNodeType.ntUnmanagedConstraint, meta)
            return self._make_node(SyntaxNodeType.ntUnknown, meta)

        def type_args(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntTypeArgs, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def field_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            fields: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    fields.extend(child)
            return fields

        def field_decl(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            names: list[str] = []
            type_node = None
            attributes: list[SyntaxNode] = []
            directives: dict[str, str] = {}
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, list):
                    names = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    type_node = child
                elif isinstance(child, dict):
                    directives.update(child)
            fields: list[SyntaxNode] = []
            if type_node is None:
                return fields
            if directives:
                self._apply_directives(type_node, directives)
            for name in names:
                field = self._make_node(SyntaxNodeType.ntField, meta)
                field.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                field.add_child(type_node.clone())
                for attr in attributes:
                    field.add_child(attr)
                fields.append(field)
            return fields

        def proc_type(self, meta: Any, *children: Any) -> SyntaxNode:
            kind = 'procedure'
            params = None
            return_type = None
            for child in children:
                if is_token(child) and child.type == 'FUNCTION':
                    kind = 'function'
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntParameters:
                    params = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    return_type = child
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anType, kind)
            if params is not None:
                node.add_child(params)
            if return_type is not None:
                return_node = self._make_node(SyntaxNodeType.ntReturnType, meta)
                return_node.add_child(return_type)
                node.add_child(return_node)
            return node

        def proc_type_decl_directive(self, meta: Any, *children: Any) -> dict[str, str]:
            for child in children:
                if is_token(child):
                    values = child.value.lstrip(';').strip().casefold().split()
                    return self._proc_type_directive_attrs(values)
            return {}

        def visibility_spec(self, meta: Any, *children: Any) -> SyntaxNode:
            token = None
            for child in children:
                if is_token(child):
                    token = child
            if token is None:
                return self._make_node(SyntaxNodeType.ntPublic, meta)
            mapping = {
                'PRIVATE': SyntaxNodeType.ntPrivate,
                'PROTECTED': SyntaxNodeType.ntProtected,
                'PUBLIC': SyntaxNodeType.ntPublic,
                'PUBLISHED': SyntaxNodeType.ntPublished,
                'AUTOMATED': SyntaxNodeType.ntPublished,
            }
            if token.type == 'PRIVATE' and any(is_token(c) and c.type == 'STRICT' for c in children):
                node = self._make_node(SyntaxNodeType.ntStrictPrivate, meta)
            elif token.type == 'PROTECTED' and any(is_token(c) and c.type == 'STRICT' for c in children):
                node = self._make_node(SyntaxNodeType.ntStrictProtected, meta)
            else:
                node = self._make_node(mapping.get(token.type, SyntaxNodeType.ntPublic), meta)
            node.set_attribute(AttributeName.anVisibility, 'true')
            return node

        def class_member(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            visibility = None
            members: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ in {
                    SyntaxNodeType.ntPrivate,
                    SyntaxNodeType.ntProtected,
                    SyntaxNodeType.ntPublic,
                    SyntaxNodeType.ntPublished,
                    SyntaxNodeType.ntStrictPrivate,
                    SyntaxNodeType.ntStrictProtected,
                }:
                    visibility = child
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            members.append(item)
                elif isinstance(child, SyntaxNode):
                    members.append(child)
            if visibility is None:
                return members
            return [visibility] + members

        def class_body(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            members: list[SyntaxNode] = []
            current_visibility: SyntaxNode | None = None
            for child in children:
                for item in self._flatten(child):
                    if isinstance(item, SyntaxNode) and item.typ in {
                        SyntaxNodeType.ntPrivate,
                        SyntaxNodeType.ntProtected,
                        SyntaxNodeType.ntPublic,
                        SyntaxNodeType.ntPublished,
                        SyntaxNodeType.ntStrictPrivate,
                        SyntaxNodeType.ntStrictProtected,
                    }:
                        current_visibility = item
                        continue
                    if isinstance(item, SyntaxNode):
                        if current_visibility is not None:
                            vis_node = current_visibility.clone()
                            vis_node.add_child(item)
                            members.append(vis_node)
                        else:
                            members.append(item)
            return members

        def record_body(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return self.class_body(meta, *children)

        def interface_body(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in children if isinstance(child, SyntaxNode)]

        def interface_member(self, meta: Any, *children: Any) -> SyntaxNode | None:
            for child in children:
                if isinstance(child, SyntaxNode):
                    return child
            return None

        def property_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            attributes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif self._is_text(child):
                    name = child
                elif is_token(child) and child.type == 'NAME':
                    name = child.value
            if name is None:
                name = 'property'
            node = self._make_node(SyntaxNodeType.ntProperty, meta)
            node.set_attribute(AttributeName.anName, token_value(name))
            for attr in attributes:
                node.add_child(attr)
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def property_directive_block(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            nodes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode):
                    nodes.append(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            nodes.append(item)
            return nodes

        def property_directive(self, meta: Any, *children: Any) -> SyntaxNode:
            token = None
            expr = None
            for child in children:
                if is_token(child):
                    token = child
                elif isinstance(child, SyntaxNode):
                    expr = child
            if token is None:
                return self._make_node(SyntaxNodeType.ntUnknown, meta)
            mapping = {
                'READ': SyntaxNodeType.ntRead,
                'WRITE': SyntaxNodeType.ntWrite,
                'ADD': SyntaxNodeType.ntUnknown,
                'REMOVE': SyntaxNodeType.ntUnknown,
                'DEFAULT': SyntaxNodeType.ntDefault,
                'STORED': SyntaxNodeType.ntUnknown,
                'IMPLEMENTS': SyntaxNodeType.ntImplements,
            }
            node = self._make_node(mapping.get(token.type, SyntaxNodeType.ntUnknown), meta)
            if token.type in {'ADD', 'REMOVE', 'STORED'}:
                node.set_attribute(AttributeName.anKind, token.type.lower())
            if expr is not None:
                node.add_child(expr)
            return node

        def property_specifier(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._make_node(SyntaxNodeType.ntUnknown, meta)

        def routine_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            heading = None
            body = None
            directives = None
            for child in children:
                if isinstance(child, SyntaxNode):
                    if heading is None:
                        heading = child
                    else:
                        body = child
                elif isinstance(child, list):
                    body = child
                elif isinstance(child, dict):
                    directives = child
            if heading is None:
                return self._make_node(SyntaxNodeType.ntMethod, meta)
            if directives:
                self._apply_directives(heading, directives)
            if body is not None:
                if isinstance(body, list):
                    for item in body:
                        if isinstance(item, SyntaxNode):
                            heading.add_child(item)
                elif isinstance(body, SyntaxNode):
                    heading.add_child(body)
            return heading

        def resolution_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            method = None
            source_name = None
            target_name = None
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntMethod:
                    method = child
                elif self._is_text(child) or is_token(child):
                    text = token_value(child)
                    if source_name is None:
                        source_name = text
                    else:
                        target_name = text
            if method is None:
                method = self._make_node(SyntaxNodeType.ntMethod, meta)
                if source_name:
                    method.set_attribute(AttributeName.anName, source_name)
                    method.set_attribute(AttributeName.anKind, 'resolution')
            if target_name:
                resolution = self._make_node(SyntaxNodeType.ntResolutionClause, meta)
                resolution.set_attribute(AttributeName.anName, target_name)
                method.add_child(resolution)
            return method

        def routine_impl(self, meta: Any, *children: Any) -> SyntaxNode:
            heading = None
            body = None
            directives: dict[str, str] = {}
            for child in children:
                if isinstance(child, SyntaxNode):
                    if heading is None:
                        heading = child
                    else:
                        body = child
                elif isinstance(child, list):
                    body = child
                elif isinstance(child, dict):
                    directives.update(child)
                elif isinstance(child, tuple):
                    key, value = child
                    if key:
                        directives[key] = value
                elif is_token(child):
                    if child.type == 'FORWARD':
                        directives['forwarded'] = 'true'
                    elif child.type == 'EXTERNAL':
                        directives['external'] = 'true'
            if heading is None:
                return self._make_node(SyntaxNodeType.ntMethod, meta)
            if directives:
                self._apply_directives(heading, directives)
            if body is not None:
                if isinstance(body, list):
                    for item in body:
                        if isinstance(item, SyntaxNode):
                            heading.add_child(item)
                elif isinstance(body, SyntaxNode):
                    heading.add_child(body)
            return heading

        def procedure_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_routine_from_children(meta, 'procedure', children)

        def function_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_routine_from_children(meta, 'function', children)

        def constructor_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_routine_from_children(meta, 'constructor', children)

        def destructor_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_routine_from_children(meta, 'destructor', children)

        def operator_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._build_routine_from_children(meta, 'operator', children)

        def routine_body(self, meta: Any, *children: Any) -> SyntaxNode | None:
            for child in children:
                if isinstance(child, SyntaxNode):
                    return child
                if isinstance(child, list):
                    return child
                if is_token(child) and child.type == 'FORWARD':
                    node = self._make_node(SyntaxNodeType.ntUnknown, meta)
                    node.set_attribute(AttributeName.anForwarded, 'true')
                    return node
            return None

        def block(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            nodes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            nodes.append(item)
                elif isinstance(child, SyntaxNode):
                        nodes.append(child)
            return nodes

        def asm_block(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return self.block(meta, *children)

        def directive_list(self, meta: Any, *children: Any) -> dict[str, str]:
            attrs: dict[str, str] = {}
            for child in children:
                if isinstance(child, tuple):
                    key, value = child
                    attrs[key] = value
                elif isinstance(child, dict):
                    attrs.update(child)
            return attrs

        def body_directive_list(self, meta: Any, *children: Any) -> dict[str, str]:
            return self.directive_list(meta, *children)

        def body_directive(self, meta: Any, *children: Any) -> tuple[str, str]:
            return self.directive(meta, *children)

        def routine_heading_directive_list(self, meta: Any, *children: Any) -> dict[str, str]:
            attrs: dict[str, str] = {}
            for child in children:
                if isinstance(child, tuple):
                    key, value = child
                    if key:
                        attrs[key] = value
            return attrs

        def routine_heading_directive(self, meta: Any, *children: Any) -> tuple[str, str]:
            return self.directive(meta, *children)

        def directive(self, meta: Any, *children: Any) -> tuple[str, str]:
            token = None
            expr = None
            directive_tokens = {
                'OVERLOAD',
                'OVERRIDE',
                'VIRTUAL',
                'DYNAMIC',
                'ABSTRACT',
                'INLINE',
                'REINTRODUCE',
                'STATIC',
                'FINAL',
                'SEALED',
                'STDCALL',
                'CDECL',
                'PASCAL',
                'REGISTER',
                'SAFECALL',
                'WINAPI',
                'MESSAGE',
                'EXTERNAL',
                'FORWARD',
                'DEPRECATED',
                'PLATFORM',
                'EXPERIMENTAL',
                'NORETURN',
                'VARARGS',
                'LOCAL',
                'LIBRARY',
                'DELAYED',
                'EXPORT',
                'FAR',
                'NEAR',
                'ASSEMBLER',
                'UNSAFE',
                'DISPID',
            }
            for child in children:
                if is_token(child) and token is None and child.type in directive_tokens:
                    token = child
                elif isinstance(child, SyntaxNode):
                    expr = child
            if token is None:
                return ('', '')
            name = token.type.lower()
            if name in {'override', 'virtual', 'dynamic'}:
                return ('methodbinding', name)
            if name in {'reintroduce', 'overload', 'abstract', 'inline'}:
                return (name, 'true')
            if name in {'stdcall', 'cdecl', 'pascal', 'register', 'safecall'}:
                return ('callingconvention', name)
            if name == 'winapi':
                return ('callingconvention', 'winapi')
            if name == 'message' and expr is not None:
                return ('message', self._expr_to_text(expr))
            if name == 'external':
                return ('external', 'true')
            if name == 'forward':
                return ('forwarded', 'true')
            if name == 'deprecated':
                return ('deprecated', self._expr_to_text(expr) if expr is not None else 'true')
            if name in {
                'platform',
                'experimental',
                'varargs',
                'local',
                'library',
                'delayed',
                'static',
                'final',
                'sealed',
                'export',
                'far',
                'near',
                'assembler',
                'unsafe',
                'noreturn',
            }:
                return (name, 'true')
            if name == 'dispid' and expr is not None:
                return ('dispid', self._expr_to_text(expr))
            return ('', '')

        def formal_parameters(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntParameters, meta)
            for child in children:
                for param in self._flatten(child):
                    if isinstance(param, SyntaxNode):
                        node.add_child(param)
            return node

        def param_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            params: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    params.extend(child)
            return params

        def param(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            modifier = None
            names: list[str] = []
            type_node = None
            default_expr = None
            attributes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif self._is_text(child):
                    names.append(child)
                elif isinstance(child, list):
                    names = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    type_node = child
                elif is_token(child):
                    modifier = child
                elif isinstance(child, SyntaxNode):
                    default_expr = child
            params: list[SyntaxNode] = []
            for name in names:
                node = self._make_node(SyntaxNodeType.ntParameter, meta)
                if modifier is not None:
                    node.set_attribute(AttributeName.anKind, modifier.value.lower())
                node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                if type_node is not None:
                    node.add_child(type_node.clone())
                for attr in attributes:
                    node.add_child(attr)
                if default_expr is not None:
                    value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                    value_node.add_child(default_expr)
                    node.add_child(value_node)
                params.append(node)
            return params

        def param_modifier(self, meta: Any, token: Any) -> Any:
            return token

        def name_list(self, meta: Any, *children: Any) -> list[str]:
            return [token_value(child) for child in children]

        def qualified_name(self, meta: Any, *children: Any) -> str:
            return '.'.join(token_value(child) for child in children)

        def qualified_name_part(self, meta: Any, token: Any) -> str:
            return token_value(token)

        def expr_identifier(self, meta: Any, token: Any) -> SyntaxNode:
            name = token_value(token)
            return self._make_expr_identifier(meta, name)

        def expr_qualified_name(self, meta: Any, *children: Any) -> SyntaxNode:
            nodes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            nodes.append(item)
                        elif self._is_text(item) or is_token(item):
                            nodes.append(self._make_expr_identifier(meta, token_value(item)))
                elif isinstance(child, SyntaxNode):
                    nodes.append(child)
                elif self._is_text(child) or is_token(child):
                    nodes.append(self._make_expr_identifier(meta, token_value(child)))
            if not nodes:
                return self._make_node(SyntaxNodeType.ntExpression, meta)
            node = nodes[0]
            for part in nodes[1:]:
                dot = self._make_node(SyntaxNodeType.ntDot, meta)
                dot.add_child(node)
                dot.add_child(part)
                node = dot
            return node

        def literal(self, meta: Any, token: Any) -> SyntaxNode:
            if isinstance(token, SyntaxNode):
                return token
            value = token_value(token)
            node = self._make_valued(SyntaxNodeType.ntLiteral, value, meta)
            if is_token(token):
                if token.type in {'STRING_LITERAL', 'STRING_BLOCK3', 'STRING_BLOCK5'}:
                    node.value = self._dequote_string(value)
                    node.set_attribute(AttributeName.anType, 'string')
                elif token.type == 'POINTER_CHAR':
                    node.set_attribute(AttributeName.anType, 'char')
                else:
                    node.set_attribute(AttributeName.anType, 'numeric')
            return node

        def string_literal_sequence(self, meta: Any, *children: Any) -> SyntaxNode:
            parts: list[str] = []
            for child in children:
                if not is_token(child):
                    continue
                value = token_value(child)
                if child.type in {'STRING_LITERAL', 'STRING_BLOCK3', 'STRING_BLOCK5'}:
                    parts.append(self._dequote_string(value))
                else:
                    parts.append(value)
            node = self._make_valued(SyntaxNodeType.ntLiteral, ''.join(parts), meta)
            node.set_attribute(AttributeName.anType, 'string')
            return node

        def string_literal_part(self, meta: Any, token: Any) -> Any:
            return token

        def numeric_literal(self, meta: Any, token: Any) -> SyntaxNode:
            value = token_value(token)
            node = self._make_valued(SyntaxNodeType.ntLiteral, value, meta)
            node.set_attribute(AttributeName.anType, 'numeric')
            return node

        def anonymous_method(self, meta: Any, *children: Any) -> SyntaxNode:
            kind = None
            params = None
            return_type = None
            body = None
            for child in children:
                if is_token(child) and child.type in {'PROCEDURE', 'FUNCTION'}:
                    kind = child.type.lower()
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntParameters:
                    params = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    return_type = child
                elif isinstance(child, list):
                    body = child
                elif isinstance(child, SyntaxNode):
                    body = child
            node = self._make_node(SyntaxNodeType.ntAnonymousMethod, meta)
            if kind:
                node.set_attribute(AttributeName.anKind, kind)
            if params is not None:
                node.add_child(params)
            if return_type is not None:
                return_node = self._make_node(SyntaxNodeType.ntReturnType, meta)
                return_node.add_child(return_type)
                node.add_child(return_node)
            if body is not None:
                if isinstance(body, list):
                    for item in body:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(body, SyntaxNode):
                    node.add_child(body)
            return node

        def primary(self, meta: Any, child: Any) -> SyntaxNode:
            if isinstance(child, SyntaxNode):
                return child
            if is_token(child) and child.type == 'NIL':
                node = self._make_valued(SyntaxNodeType.ntLiteral, 'nil', meta)
                node.set_attribute(AttributeName.anType, 'nil')
                return node
            if is_token(child) and child.type in {'TRUE', 'FALSE'}:
                node = self._make_valued(SyntaxNodeType.ntLiteral, child.value.lower(), meta)
                node.set_attribute(AttributeName.anType, 'boolean')
                return node
            if is_token(child) and child.type == 'SELF':
                node = self._make_node(SyntaxNodeType.ntIdentifier, meta)
                node.set_attribute(AttributeName.anName, 'self')
                return node
            name = token_value(child)
            node = self._make_node(SyntaxNodeType.ntIdentifier, meta)
            node.set_attribute(AttributeName.anName, name)
            return node

        def expr_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in self._flatten(children) if isinstance(child, SyntaxNode)]

        def if_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntIf, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def or_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(meta, children, {'OR': SyntaxNodeType.ntOr})

        def xor_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(meta, children, {'XOR': SyntaxNodeType.ntXor})

        def and_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(meta, children, {'AND': SyntaxNodeType.ntAnd})

        def rel_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(
                meta,
                children,
                {
                    '=': SyntaxNodeType.ntEqual,
                    '<>': SyntaxNodeType.ntNotEqual,
                    '<': SyntaxNodeType.ntLower,
                    '<=': SyntaxNodeType.ntLowerEqual,
                    '>': SyntaxNodeType.ntGreater,
                    '>=': SyntaxNodeType.ntGreaterEqual,
                    'IN': SyntaxNodeType.ntIn,
                    'IS': SyntaxNodeType.ntIs,
                    'NOT IN': SyntaxNodeType.ntNotIn,
                    'IS NOT': SyntaxNodeType.ntIsNot,
                    'AS': SyntaxNodeType.ntAs,
                },
            )

        def rel_op(self, meta: Any, *children: Any) -> Any:
            if children:
                return children[0]
            return ''

        def not_in_op(self, meta: Any, *_: Any) -> str:
            return 'NOT IN'

        def is_not_op(self, meta: Any, *_: Any) -> str:
            return 'IS NOT'

        def add_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(
                meta,
                children,
                {'+': SyntaxNodeType.ntAdd, '-': SyntaxNodeType.ntSub},
            )

        def mul_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self._binary_chain(
                meta,
                children,
                {
                    '*': SyntaxNodeType.ntMul,
                    '/': SyntaxNodeType.ntFDiv,
                    'DIV': SyntaxNodeType.ntDiv,
                    'MOD': SyntaxNodeType.ntMod,
                    'SHL': SyntaxNodeType.ntShl,
                    'SHR': SyntaxNodeType.ntShr,
                },
            )

        def unary_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            if len(children) == 2 and is_token(children[0]):
                token = children[0]
                expr = children[1]
                if token.type == 'NOT':
                    node = self._make_node(SyntaxNodeType.ntNot, meta)
                    node.add_child(expr)
                    return node
                if token.value == '@':
                    node = self._make_node(SyntaxNodeType.ntAddr, meta)
                    node.add_child(expr)
                    return node
                if token.value == '^':
                    node = self._make_node(SyntaxNodeType.ntDeref, meta)
                    node.add_child(expr)
                    return node
                if token.value == '-':
                    node = self._make_node(SyntaxNodeType.ntUnaryMinus, meta)
                    node.add_child(expr)
                    return node
                return expr
            if children:
                return children[0]
            return self._make_node(SyntaxNodeType.ntExpression, meta)

        def postfix_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            if not children:
                return self._make_node(SyntaxNodeType.ntExpression, meta)
            node = children[0]
            if not isinstance(node, SyntaxNode):
                node = self._ensure_expr_node(meta, node)
            for suffix in children[1:]:
                if isinstance(suffix, tuple) and suffix[0] == 'call':
                    call_node = self._make_node(SyntaxNodeType.ntCall, meta)
                    call_node.add_child(node)
                    args = self._make_node(SyntaxNodeType.ntArguments, meta)
                    call_args = suffix[1] if isinstance(suffix[1], list) else [suffix[1]]
                    for arg in call_args:
                        if isinstance(arg, SyntaxNode):
                            args.add_child(arg)
                    call_node.add_child(args)
                    node = call_node
                elif isinstance(suffix, tuple) and suffix[0] == 'index':
                    idx_node = self._make_node(SyntaxNodeType.ntIndexed, meta)
                    idx_node.add_child(node)
                    exprs = self._make_node(SyntaxNodeType.ntExpressions, meta)
                    index_exprs = suffix[1] if isinstance(suffix[1], list) else [suffix[1]]
                    for expr in index_exprs:
                        if isinstance(expr, SyntaxNode):
                            exprs.add_child(expr)
                    idx_node.add_child(exprs)
                    node = idx_node
                elif isinstance(suffix, tuple) and suffix[0] == 'field':
                    dot_node = self._make_node(SyntaxNodeType.ntDot, meta)
                    dot_node.add_child(node)
                    name_node = self._make_expr_identifier(meta, suffix[1])
                    dot_node.add_child(name_node)
                    node = dot_node
                elif isinstance(suffix, tuple) and suffix[0] == 'deref':
                    deref_node = self._make_node(SyntaxNodeType.ntDeref, meta)
                    deref_node.add_child(node)
                    node = deref_node
            return node

        def call_suffix(self, meta: Any, *children: Any) -> tuple[str, list[SyntaxNode]]:
            args: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    args.extend(child)
            return ('call', args)

        def range_postfix(self, meta: Any, *children: Any) -> SyntaxNode:
            return self.postfix_expr(meta, *children)

        def range_call_suffix(self, meta: Any, *children: Any) -> tuple[str, list[SyntaxNode]]:
            args: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    args.extend(child)
            return ('call', args)

        def range_arg_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in children if isinstance(child, SyntaxNode)]

        def range_primary(self, meta: Any, child: Any) -> SyntaxNode:
            if isinstance(child, SyntaxNode):
                return child
            if self._is_text(child):
                parts = [self._make_expr_identifier(meta, part) for part in child.split('.')]
                return self.expr_qualified_name(meta, *parts)
            return self._ensure_expr_node(meta, child)

        def op_add(self, meta: Any) -> str:
            return '+'

        def op_sub(self, meta: Any) -> str:
            return '-'

        def op_mul(self, meta: Any) -> str:
            return '*'

        def op_fdiv(self, meta: Any) -> str:
            return '/'

        def op_div(self, meta: Any, *_: Any) -> str:
            return 'DIV'

        def op_mod(self, meta: Any, *_: Any) -> str:
            return 'MOD'

        def op_shl(self, meta: Any, *_: Any) -> str:
            return 'SHL'

        def op_shr(self, meta: Any, *_: Any) -> str:
            return 'SHR'

        def index_suffix(self, meta: Any, exprs: list[SyntaxNode]) -> tuple[str, list[SyntaxNode]]:
            return ('index', exprs)

        def field_suffix(self, meta: Any, name: Any) -> tuple[str, str]:
            return ('field', token_value(name))

        def deref_suffix(self, meta: Any, *_: Any) -> tuple[str, None]:
            return ('deref', None)

        def variable_ref(self, meta: Any, name: str) -> SyntaxNode:
            return self._make_expr_identifier(meta, name)

        def assignment(self, meta: Any, *children: Any) -> SyntaxNode:
            nodes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode):
                    nodes.append(child)
                    continue
                if is_token(child) and child.type == 'ASSIGN':
                    continue
                nodes.append(self._ensure_expr_node(meta, child))
            left = nodes[0] if len(nodes) > 0 else self._make_node(SyntaxNodeType.ntExpression, meta)
            right = nodes[1] if len(nodes) > 1 else self._make_node(SyntaxNodeType.ntExpression, meta)
            node = self._make_node(SyntaxNodeType.ntAssign, meta)
            lhs = self._make_node(SyntaxNodeType.ntLHS, meta)
            rhs = self._make_node(SyntaxNodeType.ntRHS, meta)
            lhs.add_child(left)
            rhs.add_child(right)
            node.add_child(lhs)
            node.add_child(rhs)
            return node

        def address_assignment(self, meta: Any, *children: Any) -> SyntaxNode:
            nodes: list[SyntaxNode] = []
            for child in children:
                if is_token(child) and child.type == 'ASSIGN':
                    continue
                if isinstance(child, SyntaxNode):
                    nodes.append(child)
                elif is_token(child) and child.type in {'NIL', 'TRUE', 'FALSE', 'SELF'}:
                    nodes.append(self.primary(meta, child))
                elif child is not None:
                    nodes.append(self._ensure_expr_node(meta, child))
            target = nodes[0] if nodes else self._make_node(SyntaxNodeType.ntExpression, meta)
            value = nodes[1] if len(nodes) > 1 else self._make_node(SyntaxNodeType.ntExpression, meta)
            addr = self._make_node(SyntaxNodeType.ntAddr, meta)
            addr.add_child(target)
            return self.assignment(meta, addr, value)

        def call_expr(self, meta: Any, name: str, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCall, meta)
            target = self._make_node(SyntaxNodeType.ntIdentifier, meta)
            target.set_attribute(AttributeName.anName, name)
            node.add_child(target)
            args = self._make_node(SyntaxNodeType.ntArguments, meta)
            for child in children:
                if isinstance(child, list):
                    for arg in child:
                        args.add_child(arg)
            node.add_child(args)
            return node

        def call_statement(self, meta: Any, node: SyntaxNode) -> SyntaxNode:
            return node

        def inline_number(self, meta: Any, token: Any) -> SyntaxNode:
            return self.literal(meta, token)

        def inline_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntUnknown, meta)
            node.set_attribute(AttributeName.anKind, 'inline')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def inline_const_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntConstants, meta)
            node.set_attribute(AttributeName.anKind, 'inline')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def inline_const_decl(self, meta: Any, *children: Any) -> SyntaxNode:
            return self.const_decl(meta, *children)

        def inline_var_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntVariables, meta)
            node.set_attribute(AttributeName.anKind, 'inline')
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def inline_var_decl(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            names: list[str] = []
            type_node = None
            value_expr = None
            attributes: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, list):
                    names = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    type_node = child
                elif isinstance(child, SyntaxNode):
                    value_expr = child
            variables: list[SyntaxNode] = []
            for name in names:
                node = self._make_node(SyntaxNodeType.ntVariable, meta)
                node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                if type_node is not None:
                    node.add_child(type_node.clone())
                for attr in attributes:
                    node.add_child(attr)
                if value_expr is not None:
                    value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                    value_node.add_child(value_expr)
                    node.add_child(value_node)
                variables.append(node)
            return variables

        def argument(self, meta: Any, *children: Any) -> SyntaxNode:
            if children and (self._is_text(children[0]) or is_token(children[0])):
                name = token_value(children[0])
                expr = next((child for child in children[1:] if isinstance(child, SyntaxNode)), None)
                if expr is not None:
                    node = self._make_node(SyntaxNodeType.ntNamedArgument, meta)
                    node.set_attribute(AttributeName.anName, name)
                    node.add_child(expr)
                    return node
            node = self._make_node(SyntaxNodeType.ntPositionalArgument, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def formatted_argument(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExpression, meta)
            node.set_attribute(AttributeName.anKind, 'format')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def named_formatted_argument(self, meta: Any, name: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExpression, meta)
            node.set_attribute(AttributeName.anKind, 'format')
            node.add_child(self._make_expr_identifier(meta, token_value(name)))
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def statement_list(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntStatements, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def compound_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntStatements, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
            return node

        def if_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntIf, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def while_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntWhile, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def for_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntFor, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def for_init(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            expr = None
            type_node = None
            is_var = any(is_token(child) and child.type == 'VAR' for child in children)
            for child in children:
                if self._is_text(child) or is_token(child):
                    if is_token(child) and child.type == 'VAR':
                        continue
                    name = token_value(child)
                elif isinstance(child, SyntaxNode):
                    if child.typ == SyntaxNodeType.ntType and type_node is None:
                        type_node = child
                    else:
                        expr = child
            if is_var:
                node = self._make_node(SyntaxNodeType.ntVariable, meta)
                node.set_attribute(AttributeName.anKind, 'inline')
                if name is not None:
                    node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                if type_node is not None:
                    node.add_child(type_node)
                if expr is not None:
                    value_node = self._make_node(SyntaxNodeType.ntValue, meta)
                    value_node.add_child(expr)
                    node.add_child(value_node)
                return node
            assign = self._make_node(SyntaxNodeType.ntAssign, meta)
            lhs = self._make_node(SyntaxNodeType.ntLHS, meta)
            rhs = self._make_node(SyntaxNodeType.ntRHS, meta)
            if name is not None:
                ident = self._make_node(SyntaxNodeType.ntIdentifier, meta)
                ident.set_attribute(AttributeName.anName, name)
                lhs.add_child(ident)
            if expr is not None:
                rhs.add_child(expr)
            assign.add_child(lhs)
            assign.add_child(rhs)
            return assign

        def for_in(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            expr = None
            type_node = None
            is_var = any(is_token(child) and child.type == 'VAR' for child in children)
            for child in children:
                if self._is_text(child) or is_token(child):
                    if is_token(child) and child.type == 'VAR':
                        continue
                    name = token_value(child)
                elif isinstance(child, SyntaxNode):
                    if child.typ == SyntaxNodeType.ntType and type_node is None:
                        type_node = child
                    else:
                        expr = child
            node = self._make_node(SyntaxNodeType.ntIn, meta)
            if is_var:
                var_node = self._make_node(SyntaxNodeType.ntVariable, meta)
                var_node.set_attribute(AttributeName.anKind, 'inline')
                if name is not None:
                    var_node.add_child(self._make_valued(SyntaxNodeType.ntName, name, meta))
                if type_node is not None:
                    var_node.add_child(type_node)
                node.add_child(var_node)
            elif name is not None:
                ident = self._make_node(SyntaxNodeType.ntIdentifier, meta)
                ident.set_attribute(AttributeName.anName, name)
                node.add_child(ident)
            if expr is not None:
                node.add_child(expr)
            return node

        def repeat_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntRepeat, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def case_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCase, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def case_selector_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in self._flatten(children) if isinstance(child, SyntaxNode)]

        def case_selector(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCaseSelector, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def case_label_list(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCaseLabels, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def case_label(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCaseLabel, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def case_else(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntCaseElse, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def with_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntWith, meta)
            exprs: list[SyntaxNode] = []
            statements: list[SyntaxNode] = []
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            exprs.append(item)
                elif isinstance(child, SyntaxNode):
                    statements.append(child)
            if statements and not exprs:
                exprs = statements[:-1]
                statements = statements[-1:]
            for stmt in statements:
                node.add_child(stmt)
            if exprs:
                exprs_node = self._make_node(SyntaxNodeType.ntExpressions, meta)
                for expr in exprs:
                    exprs_node.add_child(expr)
                node.add_child(exprs_node)
            return node

        def try_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntTry, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def except_block(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExcept, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def exception_handler(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExceptionHandler, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def finally_block(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntFinally, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def raise_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntRaise, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def goto_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntGoto, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif self._is_text(child):
                    node.set_attribute(AttributeName.anName, child)
            return node

        def label_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntStatement, meta)
            node.set_attribute(AttributeName.anKind, 'label')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif self._is_text(child):
                    name_node = self._make_node(SyntaxNodeType.ntLabel, meta)
                    name_node.set_attribute(AttributeName.anName, child)
                    node.add_child(name_node)
            return node

        def inherited_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntInherited, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def inherited_expr(self, meta: Any, *children: Any) -> SyntaxNode:
            return self.inherited_statement(meta, *children)

        def break_statement(self, meta: Any, *_: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntStatement, meta)
            node.set_attribute(AttributeName.anKind, 'break')
            return node

        def continue_statement(self, meta: Any, *_: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntStatement, meta)
            node.set_attribute(AttributeName.anKind, 'continue')
            return node

        def exit_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntStatement, meta)
            node.set_attribute(AttributeName.anKind, 'exit')
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def asm_statement(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntStatements, meta)
            node.set_attribute(AttributeName.anType, 'asm')
            return node

        def empty_statement(self, meta: Any) -> SyntaxNode:
            return self._make_node(SyntaxNodeType.ntEmptyStatement, meta)

        def requires_clause(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntRequires, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif self._is_text(child):
                    pkg = self._make_node(SyntaxNodeType.ntPackage, meta)
                    pkg.set_attribute(AttributeName.anName, child)
                    node.add_child(pkg)
            return node

        def contains_clause(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntContains, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def contains_item(self, meta: Any, *children: Any) -> SyntaxNode:
            return self.uses_item(meta, *children)

        def exports_section(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExports, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def exports_item(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntElement, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
                elif self._is_text(child):
                    name_node = self._make_node(SyntaxNodeType.ntName, meta)
                    name_node.set_attribute(AttributeName.anName, child)
                    node.add_child(name_node)
            return node

        def exports_specifier(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntUnknown, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def attribute_sections(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntAttributes, meta)
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def attribute_section(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in children if isinstance(child, SyntaxNode)]

        def attribute_name(self, meta: Any, *children: Any) -> str:
            for child in children:
                if self._is_text(child) or is_token(child):
                    return token_value(child)
            return ''

        def attribute_keyword(self, meta: Any, token: Any) -> str:
            return token_value(token)

        def attribute(self, meta: Any, *children: Any) -> SyntaxNode:
            name = None
            args = None
            for child in children:
                if self._is_text(child) or is_token(child):
                    name = token_value(child)
                elif isinstance(child, SyntaxNode):
                    args = child
            node = self._make_node(SyntaxNodeType.ntAttribute, meta)
            if name is not None:
                node.set_attribute(AttributeName.anName, name)
            if args is not None:
                node.add_child(args)
            return node

        def attribute_arguments(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntArguments, meta)
            for child in children:
                if isinstance(child, list):
                    for arg in child:
                        if isinstance(arg, SyntaxNode):
                            node.add_child(arg)
            return node

        def set_const(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntSet, meta)
            for child in children:
                if isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def set_element_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in self._flatten(children) if isinstance(child, SyntaxNode)]

        def set_element(self, meta: Any, *children: Any) -> SyntaxNode:
            nodes = [child for child in children if isinstance(child, SyntaxNode)]
            if len(nodes) == 2:
                subrange = self._make_node(SyntaxNodeType.ntSubrange, meta)
                subrange.add_child(nodes[0])
                subrange.add_child(nodes[1])
                return subrange
            if nodes:
                return nodes[0]
            return self._make_node(SyntaxNodeType.ntExpression, meta)

        def array_const(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntExpressions, meta)
            for child in children:
                for item in self._flatten(child):
                    if isinstance(item, SyntaxNode):
                        node.add_child(item)
            return node

        def const_value_list(self, meta: Any, *children: Any) -> list[SyntaxNode]:
            return [child for child in children if isinstance(child, SyntaxNode)]

        def record_const(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntUnknown, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def record_const_item(self, meta: Any, *children: Any) -> SyntaxNode:
            node = self._make_node(SyntaxNodeType.ntValue, meta)
            for child in children:
                if isinstance(child, SyntaxNode):
                    node.add_child(child)
            return node

        def _build_routine(
            self,
            meta: Any,
            name: str,
            kind: str,
            return_type: SyntaxNode | None,
            children: Iterable[Any],
        ) -> SyntaxNode:
            node = self._make_compound(SyntaxNodeType.ntMethod, meta)
            node.set_attribute(AttributeName.anName, name)
            node.set_attribute(AttributeName.anKind, kind)

            parameters = None
            directives = None
            body: Any = None
            for child in children:
                if isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntParameters:
                    parameters = child
                elif isinstance(child, dict):
                    directives = child
                elif isinstance(child, SyntaxNode):
                    body = child
                elif isinstance(child, list):
                    body = child

            if parameters is not None:
                node.add_child(parameters)
            if return_type is not None:
                return_node = self._make_node(SyntaxNodeType.ntReturnType, meta)
                return_node.add_child(return_type)
                node.add_child(return_node)
            if directives:
                self._apply_directives(node, directives)
            if body is not None:
                if isinstance(body, list):
                    for item in body:
                        if isinstance(item, SyntaxNode):
                            node.add_child(item)
                elif isinstance(body, SyntaxNode):
                    node.add_child(body)
            return node

        def _apply_directives(self, node: SyntaxNode, directives: dict[str, str]) -> None:
            for key, value in directives.items():
                attr = {
                    'methodbinding': AttributeName.anMethodBinding,
                    'reintroduce': AttributeName.anReintroduce,
                    'overload': AttributeName.anOverload,
                    'abstract': AttributeName.anAbstract,
                    'inline': AttributeName.anInline,
                    'callingconvention': AttributeName.anCallingConvention,
                    'forwarded': AttributeName.anForwarded,
                    'external': AttributeName.anExternal,
                    'deprecated': AttributeName.anDeprecated,
                    'static': AttributeName.anStatic,
                    'final': AttributeName.anFinal,
                    'sealed': AttributeName.anSealed,
                    'assembler': AttributeName.anAssembler,
                    'unsafe': AttributeName.anUnsafe,
                    'export': AttributeName.anExport,
                    'far': AttributeName.anFar,
                    'near': AttributeName.anNear,
                    'noreturn': AttributeName.anNoReturn,
                    'varargs': AttributeName.anVarArgs,
                }.get(key)
                if attr is not None:
                    node.set_attribute(attr, value)

        def _apply_decl_directives(self, node: SyntaxNode, directives: dict[str, str]) -> None:
            for key, value in directives.items():
                attr = {
                    'deprecated': AttributeName.anDeprecated,
                    'platform': AttributeName.anKind,
                    'experimental': AttributeName.anKind,
                    'library': AttributeName.anKind,
                }.get(key)
                if attr is None:
                    continue
                if attr == AttributeName.anKind and value == 'true':
                    node.set_attribute(attr, key)
                else:
                    node.set_attribute(attr, value)

        def _build_routine_from_children(
            self,
            meta: Any,
            kind: str,
            children: Iterable[Any],
        ) -> SyntaxNode:
            name = None
            params = None
            return_type = None
            type_params = None
            directives = None
            body = None
            attributes: list[SyntaxNode] = []
            is_class = False
            forwarded = False
            for child in children:
                if is_token(child) and child.type == 'CLASS':
                    is_class = True
                elif is_token(child) and child.type == 'FORWARD':
                    forwarded = True
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntAttributes:
                    attributes.append(child)
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntTypeParams:
                    type_params = child
                elif self._is_text(child):
                    name = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntParameters:
                    params = child
                elif isinstance(child, SyntaxNode) and child.typ == SyntaxNodeType.ntType:
                    return_type = child
                elif isinstance(child, dict):
                    directives = child
                elif isinstance(child, SyntaxNode):
                    body = child
                elif isinstance(child, list):
                    body = child
            if name is None:
                name = kind
            if isinstance(name, str) and '<' in name:
                base_name, generic_args = self._split_generic_name(name)
                name = base_name
                if type_params is None and generic_args:
                    type_params = self._type_params_from_generic_name(meta, generic_args)
            routine = self._build_routine(meta, name, kind, return_type, [params, directives, body])
            if is_class:
                routine.set_attribute(AttributeName.anClass, 'true')
            if forwarded:
                routine.set_attribute(AttributeName.anForwarded, 'true')
            for attr in attributes:
                routine.add_child(attr)
            if type_params is not None:
                routine.add_child(type_params)
            return routine

        def _expr_to_text(self, node: SyntaxNode) -> str:
            if isinstance(node, ValuedSyntaxNode):
                return node.value
            name = node.get_attribute(AttributeName.anName)
            if name:
                return name
            return ''

        def _split_generic_name(self, name: str) -> tuple[str, str]:
            if '<' in name:
                base, rest = name.split('<', 1)
                return base, f'<{rest}'
            return name, ''

        def _type_params_from_generic_name(self, meta: Any, generic_args: str) -> SyntaxNode | None:
            if not generic_args.startswith('<') or not generic_args.endswith('>'):
                return None
            inner = generic_args[1:-1].strip()
            if not inner:
                return None
            parsed: list[dict[str, Any]] = []
            current_group_indices: list[int] = []
            for segment in self._split_top_level(inner, ';'):
                if not segment:
                    continue
                left, right = self._split_first_top_level(segment, ':')
                if right:
                    names = [part for part in self._split_top_level(left, ',') if part]
                    constraints = [part for part in self._split_top_level(right, ',') if part]
                    current_group_indices = []
                    for name in names:
                        entry = {'name': name, 'constraints': list(constraints)}
                        parsed.append(entry)
                        current_group_indices.append(len(parsed) - 1)
                    continue

                parts = [part for part in self._split_top_level(segment, ',') if part]
                if not parts:
                    continue
                if parsed and all(self._looks_like_constraint_fragment(part) for part in parts):
                    targets = current_group_indices if current_group_indices else [len(parsed) - 1]
                    for index in targets:
                        parsed[index]['constraints'].extend(parts)
                    continue

                current_group_indices = []
                for part in parts:
                    parsed.append({'name': part, 'constraints': []})
                    current_group_indices.append(len(parsed) - 1)

            if not parsed:
                return None
            params = self._make_node(SyntaxNodeType.ntTypeParams, meta)
            for item in parsed:
                raw_name = item['name']
                type_param = self._make_node(SyntaxNodeType.ntTypeParam, meta)
                type_param.add_child(self._make_valued(SyntaxNodeType.ntName, raw_name, meta))
                constraints = item.get('constraints') or []
                if constraints:
                    constraints_node = self._make_node(SyntaxNodeType.ntConstraints, meta)
                    for constraint_text in constraints:
                        constraints_node.add_child(self._constraint_node_from_text(meta, constraint_text))
                    type_param.add_child(constraints_node)
                params.add_child(type_param)
            return params

        def _split_first_top_level(self, text: str, delimiter: str) -> tuple[str, str]:
            angle = 0
            square = 0
            round_ = 0
            in_string = False
            i = 0
            while i < len(text):
                ch = text[i]
                if ch == "'":
                    if in_string and i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    in_string = not in_string
                    i += 1
                    continue
                if in_string:
                    i += 1
                    continue
                if ch == '<':
                    angle += 1
                elif ch == '>':
                    angle = max(0, angle - 1)
                elif ch == '[':
                    square += 1
                elif ch == ']':
                    square = max(0, square - 1)
                elif ch == '(':
                    round_ += 1
                elif ch == ')':
                    round_ = max(0, round_ - 1)
                elif ch == delimiter and angle == 0 and square == 0 and round_ == 0:
                    return (text[:i].strip(), text[i + 1 :].strip())
                i += 1
            return (text.strip(), '')

        def _split_top_level(self, text: str, delimiter: str) -> list[str]:
            parts: list[str] = []
            current: list[str] = []
            angle = 0
            square = 0
            round_ = 0
            in_string = False
            i = 0
            while i < len(text):
                ch = text[i]
                if ch == "'":
                    current.append(ch)
                    if in_string and i + 1 < len(text) and text[i + 1] == "'":
                        current.append(text[i + 1])
                        i += 2
                        continue
                    in_string = not in_string
                    i += 1
                    continue
                if not in_string:
                    if ch == '<':
                        angle += 1
                    elif ch == '>':
                        angle = max(0, angle - 1)
                    elif ch == '[':
                        square += 1
                    elif ch == ']':
                        square = max(0, square - 1)
                    elif ch == '(':
                        round_ += 1
                    elif ch == ')':
                        round_ = max(0, round_ - 1)
                    elif ch == delimiter and angle == 0 and square == 0 and round_ == 0:
                        part = ''.join(current).strip()
                        if part:
                            parts.append(part)
                        current = []
                        i += 1
                        continue
                current.append(ch)
                i += 1
            part = ''.join(current).strip()
            if part:
                parts.append(part)
            return parts

        def _looks_like_constraint_fragment(self, fragment: str) -> bool:
            token = fragment.strip().casefold()
            return token in {'class', 'record', 'constructor', 'interface', 'unmanaged'}

        def _constraint_node_from_text(self, meta: Any, constraint_text: str) -> SyntaxNode:
            text = constraint_text.strip()
            lowered = text.casefold()
            if lowered == 'class':
                return self._make_node(SyntaxNodeType.ntClassConstraint, meta)
            if lowered == 'record':
                return self._make_node(SyntaxNodeType.ntRecordConstraint, meta)
            if lowered == 'constructor':
                return self._make_node(SyntaxNodeType.ntConstructorConstraint, meta)
            if lowered == 'interface':
                return self._make_node(SyntaxNodeType.ntInterfaceConstraint, meta)
            if lowered == 'unmanaged':
                return self._make_node(SyntaxNodeType.ntUnmanagedConstraint, meta)
            node = self._make_node(SyntaxNodeType.ntType, meta)
            node.set_attribute(AttributeName.anName, text)
            return node

        def _dequote_string(self, value: str) -> str:
            if len(value) < 2:
                return value
            if value[0] != "'":
                return value
            if value[-1] == "'":
                inner = value[1:-1]
            else:
                inner = value[1:]
            return inner.replace("''", "'")

        def _make_node(self, typ: SyntaxNodeType, meta: Any) -> SyntaxNode:
            node = SyntaxNode(typ)
            self._set_pos(node, meta)
            return node

        def _make_compound(self, typ: SyntaxNodeType, meta: Any) -> CompoundSyntaxNode:
            node = CompoundSyntaxNode(typ)
            self._set_pos(node, meta)
            return node

        def _make_valued(self, typ: SyntaxNodeType, value: str, meta: Any) -> ValuedSyntaxNode:
            node = ValuedSyntaxNode(typ)
            node.value = value
            self._set_pos(node, meta)
            return node

        def _set_pos(self, node: SyntaxNode, meta: Any) -> None:
            if meta is None:
                return
            if getattr(meta, 'line', None) is not None:
                node.line = meta.line
                node.col = meta.column
            node.file_name = self.file_name
            if isinstance(node, CompoundSyntaxNode) and getattr(meta, 'end_line', None) is not None:
                node.end_line = meta.end_line
                node.end_col = meta.end_column

        def _binary_chain(self, meta: Any, children: Any, mapping: dict[str, SyntaxNodeType]) -> SyntaxNode:
            items = list(children)
            if not items:
                return self._make_node(SyntaxNodeType.ntExpression, meta)
            node = items[0] if isinstance(items[0], SyntaxNode) else self._make_node(SyntaxNodeType.ntExpression, meta)
            idx = 1
            while idx < len(items):
                op = items[idx]
                rhs = items[idx + 1] if idx + 1 < len(items) else None
                op_key = op.value if is_token(op) else str(op)
                op_key = op_key.upper()
                op_type = mapping.get(op_key) or mapping.get(op_key.lower()) or mapping.get(op_key)
                if rhs is None or op_type is None or not isinstance(rhs, SyntaxNode):
                    break
                op_node = self._make_node(op_type, meta)
                op_node.add_child(node)
                op_node.add_child(rhs)
                node = op_node
                idx += 2
            return node

        def _ensure_expr_node(self, meta: Any, value: Any) -> SyntaxNode:
            if isinstance(value, SyntaxNode):
                return value
            name = token_value(value)
            return self._make_expr_identifier(meta, name)

        def _make_expr_identifier(self, meta: Any, name: str) -> SyntaxNode:
            base, generic_args = self._split_generic_name(name)
            node = self._make_node(SyntaxNodeType.ntIdentifier, meta)
            node.set_attribute(AttributeName.anName, base)
            if generic_args:
                node.set_attribute(AttributeName.anGenericArgs, generic_args)
            return node

        def _flatten(self, value: Any) -> Iterable[Any]:
            if isinstance(value, (list, tuple)):
                for item in value:
                    yield from self._flatten(item)
            else:
                yield value

        def _is_text(self, value: Any) -> bool:
            return isinstance(value, str) and not is_token(value)

        def _proc_type_directive_attrs(self, values: Iterable[str]) -> dict[str, str]:
            attrs: dict[str, str] = {}
            for value in values:
                if value == 'winapi':
                    attrs['callingconvention'] = 'winapi'
                elif value in {'cdecl', 'stdcall', 'pascal', 'register', 'safecall'}:
                    attrs['callingconvention'] = value
                elif value == 'varargs':
                    attrs['varargs'] = 'true'
            return attrs

        def _extract_name(self, children: Iterable[Any], default: str) -> str:
            for child in self._flatten(children):
                if self._is_text(child):
                    return child
            return default

        def _build_program_root(self, meta: Any, kind: str, children: Iterable[Any]) -> SyntaxNode:
            name = self._extract_name(children, default=kind)
            root = self._make_node(SyntaxNodeType.ntUnit, meta)
            root.set_attribute(AttributeName.anName, name)
            root.set_attribute(AttributeName.anKind, kind)
            for child in children:
                if isinstance(child, SyntaxNode):
                    root.add_child(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, SyntaxNode):
                            root.add_child(item)
            return root

    builder = Builder()
    return builder.transform(tree)
