from __future__ import annotations

from .lark_tokens import build_grammar_snippet


GRAMMAR_RULES = r'''
?start: unit_file
      | program_file
      | library_file
      | package_file

unit_file: unit_header interface_section implementation_section? initialization_section? finalization_section? END "."
program_file: program_header program_body "."
library_file: library_header program_body "."
package_file: package_header package_body END "."

unit_header: UNIT qualified_name unit_directive_list? ";" uses_clause?
program_header: PROGRAM qualified_name program_params? ";" uses_clause?
library_header: LIBRARY qualified_name ";" uses_clause?
package_header: PACKAGE qualified_name ";"

unit_directive_list: unit_directive+
unit_directive: DEPRECATED (STRING_LITERAL)?
              | PLATFORM
              | EXPERIMENTAL
              | LIBRARY

program_params: "(" name_list ")"
program_body: block
package_body: requires_clause? contains_clause?

interface_section: INTERFACE uses_clause? interface_decl_sections
implementation_section: IMPLEMENTATION uses_clause? implementation_decl_sections
initialization_section: INITIALIZATION statement_list?
finalization_section: FINALIZATION statement_list?

interface_decl_sections: (label_section | const_section | resourcestring_section | type_section | var_section
                        | threadvar_section | routine_decl | property_decl | exports_section)*
implementation_decl_sections: (label_section | const_section | resourcestring_section | type_section | var_section
                             | threadvar_section | routine_impl | property_decl | exports_section)*

uses_clause: USES uses_item ("," uses_item)* ";"
uses_item: qualified_name (IN STRING_LITERAL)?

label_section: LABEL label_list ";"
label_list: label ("," label)*
label: NAME | INT

const_section: CONST const_decl+
resourcestring_section: RESOURCESTRING const_decl+
const_decl: attribute_sections? NAME (":" type_spec)? "=" const_value decl_directive_list? ";"

type_section: TYPE type_decl+
type_decl: attribute_sections? type_decl_name type_params? "=" forward_class_decl decl_directive_list? ";"
         | attribute_sections? type_decl_name type_params? "=" type_spec decl_directive_list? ";"
type_decl_name: NAME | GENERIC_NAME
forward_class_decl: CLASS type_heritage

decl_directive_list: decl_directive+
decl_directive: DEPRECATED (STRING_LITERAL)?
              | PLATFORM
              | EXPERIMENTAL
              | LIBRARY

var_section: VAR var_decl+
threadvar_section: THREADVAR var_decl+
var_decl: attribute_sections? name_list ":" type_spec absolute_spec? ("=" const_value)? ";"
absolute_spec: ABSOLUTE expr

exports_section: EXPORTS exports_item ("," exports_item)* ";"
exports_item: qualified_name formal_parameters? exports_specifier*
exports_specifier: NAME expr | INDEX expr | RESIDENT

requires_clause: REQUIRES qualified_name ("," qualified_name)* ";"
contains_clause: CONTAINS contains_item ("," contains_item)* ";"
contains_item: qualified_name (IN STRING_LITERAL)?

property_decl: attribute_sections? CLASS? PROPERTY NAME property_params? (":" type_spec)? property_specifier* property_directive* property_directive_block* ";"
property_params: "[" param_list? "]"
property_specifier: INDEX expr | READONLY | WRITEONLY
property_directive_block: ";" property_directive+
property_directive: READ expr
                  | WRITE expr
                  | ADD expr
                  | REMOVE expr
                  | DEFAULT expr?
                  | STORED expr?
                  | NODEFAULT
                  | IMPLEMENTS qualified_name
                  | DISPID expr

routine_decl: resolution_decl
            | routine_heading ";" directive_list?

resolution_decl: (procedure_decl | function_decl | operator_decl | qualified_name) "=" qualified_name ";"

routine_impl: routine_heading ";" body_directive_list? routine_body ";"?
            | routine_heading ";" FORWARD (";" directive)* ";"?
            | routine_heading ";" EXTERNAL external_spec? (";" directive)* ";"?

routine_heading: procedure_decl
               | function_decl
               | constructor_decl
               | destructor_decl
               | operator_decl

procedure_decl: attribute_sections? CLASS? PROCEDURE qualified_name type_params? formal_parameters?
function_decl: attribute_sections? CLASS? FUNCTION qualified_name type_params? formal_parameters? (":" type_spec)?
constructor_decl: attribute_sections? CLASS? CONSTRUCTOR qualified_name type_params? formal_parameters?
destructor_decl: attribute_sections? CLASS? DESTRUCTOR qualified_name type_params? formal_parameters?
operator_decl: attribute_sections? CLASS? OPERATOR operator_target type_params? formal_parameters? (":" type_spec)?
operator_target: operator_name | qualified_name

operator_name: "+" | "-" | "*" | "/" | "=" | "<" | ">" | "<=" | ">=" | "<>" | IN | IS | AS

routine_body: block
            | asm_statement
block: block_decl_sections? compound_statement
block_decl_sections: (label_section | const_section | resourcestring_section | type_section | var_section
                    | threadvar_section | routine_impl | exports_section)*

formal_parameters: "(" param_list? ")"
param_list: param (";" param)*
param: attribute_sections? param_modifier? attribute_sections? name_list ":" type_spec param_default?
param_modifier: CONST | VAR | OUT
param_default: "=" expr

name_list: NAME ("," NAME)*

?type_spec: simple_type
          | pointer_type
          | array_type
          | array_of_const_type
          | set_type
          | structured_type
          | proc_type
          | subrange_type
          | enum_type
          | string_type
          | file_type
          | class_of_type
          | reference_type
          | packed_type

packed_type: PACKED type_spec
simple_type: type_name
type_name: qualified_name type_args?
pointer_type: "^" type_spec
array_type: ARRAY ("[" array_bounds "]")? OF type_spec
array_of_const_type: ARRAY OF CONST
array_bounds: array_bound ("," array_bound)*
array_bound: range_expr (".." range_expr)?
set_type: SET OF type_spec
subrange_type: range_expr ".." range_expr
enum_type: "(" enum_item ("," enum_item)* ")"
enum_item.2: NAME ("=" expr)?
string_type: STRING ("[" expr "]")?
file_type: FILE (OF type_spec)?
class_of_type: CLASS OF type_spec
reference_type: REFERENCE TO proc_type

structured_type: class_type | record_type | interface_type | object_type | helper_type
class_type: CLASS class_modifiers? type_params? type_heritage? class_body? END
record_type: RECORD record_body? END record_align?
record_align: ALIGN expr
object_type: OBJECT type_heritage? class_body? END
interface_type: (INTERFACE | DISPINTERFACE) type_heritage? interface_guid? interface_body? END
helper_type: (CLASS | RECORD) HELPER FOR type_spec class_body? END

class_modifiers: (SEALED | ABSTRACT) (SEALED | ABSTRACT)?
interface_guid: "[" STRING_LITERAL "]"

class_body: class_member*
record_body: class_member*
interface_body: interface_member*

class_member: visibility_spec
            | visibility_spec? class_member_item
class_member_item: field_decl ";"
                 | routine_decl
                 | property_decl
                 | class_var_section
                 | class_const_section
                 | class_type_section
                 | var_section
                 | const_section
                 | type_section
                 | threadvar_section
                 | variant_part

class_var_section: CLASS VAR var_decl+
class_const_section: CLASS CONST const_decl+
class_type_section: CLASS TYPE type_decl+

interface_member: routine_decl | property_decl

visibility_spec: STRICT? PRIVATE
               | STRICT? PROTECTED
               | PUBLIC
               | PUBLISHED
               | AUTOMATED

variant_part: CASE variant_selector? OF variant_section (";" variant_section)* ";"?
variant_selector: NAME ":" type_spec
                | type_spec
variant_section: case_label_list ":" "(" field_list? ")"
field_list: field_decl (";" field_decl)* ";"?
field_decl: attribute_sections? name_list ":" type_spec

proc_type: (PROCEDURE | FUNCTION) formal_parameters? (":" type_spec)? directive_list? of_object?
of_object: OF OBJECT

type_heritage: "(" type_name ("," type_name)* ")"

type_params: "<" type_param ((","|";") type_param)* ">"
type_param: NAME (":" type_constraints)?
type_constraints: type_constraint ((","|";") type_constraint)*
type_constraint: type_spec | CLASS | RECORD | CONSTRUCTOR | INTERFACE | UNMANAGED

type_args: "<" type_spec ("," type_spec)* ">"

attribute_sections: attribute_section+
attribute_section: "[" attribute ("," attribute)* "]"
attribute: attribute_name attribute_arguments?
attribute_name: qualified_name | attribute_keyword
attribute_keyword: IN | OUT | CONST | VAR | UNSAFE
attribute_arguments: "(" arg_list? ")"

directive_list: directive (";" directive)* ";"?
body_directive_list: body_directive (";" body_directive)* ";"?
body_directive: OVERLOAD | OVERRIDE | VIRTUAL | DYNAMIC | ABSTRACT | INLINE | REINTRODUCE | STATIC | FINAL | SEALED
              | STDCALL | CDECL | PASCAL | REGISTER | SAFECALL | WINAPI | MESSAGE expr
              | DEPRECATED (STRING_LITERAL)?
              | PLATFORM
              | EXPERIMENTAL
              | NORETURN
              | VARARGS
              | LOCAL
              | LIBRARY
              | DELAYED
              | EXPORT
              | FAR
              | NEAR
              | ASSEMBLER
              | UNSAFE
              | DISPID expr
directive: OVERLOAD | OVERRIDE | VIRTUAL | DYNAMIC | ABSTRACT | INLINE | REINTRODUCE | STATIC | FINAL | SEALED
         | STDCALL | CDECL | PASCAL | REGISTER | SAFECALL | WINAPI | MESSAGE expr | EXTERNAL external_spec? | FORWARD
         | DEPRECATED (STRING_LITERAL)?
         | PLATFORM
         | EXPERIMENTAL
         | NORETURN
         | VARARGS
         | LOCAL
         | LIBRARY
         | DELAYED
         | EXPORT
         | FAR
         | NEAR
         | ASSEMBLER
         | UNSAFE
         | DISPID expr
external_spec: external_item+
external_item: INDEX expr | DELAYED | expr

const_value: expr | array_const | record_const
array_const: "(" expr_list? ")"
record_const: "(" record_const_item ("," record_const_item)* ")"
record_const_item: NAME ":" const_value
set_const: "[" set_element_list? "]"
set_element_list: set_element ("," set_element)*
set_element: expr (".." expr)?

?statement_list: statement (";" statement)* ";"?

?statement: compound_statement
          | if_statement
          | while_statement
          | for_statement
          | repeat_statement
          | try_statement
          | raise_statement
          | case_statement
          | with_statement
          | asm_statement
          | inline_statement
          | inline_var_section
          | inline_const_section
          | goto_statement
          | label_statement
          | inherited_statement
          | assignment
          | call_statement
          | break_statement
          | continue_statement
          | exit_statement

compound_statement: BEGIN statement_list? END
if_statement: IF expr THEN statement (ELSE statement)?
while_statement: WHILE expr DO statement
for_statement: FOR for_init (TO|DOWNTO) expr DO statement
             | FOR for_in DO statement
for_init: NAME ASSIGN expr
        | VAR NAME ASSIGN expr
for_in: NAME IN expr
      | VAR NAME IN expr
repeat_statement: REPEAT statement_list UNTIL expr
case_statement: CASE expr OF case_selector_list case_else? END
case_selector_list: case_selector (";" case_selector)* ";"?
case_selector: case_label_list ":" statement
case_label_list: case_label ("," case_label)*
case_label: expr (".." expr)?
case_else: ELSE statement_list?
with_statement: WITH expr_list DO statement
try_statement: TRY statement_list (except_block | finally_block) END
except_block: EXCEPT statement_list
            | EXCEPT exception_handler+ (ELSE statement_list)?
            | EXCEPT
exception_handler: ON qualified_name (":" qualified_name)? DO statement_list?
finally_block: FINALLY statement_list?
raise_statement: RAISE expr? (AT expr)?
goto_statement: GOTO label
label_statement: label ":" statement
inherited_statement: INHERITED call_statement?
break_statement: BREAK
continue_statement: CONTINUE
exit_statement: EXIT (expr)?
asm_statement: ASM asm_item* END
asm_item: NAME | INT | HEX_INT | BIN_INT | FLOAT | STRING_LITERAL | CHAR_CODE | "." | "," | ":" | "+" | "-" | "*" | "/" | "[" | "]" | "(" | ")" | "@" | "^" | "="
inline_statement: INLINE "(" inline_number ("/" inline_number)* ")"
inline_number: INT | HEX_INT
inline_const_section: CONST inline_const_decl
inline_const_decl: attribute_sections? NAME (":" type_spec)? "=" const_value
inline_var_section: VAR inline_var_decl
inline_var_decl: attribute_sections? name_list (":" type_spec)? (ASSIGN expr)?

assignment: postfix_expr ASSIGN expr
call_statement: postfix_expr

?expr_list: expr ("," expr)*

?expr: if_expr
     | or_expr
if_expr: IF expr THEN expr ELSE expr
?or_expr: xor_expr (OR xor_expr)*
?xor_expr: and_expr (XOR and_expr)*
?and_expr: rel_expr (AND rel_expr)*
?rel_expr: add_expr (rel_op add_expr)*
rel_op: "=" | "<>" | "<" | "<=" | ">" | ">=" | IN | IS | AS | not_in_op | is_not_op
not_in_op: NOT IN
is_not_op: IS NOT
?add_expr: mul_expr (("+"|"-") mul_expr)*
?mul_expr: unary_expr (("*"|"/"|DIV|MOD|SHL|SHR) unary_expr)*
?unary_expr: (NOT|"+"|"-"|"@"|"^") unary_expr
           | postfix_expr

?postfix_expr: primary (call_suffix | index_suffix | field_suffix | deref_suffix)*
call_suffix: "(" arg_list? ")"
index_suffix: "[" expr_list "]"
field_suffix: "." NAME
deref_suffix: "^"
arg_list: argument ("," argument)*
argument: NAME ":" expr | expr

?primary: literal
        | expr_qualified_name
        | set_const
        | anonymous_method
        | "(" expr ")"
        | NIL
        | TRUE
        | FALSE
        | SELF

anonymous_method: (PROCEDURE formal_parameters? | FUNCTION formal_parameters? ":" type_spec) block

literal: STRING_BLOCK5 | STRING_BLOCK3 | STRING_LITERAL | CHAR_CODE | POINTER_CHAR | FLOAT | HEX_INT | BIN_INT | INT

expr_qualified_name: expr_identifier ("." expr_identifier)*
expr_identifier: NAME | GENERIC_NAME

qualified_name: qualified_name_part ("." qualified_name_part)*
qualified_name_part: NAME | GENERIC_NAME

?range_expr: range_unary
?range_unary: ("+"|"-") range_unary | range_primary
?range_primary: literal | qualified_name | "(" range_expr ")"

ASSIGN: ":="
'''


def build_grammar() -> str:
    return build_grammar_snippet() + '\n' + GRAMMAR_RULES
