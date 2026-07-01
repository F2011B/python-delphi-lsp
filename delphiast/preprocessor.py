from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .source_reader import read_source_text


IncludeLoader = Callable[[str, str], Optional[tuple[str, str]]]


@dataclass(frozen=True)
class SourceMapEntry:
    file_name: str
    line: int
    col_offset: int = 0


@dataclass
class PreprocessorProblem:
    kind: str
    message: str
    file_name: str
    line: int
    col: int


@dataclass
class CommentInfo:
    kind: str
    text: str
    file_name: str
    line: int
    col: int
    end_line: int
    end_col: int


@dataclass
class PreprocessedSource:
    text: str
    source_map: list[SourceMapEntry]
    defines: set[str]
    scoped_enums: bool
    problems: list[PreprocessorProblem]
    comments: list[CommentInfo]

    def map_position(self, line: int, col: int) -> tuple[str, int, int]:
        if line < 1 or line > len(self.source_map):
            return ('', 0, 0)
        entry = self.source_map[line - 1]
        return (entry.file_name, entry.line, col + entry.col_offset)


@dataclass
class PreprocessorOptions:
    use_defines: bool = True
    compiler_version: float = 36.0
    rtl_version: float = 36.0
    scoped_enums: bool = False
    option_states: dict[str, bool] = field(default_factory=dict)


@dataclass
class _ConditionalFrame:
    parent_active: bool
    branch_taken: bool
    current_active: bool


@dataclass
class _FileContext:
    text: str
    file_name: str
    index: int = 0
    line: int = 1
    col: int = 1
    pending_output: str = ''
    pending_line: int = 1

    def peek(self, offset: int = 0) -> str:
        idx = self.index + offset
        if idx < 0 or idx >= len(self.text):
            return ''
        return self.text[idx]

    def advance(self, count: int = 1) -> str:
        if count <= 0:
            return ''
        end = min(len(self.text), self.index + count)
        chunk = self.text[self.index:end]
        for ch in chunk:
            if ch == '\n':
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.index = end
        return chunk

    def eof(self) -> bool:
        return self.index >= len(self.text)


class _OutputBuffer:
    def __init__(self) -> None:
        self._lines: list[str] = ['']
        self._line_map: list[SourceMapEntry] = []

    @property
    def text(self) -> str:
        return '\n'.join(self._lines)

    @property
    def line_map(self) -> list[SourceMapEntry]:
        return self._line_map

    def append_char(self, ch: str, file_name: str, line: int) -> None:
        self._ensure_mapping(file_name, line)
        if ch == '\n':
            self._lines.append('')
        else:
            self._lines[-1] += ch

    def _ensure_mapping(self, file_name: str, line: int) -> None:
        if len(self._line_map) < len(self._lines):
            self._line_map.append(SourceMapEntry(file_name=file_name, line=line, col_offset=0))


