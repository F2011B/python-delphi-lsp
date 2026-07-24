from __future__ import annotations

from delphi_lsp.consts import AttributeName, SyntaxNodeType
from delphi_lsp.delphiast_parser import DelphiAstParser
from delphi_lsp.nodes import SyntaxNode, ValuedSyntaxNode


def walk(node: SyntaxNode):
    yield node
    for child in node.child_nodes:
        yield from walk(child)


def property_named(root: SyntaxNode, name: str) -> SyntaxNode:
    return next(
        node
        for node in walk(root)
        if node.typ is SyntaxNodeType.ntProperty
        and node.get_attribute(AttributeName.anName) == name
    )


def test_property_matches_delphiast_parameter_and_accessor_shape() -> None:
    source = """
unit PropertyShape;
interface
type
  TItems = class
  public
    property Items[const Index, Other: Integer]: string
      read FItems[Index] write SetItem stored IsStored default 50;
  end;
implementation
end.
"""

    result = DelphiAstParser(source, "PropertyShape.pas").parse()
    prop = property_named(result.root, "Items")

    assert [child.typ for child in prop.child_nodes] == [
        SyntaxNodeType.ntParameter,
        SyntaxNodeType.ntParameter,
        SyntaxNodeType.ntType,
        SyntaxNodeType.ntRead,
        SyntaxNodeType.ntWrite,
        SyntaxNodeType.ntUnknown,
        SyntaxNodeType.ntDefault,
    ]
    first_parameter = prop.child_nodes[0]
    assert first_parameter.get_attribute(AttributeName.anKind) == "const"
    assert isinstance(first_parameter.child_nodes[0], ValuedSyntaxNode)
    assert first_parameter.child_nodes[0].typ is SyntaxNodeType.ntName
    assert first_parameter.child_nodes[0].value == "Index"
    assert first_parameter.child_nodes[1].get_attribute(AttributeName.anName) == "Integer"
    assert prop.child_nodes[2].get_attribute(AttributeName.anName) == "string"

    read = prop.child_nodes[3]
    assert read.child_nodes[0].typ is SyntaxNodeType.ntIndexed
    write = prop.child_nodes[4]
    assert write.child_nodes[0].get_attribute(AttributeName.anName) == "SetItem"
    stored = prop.child_nodes[5]
    assert stored.get_attribute(AttributeName.anKind) == "stored"
    assert stored.child_nodes[0].get_attribute(AttributeName.anName) == "IsStored"
    default = prop.child_nodes[6]
    assert isinstance(default.child_nodes[0], ValuedSyntaxNode)
    assert default.child_nodes[0].typ is SyntaxNodeType.ntLiteral
    assert default.child_nodes[0].value == "50"


def test_property_supports_index_readonly_nodefault_and_directive_block() -> None:
    source = """
unit PropertyDirectives;
interface
type
  TItems = class
    property Current: Integer index 7 readonly;
    property DefaultItem: Integer read GetItem; default;
    property Mutable: Integer write SetItem nodefault;
    property Hook: Integer add AddHook remove RemoveHook;
  end;
implementation
end.
"""

    result = DelphiAstParser(source, "PropertyDirectives.pas").parse()

    current = property_named(result.root, "Current")
    assert [child.typ for child in current.child_nodes] == [
        SyntaxNodeType.ntType,
        SyntaxNodeType.ntIndex,
        SyntaxNodeType.ntUnknown,
    ]
    assert current.child_nodes[-1].get_attribute(AttributeName.anKind) == "readonly"

    default_item = property_named(result.root, "DefaultItem")
    assert default_item.child_nodes[-1].typ is SyntaxNodeType.ntDefault
    assert not default_item.child_nodes[-1].child_nodes

    mutable = property_named(result.root, "Mutable")
    assert mutable.child_nodes[-1].typ is SyntaxNodeType.ntDefault
    assert mutable.child_nodes[-1].get_attribute(AttributeName.anKind) == "nodefault"

    hook = property_named(result.root, "Hook")
    assert [
        child.get_attribute(AttributeName.anKind) for child in hook.child_nodes[-2:]
    ] == ["add", "remove"]


