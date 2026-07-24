"""Syntax highlighting for the terminal, using the lexer."""

from __future__ import annotations

from .builtins import BUILTINS, CONSTANTS
from .errors import JarLangError
from .lexer import Lexer
from .source import Source
from .term import style
from .tokens import KEYWORDS, T

# Colors for each kind of token
THEME = {
    "keyword": "bold magenta",
    "constant": "bright_cyan",
    "number": "bright_cyan",
    "string": "green",
    "comment": "gray italic",
    "builtin": "bright_blue",
    "function": "bright_yellow",
    "operator": "yellow",
    "punct": "",
    "name": "",
}

_LITERAL_KEYWORDS = {T.TRUE, T.FALSE, T.NIL}
_KEYWORD_KINDS = set(KEYWORDS.values())
_OPERATOR_KINDS = {
    T.PLUS, T.MINUS, T.STAR, T.SLASH, T.DSLASH, T.PERCENT, T.CARET, T.BANG, T.SQRT,
    T.EQ, T.NE, T.LT, T.LE, T.GT, T.GE, T.AMPAMP, T.PIPEPIPE, T.ASSIGN, T.PLUS_ASSIGN,
    T.MINUS_ASSIGN, T.STAR_ASSIGN, T.SLASH_ASSIGN, T.DSLASH_ASSIGN, T.PERCENT_ASSIGN,
    T.CARET_ASSIGN, T.PIPE_GT, T.DOTDOT, T.DOTDOTLT, T.FAT_ARROW, T.ARROW,
}


def classify(text: str) -> list[tuple[int, int, str]]:
    """Return a (start, end, category) range for each part of the text to color."""
    source = Source(text)
    lexer = Lexer(source)
    try:
        tokens = lexer.tokenize()
    except JarLangError:
        tokens = lexer.tokens
    ranges: list[tuple[int, int, str]] = []
    for i, tok in enumerate(tokens):
        kind = tok.kind
        if kind in (T.NEWLINE, T.EOF):
            continue
        s, e = tok.span.start, tok.span.end
        if kind in _LITERAL_KEYWORDS:
            cat = "constant"
        elif kind in _KEYWORD_KINDS:
            cat = "keyword"
        elif kind in (T.INT, T.FLOAT):
            cat = "number"
        elif kind is T.STRING:
            cat = "string"
        elif kind is T.IDENT:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            prev = tokens[i - 1] if i > 0 else None
            if tok.value in CONSTANTS:
                cat = "constant"
            elif prev is not None and prev.kind is T.FN:
                cat = "function"
            elif nxt is not None and nxt.kind is T.LPAREN and not nxt.spaced:
                cat = "builtin" if tok.value in BUILTINS else "function"
            else:
                cat = "name"
        elif kind in _OPERATOR_KINDS:
            cat = "operator"
        else:
            cat = "punct"
        ranges.append((s, e, cat))
    for c in lexer.comments:
        ranges.append((c.span.start, c.span.end, "comment"))
    ranges.sort()
    return ranges


def highlight(text: str, enabled: bool = True) -> str:
    """Return the text with terminal colors added."""
    if not enabled:
        return text
    out: list[str] = []
    pos = 0
    for start, end, cat in classify(text):
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append(style(text[start:end], THEME.get(cat, ""), True))
        pos = end
    out.append(text[pos:])
    return "".join(out)
