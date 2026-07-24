from __future__ import annotations

from delphi_lsp.delphiast_lexer import DelphiAstLexer
from delphi_lsp.delphiast_tokens import TokenKind


def significant(source: str):
    return [
        token
        for token in DelphiAstLexer(source, "Demo.pas").tokenize()
        if token.kind is not TokenKind.EOF
    ]


def test_lexer_recognizes_keywords_identifiers_and_escaped_identifiers() -> None:
    tokens = significant("unit Demo; type &type = Integer;")

    assert [(token.kind, token.value) for token in tokens] == [
        (TokenKind.KEYWORD, "unit"),
        (TokenKind.IDENTIFIER, "Demo"),
        (TokenKind.SYMBOL, ";"),
        (TokenKind.KEYWORD, "type"),
        (TokenKind.IDENTIFIER, "type"),
        (TokenKind.SYMBOL, "="),
        (TokenKind.KEYWORD, "Integer"),
        (TokenKind.SYMBOL, ";"),
    ]


def test_lexer_recognizes_numbers_strings_and_character_codes() -> None:
    tokens = significant("$ff %101 42 1.5 1..2 'a''b' #13 #$0A")

    assert [(token.kind, token.value) for token in tokens] == [
        (TokenKind.NUMBER, "$ff"),
        (TokenKind.NUMBER, "%101"),
        (TokenKind.NUMBER, "42"),
        (TokenKind.NUMBER, "1.5"),
        (TokenKind.NUMBER, "1"),
        (TokenKind.SYMBOL, ".."),
        (TokenKind.NUMBER, "2"),
        (TokenKind.STRING, "'a''b'"),
        (TokenKind.CHAR_CODE, "#13"),
        (TokenKind.CHAR_CODE, "#$0A"),
    ]


def test_lexer_preserves_comment_and_directive_kinds_when_requested() -> None:
    source = "{plain}\n{$IFDEF FPC}\n(* block *) // line\nunit Demo;"

    tokens = DelphiAstLexer(source, "Demo.pas").tokenize(include_trivia=True)

    assert [(token.kind, token.value) for token in tokens[:4]] == [
        (TokenKind.COMMENT, "{plain}"),
        (TokenKind.DIRECTIVE, "{$IFDEF FPC}"),
        (TokenKind.COMMENT, "(* block *)"),
        (TokenKind.COMMENT, "// line"),
    ]
    unit = next(token for token in tokens if token.value == "unit")
    assert (unit.line, unit.column) == (4, 1)


def test_lexer_recognizes_compound_operators_without_backtracking() -> None:
    tokens = significant("A:=B<=C>=D<>E..F**G += H -= I *= J /= K")

    assert [token.value for token in tokens if token.kind is TokenKind.SYMBOL] == [
        ":=",
        "<=",
        ">=",
        "<>",
        "..",
        "**",
        "+=",
        "-=",
        "*=",
        "/=",
    ]


def test_lexer_always_makes_progress_on_invalid_unicode_and_unclosed_text() -> None:
    tokens = significant("🙂 'unterminated\n{comment")

    assert tokens[0].kind is TokenKind.UNKNOWN
    assert tokens[1].kind is TokenKind.STRING
    assert tokens[-1].kind is TokenKind.COMMENT
    assert all(token.end > token.start for token in tokens)


def test_lexer_compacts_very_large_constant_initializers() -> None:
    values = ",".join("$FF" for _ in range(20_000))
    source = f"const Lookup = ({values}); procedure Done;"

    tokens = significant(source)

    assert len(tokens) < 12
    opaque = next(token for token in tokens if token.value == "<large-initializer>")
    assert opaque.end - opaque.start > 64_000
    assert tokens[-3].value.casefold() == "procedure"