def test_generic_parameter_groups_clone_delphiast_constraints_per_name() -> None:
    source = """
unit GenericShape;
interface
type
  TFoo<TFirst, TSecond: class;
       TThird: TComponent, IUnknown, constructor;
       TFourth: record;
       TFifth: interface;
       TSixth: unmanaged> = class
  end;
implementation
end.
"""

    result = DelphiAstParser(source, "GenericShape.pas").parse()
    type_params = next(
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntTypeParams
    )

    assert len(type_params.child_nodes) == 6
    by_name = {
        parameter.child_nodes[0].value: parameter
        for parameter in type_params.child_nodes
    }
    assert all(
        parameter.child_nodes[0].typ is SyntaxNodeType.ntName
        for parameter in type_params.child_nodes
    )
    assert by_name["TFirst"].child_nodes[1].child_nodes[0].typ is SyntaxNodeType.ntClassConstraint
    assert by_name["TSecond"].child_nodes[1].child_nodes[0].typ is SyntaxNodeType.ntClassConstraint
    assert [
        child.typ for child in by_name["TThird"].child_nodes[1].child_nodes
    ] == [
        SyntaxNodeType.ntType,
        SyntaxNodeType.ntType,
        SyntaxNodeType.ntConstructorConstraint,
    ]
    assert by_name["TFourth"].child_nodes[1].child_nodes[0].typ is SyntaxNodeType.ntRecordConstraint
    assert by_name["TFifth"].child_nodes[1].child_nodes[0].typ is SyntaxNodeType.ntInterfaceConstraint
    assert by_name["TSixth"].child_nodes[1].child_nodes[0].typ is SyntaxNodeType.ntUnmanagedConstraint


def test_forward_class_type_does_not_consume_following_type_declarations() -> None:
    source = """
unit ForwardTypes;
interface
type
  TFirst<T: TObject> = class(TObject);
  TSecond<T: class> = class(TObject);
implementation
end.
"""

    result = DelphiAstParser(source, "ForwardTypes.pas").parse()

    declarations = [
        node.get_attribute(AttributeName.anName)
        for node in walk(result.root)
        if node.typ is SyntaxNodeType.ntTypeDecl
    ]
    assert declarations == ["TFirst", "TSecond"]
    assert not result.problems


def test_nested_type_and_typed_set_constant_match_delphiast_shape() -> None:
    source = """
unit ConstantShape;
interface
type
  TContainer = class
  public type
    TKind = (One, Two, Three);
  end;
const
  Enabled: set of TContainer.TKind = [
    TContainer.TKind.One,
    TContainer.TKind.Two
  ];
implementation
end.
"""

    result = DelphiAstParser(source, "ConstantShape.pas").parse()
    declarations = [
        node.get_attribute(AttributeName.anName)
        for node in walk(result.root)
        if node.typ is SyntaxNodeType.ntTypeDecl
    ]
    assert declarations == ["TContainer", "TKind"]

    constant = next(
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntConstant
    )
    assert isinstance(constant.child_nodes[0], ValuedSyntaxNode)
    assert constant.child_nodes[0].typ is SyntaxNodeType.ntName
    assert constant.child_nodes[0].value == "Enabled"
    set_type = constant.child_nodes[1]
    assert set_type.get_attribute(AttributeName.anType) == "set"
    assert set_type.child_nodes[0].get_attribute(AttributeName.anName) == "TContainer.TKind"
    value = constant.child_nodes[2]
    assert value.typ is SyntaxNodeType.ntValue
    set_value = value.child_nodes[0]
    assert set_value.typ is SyntaxNodeType.ntSet
    assert [child.typ for child in set_value.child_nodes] == [
        SyntaxNodeType.ntDot,
        SyntaxNodeType.ntDot,
    ]


