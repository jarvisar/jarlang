"""Converts source code into a list of tokens."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError
from .source import Source, Span
from .tokens import KEYWORDS, T, Token


@dataclass(slots=True)
class Interp:
    """A {expression} inside a string."""

    start: int  # position of the expression in the source
    end: int
    spec: str | None = None  # format after ':', such as ".2f"


@dataclass(slots=True)
class Comment:
    span: Span
    text: str  # without the leading '#'


# Longest operators first so they match before shorter ones
_OPERATORS: list[tuple[str, T]] = [
    ("..<", T.DOTDOTLT),
    ("//=", T.DSLASH_ASSIGN),
    ("**=", T.CARET_ASSIGN),
    ("**", T.CARET),
    ("//", T.DSLASH),
    ("==", T.EQ),
    ("!=", T.NE),
    ("<=", T.LE),
    (">=", T.GE),
    ("&&", T.AMPAMP),
    ("||", T.PIPEPIPE),
    ("|>", T.PIPE_GT),
    ("..", T.DOTDOT),
    ("=>", T.FAT_ARROW),
    ("->", T.ARROW),
    ("+=", T.PLUS_ASSIGN),
    ("-=", T.MINUS_ASSIGN),
    ("*=", T.STAR_ASSIGN),
    ("/=", T.SLASH_ASSIGN),
    ("%=", T.PERCENT_ASSIGN),
    ("^=", T.CARET_ASSIGN),
    ("+", T.PLUS),
    ("-", T.MINUS),
    ("*", T.STAR),
    ("/", T.SLASH),
    ("%", T.PERCENT),
    ("^", T.CARET),
    ("!", T.BANG),
    ("<", T.LT),
    (">", T.GT),
    ("=", T.ASSIGN),
    (".", T.DOT),
    (":", T.COLON),
    (",", T.COMMA),
    (";", T.SEMI),
    ("(", T.LPAREN),
    (")", T.RPAREN),
    ("[", T.LBRACKET),
    ("]", T.RBRACKET),
    ("{", T.LBRACE),
    ("}", T.RBRACE),
    # Unicode math operators
    ("×", T.STAR),
    ("·", T.STAR),
    ("÷", T.SLASH),
    ("≤", T.LE),
    ("≥", T.GE),
    ("≠", T.NE),
    ("√", T.SQRT),
    ("−", T.MINUS),  # U+2212 MINUS SIGN
]

_OP_FIRST = {op[0] for op, _ in _OPERATORS}

# A line that ends with one of these continues on the next line
_NO_NEWLINE_AFTER = frozenset({
    T.NEWLINE, T.SEMI, T.LBRACE, T.COMMA, T.COLON, T.DOT,
    T.PLUS, T.MINUS, T.STAR, T.SLASH, T.DSLASH, T.PERCENT, T.CARET, T.SQRT,
    T.EQ, T.NE, T.LT, T.LE, T.GT, T.GE, T.AMPAMP, T.PIPEPIPE, T.AND, T.OR, T.NOT,
    T.ASSIGN, T.PLUS_ASSIGN, T.MINUS_ASSIGN, T.STAR_ASSIGN, T.SLASH_ASSIGN,
    T.DSLASH_ASSIGN, T.PERCENT_ASSIGN, T.CARET_ASSIGN,
    T.PIPE_GT, T.DOTDOT, T.DOTDOTLT, T.FAT_ARROW, T.ARROW,
})

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'",
            "{": "{", "}": "}", "e": "\x1b", "a": "\a", "b": "\b"}


def _is_digit(ch: str) -> bool:
    # str.isdigit() is also true for characters like ² and ١, which int() can't read
    return "0" <= ch <= "9"


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_ident_char(ch: str) -> bool:
    # Same rules as Python, so x² is x followed by ² and not one name
    return ch == "_" or ch.isalnum() and (ch.isascii() or ("_" + ch).isidentifier())


class Lexer:
    def __init__(self, source: Source, start: int = 0, end: int | None = None) -> None:
        self.source = source
        self.text = source.text
        self.pos = start
        self.end = len(self.text) if end is None else end
        self.tokens: list[Token] = []
        self.comments: list[Comment] = []
        self._brackets: list[str] = []

    # Helpers
    def _peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        return self.text[i] if i < self.end else ""

    def _span(self, start: int) -> Span:
        return Span(self.source, start, self.pos)

    def _error(self, message: str, start: int, end: int | None = None, label: str = "",
               helps: tuple[str, ...] = ()) -> LexError:
        span = Span(self.source, start, end if end is not None else start + 1)
        return LexError(message, span, label, helps=helps)

    def _emit(self, kind: T, value, start: int, spaced: bool) -> None:
        self.tokens.append(Token(kind, value, Span(self.source, start, self.pos), spaced))

    # Main loop
    def tokenize(self) -> list[Token]:
        spaced = True
        while self.pos < self.end:
            ch = self.text[self.pos]

            if ch == "\n":
                self.pos += 1
                self._newline()
                spaced = True
                continue
            if ch in " \t\r\f\v":
                self.pos += 1
                spaced = True
                continue
            if ch == "\\" and self._peek(1) in ("\n", "\r"):
                # A backslash at the end of a line continues it
                self.pos += 1
                while self._peek() in ("\r", "\n"):
                    if self._peek() == "\n":
                        self.pos += 1
                        break
                    self.pos += 1
                spaced = True
                continue
            if ch == "#":
                start = self.pos
                while self.pos < self.end and self.text[self.pos] != "\n":
                    self.pos += 1
                self.comments.append(Comment(self._span(start), self.text[start + 1:self.pos].rstrip("\r")))
                spaced = True
                continue

            start = self.pos
            if _is_digit(ch):
                self._number(spaced)
            elif ch == '"' or ch == "'":
                self._string(ch, spaced)
            elif _is_ident_start(ch):
                while self.pos < self.end and _is_ident_char(self.text[self.pos]):
                    self.pos += 1
                word = self.text[start:self.pos]
                kind = KEYWORDS.get(word)
                if kind is not None:
                    self._emit(kind, word, start, spaced)
                else:
                    self._emit(T.IDENT, word, start, spaced)
            elif ch == "∞":
                self.pos += 1
                self._emit(T.IDENT, "inf", start, spaced)
            elif ch in _OP_FIRST:
                self._operator(spaced)
            else:
                raise self._error(f"unexpected character {ch!r}", start, label="not valid here",
                                  helps=self._char_help(ch))
            spaced = False

        self._newline()
        self.tokens.append(Token(T.EOF, None, Span(self.source, self.end, self.end), True))
        return self.tokens

    @staticmethod
    def _char_help(ch: str) -> tuple[str, ...]:
        if ch == "$":
            return ("string interpolation uses `{expr}`, e.g. \"x = {x}\"",)
        if ch == "?":
            return ("use `if cond { a } else { b }` as an expression",)
        if ch == "&":
            return ("logical and is written `and` or `&&`",)
        if ch == "|":
            return ("logical or is written `or` or `||`, and the pipeline operator is `|>`",)
        if ch == "@":
            return ("JarLang has no decorators or `@` operator",)
        if ch in "‘’“”":
            return ("use straight quotes (\" or ') for strings",)
        if ch in "⁰¹²³⁴⁵⁶⁷⁸⁹":
            return ("write powers with `^`, e.g. x^2",)
        return ()

    def _newline(self) -> None:
        if self._brackets and self._brackets[-1] in "([":
            return
        if not self.tokens or self.tokens[-1].kind in _NO_NEWLINE_AFTER:
            return
        if self._continues_on_next_line():
            return
        tok = Token(T.NEWLINE, "\n", Span(self.source, max(self.pos - 1, 0), self.pos), True)
        self.tokens.append(tok)

    def _continues_on_next_line(self) -> bool:
        """Check if the next non-blank line starts with |> or .name."""
        i = self.pos
        text, end = self.text, self.end
        while i < end:
            ch = text[i]
            if ch in " \t\r\n\f\v":
                i += 1
            elif ch == "#":
                while i < end and text[i] != "\n":
                    i += 1
            else:
                break
        if text.startswith("|>", i):
            return True
        if i < end and text[i] == "." and i + 1 < end and _is_ident_start(text[i + 1]):
            return True
        return False

    # Numbers
    def _digits(self, allowed: str) -> str:
        out = []
        while self.pos < self.end:
            ch = self.text[self.pos]
            if ch in allowed:
                out.append(ch)
            elif ch == "_" and self._peek(1) in allowed and self._peek(1) != "":
                pass
            else:
                break
            self.pos += 1
        return "".join(out)

    def _number(self, spaced: bool) -> None:
        start = self.pos
        text = self.text
        if text[start] == "0" and self._peek(1) in ("x", "X", "b", "B", "o", "O"):
            base_ch = self._peek(1).lower()
            base, allowed = {"x": (16, "0123456789abcdefABCDEF"), "b": (2, "01"), "o": (8, "01234567")}[base_ch]
            self.pos += 2
            digits = self._digits(allowed)
            if not digits:
                raise self._error(f"expected digits after `0{base_ch}`", start, self.pos)
            self._emit(T.INT, int(digits, base), start, spaced)
            return

        digits = self._digits("0123456789")
        is_float = False
        frac = ""
        if self._peek() == "." and _is_digit(self._peek(1)):
            is_float = True
            self.pos += 1
            frac = self._digits("0123456789")
        exp = ""
        if self._peek() in ("e", "E"):
            nxt = self._peek(1)
            if _is_digit(nxt) or (nxt in ("+", "-") and _is_digit(self._peek(2))):
                is_float = True
                self.pos += 1
                sign = ""
                if self._peek() in "+-":
                    sign = self._peek()
                    self.pos += 1
                exp = sign + self._digits("0123456789")
        if is_float:
            literal = digits + ("." + frac if frac else "") + ("e" + exp if exp else "")
            self._emit(T.FLOAT, float(literal), start, spaced)
        else:
            self._emit(T.INT, int(digits), start, spaced)

    # Strings
    def _string(self, quote: str, spaced: bool) -> None:
        start = self.pos
        self.pos += 1
        parts: list[str | Interp] = []
        buf: list[str] = []
        interpolate = quote == '"'
        while True:
            if self.pos >= self.end:
                raise self._error("unterminated string literal", start, self.pos,
                                  label="string starts here",
                                  helps=(f"add a closing {quote} to end the string",))
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                break
            if ch == "\\":
                self.pos += 1
                buf.append(self._escape(start))
                continue
            if interpolate and ch == "{":
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(self._interpolation())
                continue
            if interpolate and ch == "}":
                raise self._error("unmatched `}` in string", self.pos,
                                  helps=("write `\\}` for a literal brace",))
            buf.append(ch)
            self.pos += 1
        if buf or not parts:
            parts.append("".join(buf))
        self._emit(T.STRING, parts, start, spaced)

    def _escape(self, string_start: int) -> str:
        ch = self._peek()
        if ch == "":
            raise self._error("unterminated string literal", string_start, self.pos)
        if ch in _ESCAPES:
            self.pos += 1
            return _ESCAPES[ch]
        if ch == "\n":
            self.pos += 1
            return ""
        if ch == "\r" and self._peek(1) == "\n":
            self.pos += 2
            return ""
        if ch == "u":
            self.pos += 1
            esc_start = self.pos - 2
            if self._peek() == "{":
                close = self.text.find("}", self.pos, self.end)
                hex_digits = self.text[self.pos + 1:close] if close != -1 else ""
                self.pos = close + 1 if close != -1 else self.pos
            else:
                hex_digits = self.text[self.pos:self.pos + 4]
                self.pos += 4
            try:
                return chr(int(hex_digits, 16))
            except ValueError:
                raise self._error("invalid unicode escape", esc_start, self.pos,
                                  helps=("use `\\u{1F600}` or `\\u00e9`",)) from None
        raise self._error(f"unknown escape sequence `\\{ch}`", self.pos - 1, self.pos + 1,
                          helps=("valid escapes: \\n \\t \\r \\\\ \\\" \\' \\{ \\} \\u{XXXX}",))

    def _interpolation(self) -> Interp:
        """Read {expr} or {expr:spec} in a string, starting at the {."""
        open_pos = self.pos
        self.pos += 1
        expr_start = self.pos
        opened: list[str] = []  # brackets opened inside the expression
        spec_start = None
        text = self.text
        while True:
            if self.pos >= self.end:
                raise self._error("unterminated interpolation in string", open_pos,
                                  helps=("close it with `}`, or write `\\{` for a literal brace",))
            ch = text[self.pos]
            if ch in "\"'":
                self._skip_nested_string(ch)
                continue
            if ch in "([{":
                opened.append(ch)
            elif ch in ")]":
                # Only close a matching bracket, so the parser reports a stray `)` instead of a confusing lexer error
                if opened and opened[-1] == ("(" if ch == ")" else "["):
                    opened.pop()
            elif ch == "}":
                # A `}` with a `(` or `[` still open ends the interpolation, so the parser reports the missing `)`
                if not opened or opened[-1] != "{":
                    break
                opened.pop()
            elif ch == ":" and not opened and spec_start is None:
                spec_start = self.pos
            elif ch == "\n":
                raise self._error("unterminated interpolation in string", open_pos,
                                  helps=("close it with `}`, or write `\\{` for a literal brace",))
            self.pos += 1
        expr_end = spec_start if spec_start is not None else self.pos
        spec = text[spec_start + 1:self.pos] if spec_start is not None else None
        if not text[expr_start:expr_end].strip():
            raise self._error("empty interpolation `{}` in string", open_pos, self.pos + 1,
                              helps=("write `\\{\\}` for literal braces",))
        self.pos += 1  # skip the closing brace
        return Interp(expr_start, expr_end, spec)

    def _skip_nested_string(self, quote: str) -> None:
        start = self.pos
        self.pos += 1
        while self.pos < self.end:
            ch = self.text[self.pos]
            if ch == "\\":
                self.pos += 2
                continue
            if ch == quote:
                self.pos += 1
                return
            if quote == '"' and ch == "{":
                depth = 1
                self.pos += 1
                while self.pos < self.end and depth:
                    c = self.text[self.pos]
                    if c in "\"'":
                        self._skip_nested_string(c)
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                    self.pos += 1
                continue
            self.pos += 1
        raise self._error("unterminated string literal", start)

    # Operators
    def _operator(self, spaced: bool) -> None:
        start = self.pos
        text = self.text
        for op, kind in _OPERATORS:
            if text.startswith(op, self.pos) and self.pos + len(op) <= self.end:
                self.pos += len(op)
                if kind in (T.LPAREN, T.LBRACKET, T.LBRACE):
                    self._brackets.append(op)
                elif kind in (T.RPAREN, T.RBRACKET, T.RBRACE):
                    if self._brackets:
                        self._brackets.pop()
                self._emit(kind, op, start, spaced)
                return
        ch = text[start]
        raise self._error(f"unexpected character {ch!r}", start, helps=self._char_help(ch))


def tokenize(source: Source | str, name: str = "<input>") -> list[Token]:
    if isinstance(source, str):
        source = Source(source, name)
    return Lexer(source).tokenize()