class Preprocessor:
    def __init__(
        self,
        *,
        defines: Iterable[str] = (),
        include_paths: Iterable[str] = (),
        include_loader: Optional[IncludeLoader] = None,
        options: Optional[PreprocessorOptions] = None,
    ) -> None:
        self.defines: set[str] = {self._normalize_define(d) for d in defines if d}
        self.include_paths = [Path(p) for p in include_paths]
        self.include_loader = include_loader or self._default_include_loader
        self.options = options or PreprocessorOptions()
        self._apply_default_compiler_defines()
        self.scoped_enums = self.options.scoped_enums
        self._option_values: dict[str, str] = {}
        self._option_stack: list[tuple[dict[str, str], bool]] = []
        self._problems: list[PreprocessorProblem] = []
        self._comments: list[CommentInfo] = []
        self._conditional_stack: list[_ConditionalFrame] = []

    def process(self, text: str, file_name: str) -> PreprocessedSource:
        self._problems = []
        self._comments = []
        self._conditional_stack = []
        self._option_stack = []
        self.scoped_enums = self.options.scoped_enums
        self._option_values = {
            self._normalize_option_name(name): 'ON' if bool(value) else 'OFF'
            for name, value in self.options.option_states.items()
        }
        if 'SCOPEDENUMS' in self._option_values:
            self.scoped_enums = self._option_values['SCOPEDENUMS'] == 'ON'
        else:
            self._set_option('SCOPEDENUMS', 'ON' if self.scoped_enums else 'OFF')

        normalized = self._normalize_newlines(text)
        contexts = [_FileContext(text=normalized, file_name=file_name)]
        include_stack = [file_name]
        output = _OutputBuffer()

        while contexts:
            ctx = contexts[-1]

            if ctx.pending_output:
                pending = ctx.pending_output
                ctx.pending_output = ''
                pending_line = ctx.pending_line
                ctx.pending_line = ctx.line
                self._emit_text(output, pending, ctx.file_name, pending_line, active=True)
                continue

            if ctx.eof():
                contexts.pop()
                if include_stack:
                    include_stack.pop()
                continue

            ch = ctx.peek()

            if ch == "'":
                start_line = ctx.line
                active = self._is_active()
                literal = self._consume_string(ctx)
                self._emit_text(output, literal, ctx.file_name, start_line, active=active)
                continue

            if ch == '/' and ctx.peek(1) == '/':
                start_line = ctx.line
                start_col = ctx.col
                active = self._is_active()
                raw, content = self._consume_line_comment(ctx)
                self._emit_text(output, raw, ctx.file_name, start_line, active=active)
                if active:
                    self._record_comment('slashes', content, ctx.file_name, start_line, start_col, ctx)
                continue

            if ch == '{':
                start_line = ctx.line
                start_col = ctx.col
                active = self._is_active()
                if ctx.peek(1) == '$':
                    raw_text, content = self._consume_brace_directive(ctx)
                    self._handle_directive(
                        raw_text,
                        content,
                        ctx,
                        start_line,
                        contexts,
                        include_stack,
                        output,
                    )
                else:
                    raw, content = self._consume_brace_comment(ctx)
                    self._emit_text(output, raw, ctx.file_name, start_line, active=active)
                    if active:
                        self._record_comment('borland', content, ctx.file_name, start_line, start_col, ctx)
                continue

            if ch == '(' and ctx.peek(1) == '*':
                start_line = ctx.line
                start_col = ctx.col
                active = self._is_active()
                if ctx.peek(2) == '$':
                    raw_text, content = self._consume_paren_directive(ctx)
                    self._handle_directive(
                        raw_text,
                        content,
                        ctx,
                        start_line,
                        contexts,
                        include_stack,
                        output,
                    )
                else:
                    raw, content = self._consume_paren_comment(ctx)
                    self._emit_text(output, raw, ctx.file_name, start_line, active=active)
                    if active:
                        self._record_comment('ansi', content, ctx.file_name, start_line, start_col, ctx)
                continue

            start_line = ctx.line
            ch = ctx.advance(1)
            self._emit_text(output, ch, ctx.file_name, start_line, active=self._is_active())

        return PreprocessedSource(
            text=output.text,
            source_map=output.line_map,
            defines=self.defines,
            scoped_enums=self.scoped_enums,
            problems=self._problems,
            comments=self._comments,
        )

    def _apply_default_compiler_defines(self) -> None:
        if not self.options.use_defines:
            return
        if 'FPC' in self.defines:
            return
        if self.options.compiler_version >= 20.0:
            self.defines.add('CONDITIONALEXPRESSIONS')
            self.defines.add('UNICODE')
            version_define = int(round(self.options.compiler_version * 10))
            self.defines.add(f'VER{version_define}')

    def _emit_text(
        self,
        output: _OutputBuffer,
        text: str,
        file_name: str,
        line: int,
        *,
        active: bool,
    ) -> None:
        current_line = line
        for ch in text:
            if ch == '\n':
                output.append_char('\n', file_name, current_line)
                current_line += 1
            else:
                if active:
                    output.append_char(ch, file_name, current_line)
                else:
                    output.append_char(' ', file_name, current_line)

    def _is_active(self) -> bool:
        if not self._conditional_stack:
            return True
        return self._conditional_stack[-1].current_active

    def _handle_directive(
        self,
        directive_raw: str,
        directive_content: str,
        ctx: _FileContext,
        start_line: int,
        contexts: list[_FileContext],
        include_stack: list[str],
        output: _OutputBuffer,
    ) -> None:
        name, param = self._parse_directive(directive_content)
        replacement = self._replace_with_spaces(directive_raw)

        if not name:
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'IFDEF', 'IFNDEF', 'IF', 'IFOPT'}:
            condition = False
            if self._is_active():
                if name == 'IFDEF':
                    condition = self._is_defined(param)
                elif name == 'IFNDEF':
                    condition = not self._is_defined(param)
                elif name == 'IF':
                    condition = self._evaluate_conditional_expression(param)
                elif name == 'IFOPT':
                    condition = self._evaluate_ifopt(param)
            frame = _ConditionalFrame(
                parent_active=self._is_active(),
                branch_taken=condition,
                current_active=self._is_active() and condition,
            )
            self._conditional_stack.append(frame)
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'ELSEIF', 'ELSE', 'ENDIF', 'IFEND'}:
            if not self._conditional_stack:
                self._problems.append(
                    PreprocessorProblem(
                        kind='directive',
                        message=f'unexpected {name}',
                        file_name=ctx.file_name,
                        line=start_line,
                        col=ctx.col,
                    )
                )
                self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
                return

            frame = self._conditional_stack[-1]
            if name == 'ELSEIF':
                if not frame.parent_active:
                    frame.current_active = False
                elif frame.branch_taken:
                    frame.current_active = False
                else:
                    condition = self._evaluate_conditional_expression(param)
                    frame.current_active = frame.parent_active and condition
                    if condition:
                        frame.branch_taken = True
            elif name == 'ELSE':
                if not frame.parent_active:
                    frame.current_active = False
                else:
                    frame.current_active = frame.parent_active and not frame.branch_taken
                frame.branch_taken = True
            else:
                self._conditional_stack.pop()
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'DEFINE', 'UNDEF'}:
            if self._is_active() and self.options.use_defines:
                for define_name in self._parse_define_list(param):
                    if name == 'DEFINE':
                        self.defines.add(define_name)
                    else:
                        self.defines.discard(define_name)
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'SCOPEDENUMS'}:
            if self._is_active():
                self.scoped_enums = self._parse_on_off(param)
                self._set_option('SCOPEDENUMS', 'ON' if self.scoped_enums else 'OFF')
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'PUSHOPT', 'POPOPT'}:
            if self._is_active():
                if name == 'PUSHOPT':
                    self._option_stack.append((dict(self._option_values), self.scoped_enums))
                else:
                    if self._option_stack:
                        self._option_values, self.scoped_enums = self._option_stack.pop()
                    else:
                        self._problems.append(
                            PreprocessorProblem(
                                kind='directive',
                                message='POPOPT without matching PUSHOPT',
                                file_name=ctx.file_name,
                                line=start_line,
                                col=ctx.col,
                            )
                        )
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name == 'OPT':
            if self._is_active():
                self._apply_opt_directive(param)
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if name in {'I', 'INCLUDE'}:
            if self._is_active():
                include_name = self._extract_include_name(param)
                if include_name:
                    resolved = self.include_loader(ctx.file_name, include_name)
                    if resolved is None:
                        self._problems.append(
                            PreprocessorProblem(
                                kind='include',
                                message=f'include not found: {include_name}',
                                file_name=ctx.file_name,
                                line=start_line,
                                col=ctx.col,
                            )
                        )
                    else:
                        content, resolved_path = resolved
                        if resolved_path in include_stack:
                            self._problems.append(
                                PreprocessorProblem(
                                    kind='include',
                                    message=f'include cycle detected: {resolved_path}',
                                    file_name=ctx.file_name,
                                    line=start_line,
                                    col=ctx.col,
                                )
                            )
                        else:
                            include_stack.append(resolved_path)
                            ctx.pending_output = replacement
                            ctx.pending_line = start_line
                            normalized = self._normalize_newlines(content)
                            if not normalized.endswith('\n'):
                                normalized += '\n'
                            contexts.append(_FileContext(text=normalized, file_name=resolved_path))
                            return
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        if self._is_active() and self._apply_named_option(name, param):
            self._emit_text(output, replacement, ctx.file_name, start_line, active=True)
            return

        self._emit_text(output, replacement, ctx.file_name, start_line, active=True)

    def _parse_directive(self, directive_text: str) -> tuple[str, str]:
        text = directive_text.strip()
        if not text:
            return ('', '')
        name = []
        idx = 0
        while idx < len(text) and (text[idx].isalpha() or text[idx] == '_'):
            name.append(text[idx])
            idx += 1
        if not name:
            return ('', '')
        param = text[idx:].strip()
        return (''.join(name).upper(), param)

    def _parse_define_list(self, param: str) -> list[str]:
        if not param:
            return []
        cleaned = param.replace(',', ' ')
        names = [self._normalize_define(part) for part in cleaned.split() if part]
        return [n for n in names if n]

    def _evaluate_ifopt(self, param: str) -> bool:
        if not param:
            return False
        token = param.strip().upper()
        if not token:
            return False
        if token.endswith('+') and len(token) > 1:
            option_name = token[:-1].strip()
            return self._get_option(option_name) == 'ON'
        if token.endswith('-') and len(token) > 1:
            option_name = token[:-1].strip()
            return self._get_option(option_name) == 'OFF'
        if '=' in token:
            name, raw_state = token.split('=', 1)
            expected = self._normalize_option_state(raw_state)
            if not name.strip() or expected is None:
                return False
            return self._get_option(name) == expected
        parts = token.split()
        if len(parts) == 2:
            expected = self._normalize_option_state(parts[1])
            if expected is None:
                return False
            return self._get_option(parts[0]) == expected
        return False

    def _evaluate_conditional_expression(self, param: str) -> bool:
        text = param.strip().upper()
        if text.startswith('COMPILERVERSION'):
            return self._evaluate_version(text, 'COMPILERVERSION', self.options.compiler_version)
        if text.startswith('RTLVERSION'):
            return self._evaluate_version(text, 'RTLVERSION', self.options.rtl_version)
        if text.startswith('DEFINED(') or text.startswith('NOT DEFINED('):
            return self._evaluate_defined_chain(text)
        return False

    def _evaluate_version(self, text: str, label: str, value: float) -> bool:
        rest = text[len(label):].strip()
        if not rest:
            return False
        parts = rest.split()
        if len(parts) < 2:
            return False
        oper = parts[0]
        num = parts[1]
        try:
            right = float(num)
        except ValueError:
            return False
        if oper == '=':
            return value == right
        if oper == '<>':
            return value != right
        if oper == '<':
            return value < right
        if oper == '<=':
            return value <= right
        if oper == '>':
            return value > right
        if oper == '>=':
            return value >= right
        return False

    def _evaluate_defined_chain(self, text: str) -> bool:
        remaining = text
        result = True
        evaluation = None
        while remaining.startswith('DEFINED(') or remaining.startswith('NOT DEFINED('):
            if remaining.startswith('DEFINED('):
                define_name, remaining = self._consume_defined(remaining, 'DEFINED(')
                cond = self._is_defined(define_name)
            else:
                define_name, remaining = self._consume_defined(remaining, 'NOT DEFINED(')
                cond = not self._is_defined(define_name)
            if evaluation is None:
                result = cond
            elif evaluation == 'AND':
                result = result and cond
            elif evaluation == 'OR':
                result = result or cond
            remaining = remaining.lstrip()
            if remaining.startswith('AND '):
                evaluation = 'AND'
                remaining = remaining[4:]
            elif remaining.startswith('OR '):
                evaluation = 'OR'
                remaining = remaining[3:]
        return result

    def _consume_defined(self, text: str, prefix: str) -> tuple[str, str]:
        rest = text[len(prefix):]
        end = rest.find(')')
        if end == -1:
            return ('', '')
        name = rest[:end].strip()
        remaining = rest[end + 1:]
        return (name, remaining)

    def _parse_on_off(self, param: str) -> bool:
        token = param.strip().upper()
        if token in {'ON', '1', 'TRUE', '+'}:
            return True
        if token in {'OFF', '0', 'FALSE', '-'}:
            return False
        return self.scoped_enums

    def _is_defined(self, name: str) -> bool:
        return self._normalize_define(name) in self.defines

    def _normalize_define(self, name: str) -> str:
        return name.strip().upper()

    def _normalize_option_name(self, name: str) -> str:
        return name.strip().upper()

    def _normalize_option_state(self, raw_state: str) -> Optional[str]:
        token = raw_state.strip().upper()
        if token in {'+', 'ON', '1', 'TRUE'}:
            return 'ON'
        if token in {'-', 'OFF', '0', 'FALSE'}:
            return 'OFF'
        if token == 'AUTO':
            return 'AUTO'
        return None

    def _set_option(self, option_name: str, state: str) -> None:
        name = self._normalize_option_name(option_name)
        normalized = self._normalize_option_state(state)
        if not name or normalized is None:
            return
        self._option_values[name] = normalized
        if name == 'SCOPEDENUMS':
            self.scoped_enums = normalized == 'ON'

    def _get_option(self, option_name: str) -> str:
        return self._option_values.get(self._normalize_option_name(option_name), '')

    def _apply_named_option(self, name: str, param: str) -> bool:
        if not name:
            return False
        cleaned = param.strip()
        if not cleaned:
            return False
        if cleaned.startswith('='):
            cleaned = cleaned[1:].strip()
        state = self._normalize_option_state(cleaned)
        if state is not None:
            self._set_option(name, state)
            return True
        if len(cleaned) >= 2 and cleaned[0].isalpha() and cleaned[1] in {'+', '-'} and len(cleaned) == 2:
            # Handles forms like {$OPT R+} where R+ is passed as the param token.
            self._set_option(cleaned[0], cleaned[1])
            return True
        return False

    def _apply_opt_directive(self, param: str) -> None:
        if not param:
            return
        parts = [part for part in param.replace(',', ' ').split() if part]
        index = 0
        while index < len(parts):
            part = parts[index]
            if len(part) >= 2 and part[-1] in {'+', '-'}:
                self._set_option(part[:-1], part[-1])
                index += 1
                continue
            if '=' in part:
                name, raw_state = part.split('=', 1)
                self._set_option(name, raw_state)
                index += 1
                continue
            if index + 1 < len(parts):
                candidate_state = self._normalize_option_state(parts[index + 1])
                if candidate_state is not None:
                    self._set_option(part, candidate_state)
                    index += 2
                    continue
            index += 1

    def _consume_line_comment(self, ctx: _FileContext) -> tuple[str, str]:
        start = ctx.index
        while not ctx.eof() and ctx.peek() not in {'\n'}:
            ctx.advance(1)
        raw = ctx.text[start:ctx.index]
        return raw, raw[2:] if raw.startswith('//') else raw

    def _consume_brace_comment(self, ctx: _FileContext) -> tuple[str, str]:
        start = ctx.index
        ctx.advance(1)
        while not ctx.eof():
            if ctx.peek() == '}':
                ctx.advance(1)
                break
            ctx.advance(1)
        else:
            self._problems.append(
                PreprocessorProblem(
                    kind='comment',
                    message='unterminated comment',
                    file_name=ctx.file_name,
                    line=ctx.line,
                    col=ctx.col,
                )
            )
        raw = ctx.text[start:ctx.index]
        if raw.endswith('}'):
            return raw, raw[1:-1]
        return raw, raw[1:]

    def _consume_paren_comment(self, ctx: _FileContext) -> tuple[str, str]:
        start = ctx.index
        ctx.advance(2)
        while not ctx.eof():
            if ctx.peek() == '*' and ctx.peek(1) == ')':
                ctx.advance(2)
                break
            ctx.advance(1)
        else:
            self._problems.append(
                PreprocessorProblem(
                    kind='comment',
                    message='unterminated comment',
                    file_name=ctx.file_name,
                    line=ctx.line,
                    col=ctx.col,
                )
            )
        raw = ctx.text[start:ctx.index]
        if raw.endswith('*)'):
            return raw, raw[2:-2]
        return raw, raw[2:]

    def _consume_brace_directive(self, ctx: _FileContext) -> tuple[str, str]:
        start = ctx.index
        ctx.advance(2)
        while not ctx.eof():
            if ctx.peek() == '}':
                ctx.advance(1)
                break
            ctx.advance(1)
        else:
            self._problems.append(
                PreprocessorProblem(
                    kind='directive',
                    message='unterminated directive',
                    file_name=ctx.file_name,
                    line=ctx.line,
                    col=ctx.col,
                )
            )
        token = ctx.text[start:ctx.index]
        content = token[2:-1].strip() if token.endswith('}') else token[2:].strip()
        return token, content

    def _consume_paren_directive(self, ctx: _FileContext) -> tuple[str, str]:
        start = ctx.index
        ctx.advance(3)
        while not ctx.eof():
            if ctx.peek() == '*' and ctx.peek(1) == ')':
                ctx.advance(2)
                break
            ctx.advance(1)
        else:
            self._problems.append(
                PreprocessorProblem(
                    kind='directive',
                    message='unterminated directive',
                    file_name=ctx.file_name,
                    line=ctx.line,
                    col=ctx.col,
                )
            )
        token = ctx.text[start:ctx.index]
        if token.endswith('*)'):
            content = token[3:-2].strip()
        else:
            content = token[3:].strip()
        return token, content

    def _consume_string(self, ctx: _FileContext) -> str:
        start = ctx.index
        ctx.advance(1)
        while not ctx.eof():
            ch = ctx.peek()
            if ch == "'":
                if ctx.peek(1) == "'":
                    ctx.advance(2)
                    continue
                ctx.advance(1)
                break
            if ch == '\n':
                self._problems.append(
                    PreprocessorProblem(
                        kind='string',
                        message='unterminated string literal',
                        file_name=ctx.file_name,
                        line=ctx.line,
                        col=ctx.col,
                    )
                )
                break
            ctx.advance(1)
        return ctx.text[start:ctx.index]

    def _extract_include_name(self, param: str) -> str:
        text = param.strip()
        if not text:
            return ''
        if text[0] in {"'", '"'}:
            quote = text[0]
            end = text.find(quote, 1)
            if end == -1:
                return text[1:]
            return text[1:end]
        parts = text.split()
        return parts[0] if parts else ''

    def _replace_with_spaces(self, text: str) -> str:
        return ''.join('\n' if ch == '\n' else ' ' for ch in text)

    def _normalize_newlines(self, text: str) -> str:
        return text.replace('\ufeff', '').replace('\r\n', '\n').replace('\r', '\n')

    def _default_include_loader(self, parent_file: str, include_name: str) -> Optional[tuple[str, str]]:
        parent_path = Path(parent_file)
        include_path = Path(include_name.replace('\\', '/'))
        search_paths = [parent_path.parent] + self.include_paths
        for base in search_paths:
            candidate = (base / include_path).resolve()
            if candidate.exists():
                return (read_source_text(candidate), str(candidate))
        return None

    def _record_comment(
        self,
        kind: str,
        text: str,
        file_name: str,
        start_line: int,
        start_col: int,
        ctx: _FileContext,
    ) -> None:
        end_line = ctx.line
        end_col = max(ctx.col - 1, 1)
        self._comments.append(
            CommentInfo(
                kind=kind,
                text=text,
                file_name=file_name,
                line=start_line,
                col=start_col,
                end_line=end_line,
                end_col=end_col,
            )
        )