def test_multiline_and_simple_strings_are_single_dequoted_literals() -> None:
    source = """unit StringShape;
interface
const
  Simple = 'It''s fast';
  Block = '''
line one
line two
''';
implementation
end.
"""

    result = DelphiAstParser(source, "StringShape.pas").parse()
    constants = [
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntConstant
    ]

    values = {}
    for constant in constants:
        name = constant.child_nodes[0].value
        literal = constant.find_node(SyntaxNodeType.ntValue).child_nodes[0]
        values[name] = literal
    assert values["Simple"].value == "It's fast"
    assert values["Simple"].get_attribute(AttributeName.anType) == "string"
    assert values["Block"].value == "'\nline one\nline two\n'"
    assert values["Block"].get_attribute(AttributeName.anType) == "string"
    assert not result.problems


def test_variant_record_keeps_selector_labels_and_attributed_fields() -> None:
    source = """
unit VariantShape;
interface
type
  TVariant = record
    case Byte of
      1, 2: (
        Plain: Double;
        [Example(42)]
        Decorated: Integer
      );
  end;
implementation
end.
"""

    result = DelphiAstParser(source, "VariantShape.pas").parse()

    selector_type = next(
        node
        for node in walk(result.root)
        if node.typ is SyntaxNodeType.ntType
        and node.get_attribute(AttributeName.anName) == "Byte"
    )
    assert selector_type is not None
    labels = next(
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntCaseLabels
    )
    assert [label.child_nodes[0].value for label in labels.child_nodes] == ["1", "2"]
    fields = [
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntField
    ]
    assert [field.child_nodes[0].value for field in fields] == ["Plain", "Decorated"]
    attributes = fields[1].find_node(SyntaxNodeType.ntAttributes)
    assert attributes is not None
    assert attributes.child_nodes[0].get_attribute(AttributeName.anName) == "Example"
    assert attributes.child_nodes[0].find_node(SyntaxNodeType.ntArguments) is not None
    assert not result.problems


def test_try_except_builds_handlers_and_recovery_statements() -> None:
    source = """
unit TryShape;
interface
implementation
procedure Run;
begin
  try
    Work;
  except
    on E: Exception do
      Handle(E);
    else
      HandleUnknown;
  end;
end;
end.
"""

    result = DelphiAstParser(source, "TryShape.pas").parse()

    try_node = next(
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntTry
    )
    except_node = try_node.find_node(SyntaxNodeType.ntExcept)
    assert except_node is not None
    assert (
        sum(
            node.typ is SyntaxNodeType.ntExceptionHandler
            for node in except_node.child_nodes
        )
        == 1
    )
    assert any(node.typ is SyntaxNodeType.ntCall for node in walk(except_node))
    assert not result.problems


def test_qualified_generic_routine_names_keep_suffix_and_signature() -> None:
    source = """
unit GenericRoutineShape;
interface
type
  TGenerator<T, TResult> = class
  private
    function TFunc<T, IEnumerable<TResult>>.Invoke = Bind;
  public
    function Bind(Value: T): IEnumerable<TResult>;
  end;
implementation
function TGenerator<T, TResult>.Bind(Value: T): IEnumerable<TResult>;
begin
end;
end.
"""

    result = DelphiAstParser(source, "GenericRoutineShape.pas").parse()
    methods = [
        node for node in walk(result.root) if node.typ is SyntaxNodeType.ntMethod
    ]
    by_name = {
        method.get_attribute(AttributeName.anName): method for method in methods
    }

    assert "TFunc.Invoke" in by_name
    assert (
        by_name["TFunc.Invoke"]
        .find_node(SyntaxNodeType.ntResolutionClause)
        .get_attribute(AttributeName.anName)
        == "Bind"
    )
    implementation = by_name["TGenerator.Bind"]
    assert implementation.find_node(SyntaxNodeType.ntParameters) is not None
    assert implementation.find_node(SyntaxNodeType.ntReturnType) is not None
    assert implementation.find_node(SyntaxNodeType.ntStatements) is not None
    assert not result.problems
