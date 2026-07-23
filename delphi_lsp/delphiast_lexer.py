"""Linear Delphi/Object Pascal lexer ported from DelphiAST/SimpleParser.

The implementation intentionally uses direct character dispatch rather than a
backtracking regular-expression lexer.  Source: DelphiAST, MPL-2.0,
https://github.com/RomanYankovsky/DelphiAST
"""

from __future__ import annotations

from .delphiast_tokens import Token, TokenKind
from .lark_tokens import KEYWORDS


_KEYWORDS = frozenset(value.casefold() for value in KEYWORDS)
_COMPOUND_SYMBOLS = frozenset(
    {
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
        "><",
        "=>",
    }
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class DelphiAstLexer:
    def __init__(self, source: str, file_name: str = "") -> None:
        self.source = source
        self.file_name = file_name
        self._index = 0
        self._line = 1
        self._column = 1

    def tokenize(self, *, include_trivia: bool = False) -> list[Token]:
        del include_trivia  # Comments/directives are useful parser evidence.
        tokens: list[Token] = []
        while self._index < len(self.source):
            character = self.source[self._index]
            if character.isspace():
                self._advance_to(self._index + 1)
                continue
            token = self._next_token()
            tokens.append(token)
            if token.end <= token.start:  # Defensive forward-progress guard.
                self._advance_to(self._index + 1)
        tokens.append(
            Token(
                TokenKind.EOF,
                "",
                self._line,
                self._column,
                self._index,
                self._index,
                self.file_name,
            )
        )
        return tokens

    def _next_token(self) -> Token:
        start = self._index
        line = self._line
        column = self._column
        character = self.source[start]
        following = self.source[start + 1] if start + 1 < len(self.source) else ""

        if character == "/" and following == "/":
            end = self.source.find("\n", start + 2)
            end = len(self.source) if end < 0 else end
            return self._emit(TokenKind.COMMENT, start, end, line, column)
        if character == "{":
            end = self.source.find("}", start + 1)
            end = len(self.source) if end < 0 else end + 1
            kind = TokenKind.DIRECTIVE if following == "$" else TokenKind.COMMENT
            return self._emit(kind, start, end, line, column)
        if character == "(" and following == "*":
            end = self.source.find("*)", start + 2)
            end = len(self.source) if end < 0 else end + 2
            directive = start + 2 < len(self.source) and self.source[start + 2] == "$"
            return self._emit(
                TokenKind.DIRECTIVE if directive else TokenKind.COMMENT,
                start,
                end,
                line,
                column,
            )
        if character == "'":
            end = self._quoted_end(start)
            return self._emit(TokenKind.STRING, start, end, line, column)
        if character == "#":
            end = start + 1
            if end < len(self.source) and self.source[end] == "$":
                end += 1
                while end < len(self.source) and self.source[end] in _HEX_DIGITS:
                    end += 1
            else:
                while end < len(self.source) and self.source[end].isdigit():
                    end += 1
            return self._emit(TokenKind.CHAR_CODE, start, max(end, start + 1), line, column)
        if character == "$":
            end = start + 1
            while end < len(self.source) and self.source[end] in _HEX_DIGITS:
                end += 1
            return self._emit(TokenKind.NUMBER, start, end, line, column)
        if character == "%" and following in {"0", "1"}:
            end = start + 1
            while end < len(self.source) and self.source[end] in {"0", "1"}:
                end += 1
            return self._emit(TokenKind.NUMBER, start, end, line, column)
        if character == "&" and following.isdigit():
            end = start + 1
            while end < len(self.source) and self.source[end] in "01234567":
                end += 1
            return self._emit(TokenKind.NUMBER, start, end, line, column)
        if character == "&" and _identifier_start(following):
            end = start + 2
            while end < len(self.source) and _identifier_part(self.source[end]):
                end += 1
            token = self._emit(TokenKind.IDENTIFIER, start, end, line, column)
            return Token(
                token.kind,
                token.value[1:],
                token.line,
                token.column,
                token.start,
                token.end,
                token.file_name,
            )
        if _identifier_start(character):
            end = start + 1
            while end < len(self.source) and _identifier_part(self.source[end]):
                end += 1
            value = self.source[start:end]
            kind = TokenKind.KEYWORD if value.casefold() in _KEYWORDS else TokenKind.IDENTIFIER
            return self._emit(kind, start, end, line, column)
        if character.isdigit():
            end = self._number_end(start)
            return self._emit(TokenKind.NUMBER, start, end, line, column)

        compound = self.source[start : start + 2]
        if compound in _COMPOUND_SYMBOLS:
            return self._emit(TokenKind.SYMBOL, start, start + 2, line, column)
        if character in ";,:.=+-*/()[]^@<>":
            return self._emit(TokenKind.SYMBOL, start, start + 1, line, column)
        return self._emit(TokenKind.UNKNOWN, start, start + 1, line, column)

    def _number_end(self, start: int) -> int:
        end = start
        while end < len(self.source) and self.source[end].isdigit():
            end += 1
        if (
            end < len(self.source)
            and self.source[end] == "."
            and self.source[end : end + 2] != ".."
        ):
            end += 1
            while end < len(self.source) and self.source[end].isdigit():
                end += 1
        if end < len(self.source) and self.source[end] in {"e", "E"}:
            exponent = end + 1
            if exponent < len(self.source) and self.source[exponent] in {"+", "-"}:
                exponent += 1
            digit_start = exponent
            while exponent < len(self.source) and self.source[exponent].isdigit():
                exponent += 1
            if exponent > digit_start:
                end = exponent
        return end

    def _quoted_end(self, start: int) -> int:
        end = start + 1
        while end < len(self.source):
            if self.source[end] in {"\r", "\n"}:
                return end
            if self.source[end] != "'":
                end += 1
                continue
            if end + 1 < len(self.source) and self.source[end + 1] == "'":
                end += 2
                continue
            return end + 1
        return len(self.source)

    def _emit(
        self,
        kind: TokenKind,
        start: int,
        end: int,
        line: int,
        column: int,
    ) -> Token:
        value = self.source[start:end]
        self._advance_to(end)
        return Token(kind, value, line, column, start, end, self.file_name)

    def _advance_to(self, end: int) -> None:
        while self._index < end:
            if self.source[self._index] == "\n":
                self._line += 1
                self._column = 1
            else:
                self._column += 1
            self._index += 1


def _identifier_start(character: str) -> bool:
    return bool(character) and (character == "_" or character.isalpha())


def _identifier_part(character: str) -> bool:
    return character == "_" or character.isalnum()


__all__ = ["DelphiAstLexer"]
