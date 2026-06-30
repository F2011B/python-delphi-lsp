from __future__ import annotations

import re


KEYWORDS = [
    'abort',
    'absolute',
    'abstract',
    'add',
    'align',
    'and',
    'ansistring',
    'array',
    'as',
    'asm',
    'assembler',
    'at',
    'automated',
    'begin',
    'boolean',
    'break',
    'byte',
    'bytebool',
    'cardinal',
    'case',
    'cdecl',
    'char',
    'class',
    'comp',
    'const',
    'constructor',
    'contains',
    'continue',
    'currency',
    'default',
    'delayed',
    'deprecated',
    'destructor',
    'dispid',
    'dispinterface',
    'div',
    'do',
    'double',
    'downto',
    'dword',
    'dynamic',
    'else',
    'end',
    'except',
    'exit',
    'experimental',
    'export',
    'exports',
    'extended',
    'false',
    'external',
    'far',
    'file',
    'final',
    'finalization',
    'finally',
    'for',
    'forward',
    'function',
    'goto',
    'halt',
    'helper',
    'if',
    'implementation',
    'implements',
    'in',
    'index',
    'inherited',
    'initialization',
    'inline',
    'int64',
    'integer',
    'interface',
    'is',
    'label',
    'library',
    'local',
    'longbool',
    'longint',
    'longword',
    'message',
    'mod',
    'namespace',
    'near',
    'nil',
    'noreturn',
    'nodefault',
    'null',
    'not',
    'object',
    'of',
    'olevariant',
    'on',
    'operator',
    'or',
    'out',
    'overload',
    'override',
    'package',
    'packed',
    'pascal',
    'pchar',
    'platform',
    'private',
    'procedure',
    'program',
    'property',
    'protected',
    'public',
    'published',
    'raise',
    'read',
    'readonly',
    'real',
    'real48',
    'record',
    'reference',
    'register',
    'reintroduce',
    'remove',
    'repeat',
    'requires',
    'resident',
    'resourcestring',
    'runerror',
    'safecall',
    'sealed',
    'self',
    'set',
    'shl',
    'shortint',
    'shortstring',
    'shr',
    'single',
    'smallint',
    'static',
    'stdcall',
    'stored',
    'strict',
    'string',
    'stringresource',
    'then',
    'threadvar',
    'true',
    'to',
    'try',
    'type',
    'unit',
    'unmanaged',
    'unsafe',
    'until',
    'uses',
    'var',
    'varargs',
    'variant',
    'virtual',
    'while',
    'widechar',
    'widestring',
    'with',
    'word',
    'wordbool',
    'write',
    'writeonly',
    'winapi',
    'xor',
]

DIRECTIVE_KEYWORDS = [
    'define',
    'endif',
    'ifend',
    'elseif',
    'ifdef',
    'ifndef',
    'ifopt',
    'include',
    'resource',
    'scopedenums',
    'undef',
]

def _keyword_terminal(keyword: str) -> str:
    token_name = f"{keyword.upper()}.2"
    pattern = re.escape(keyword)
    return f"{token_name}: /(?i:{pattern})(?!\\w)/"


KEYWORD_TERMINALS = "\n".join(_keyword_terminal(k) for k in KEYWORDS)


BASE_TERMINALS = r"""
// literals
STRING_BLOCK5: /(?s:'''''(?:.*?)''''')/
STRING_BLOCK3: /(?s:'''(?:.*?)''')/
STRING_LITERAL: /(?s:'(?:''|[^'])*')/
CHAR_CODE: /#\$[0-9A-Fa-f_]+|#[0-9_]+/
POINTER_CHAR: /\^[A-Za-z\\]/
HEX_INT: /\$[0-9A-Fa-f_]+/
BIN_INT: /%[01_]+/
FLOAT: /(?:[0-9][0-9_]*\.(?!\.)[0-9_]*([eE][+-]?[0-9_]+)?|[0-9][0-9_]*[eE][+-]?[0-9_]+)/
INT: /[0-9][0-9_]*/

// identifiers
GENERIC_NAME: /&?(?:[_]|[^\W\d_])[\w]*<(?:[^<>\r\n]+|<(?:[^<>\r\n]+|<(?:[^<>\r\n]+|<[^<>\r\n]*>)*>)*>)*>/
NAME: /&?(?:[_]|[^\W\d_])[\w]*/

// comments
COMMENT_BRACE: /(?s:\{.*?\})/
COMMENT_PAREN: /(?s:\(\*.*?\*\))/
COMMENT_SLASH: /\/\/[^\n]*/

%ignore COMMENT_BRACE
%ignore COMMENT_PAREN
%ignore COMMENT_SLASH

%import common.WS_INLINE
%import common.NEWLINE -> _NL
%ignore WS_INLINE
%ignore _NL
"""


def build_grammar_snippet() -> str:
    return '\n'.join([KEYWORD_TERMINALS, BASE_TERMINALS])
