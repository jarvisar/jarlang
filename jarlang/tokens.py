"""Token kinds and the Token record produced by the lexer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .source import Span


class T(str, Enum):
    """Token kinds. The value is the name shown in error messages."""

    # Literals and names
    INT = "integer"
    FLOAT = "number"
    STRING = "string"
    IDENT = "identifier"

    # Keywords
    LET = "`let`"
    CONST = "`const`"
    FN = "`fn`"
    RETURN = "`return`"
    IF = "`if`"
    ELIF = "`elif`"
    ELSE = "`else`"
    WHILE = "`while`"
    FOR = "`for`"
    IN = "`in`"
    BREAK = "`break`"
    CONTINUE = "`continue`"
    TRUE = "`true`"
    FALSE = "`false`"
    NIL = "`nil`"
    AND = "`and`"
    OR = "`or`"
    NOT = "`not`"
    TRY = "`try`"
    CATCH = "`catch`"
    THROW = "`throw`"
    IMPORT = "`import`"

    # Arithmetic
    PLUS = "`+`"
    MINUS = "`-`"
    STAR = "`*`"
    SLASH = "`/`"
    DSLASH = "`//`"
    PERCENT = "`%`"
    CARET = "`^`"
    BANG = "`!`"
    SQRT = "`√`"

    # Comparison and logic
    EQ = "`==`"
    NE = "`!=`"
    LT = "`<`"
    LE = "`<=`"
    GT = "`>`"
    GE = "`>=`"
    AMPAMP = "`&&`"
    PIPEPIPE = "`||`"

    # Assignment
    ASSIGN = "`=`"
    PLUS_ASSIGN = "`+=`"
    MINUS_ASSIGN = "`-=`"
    STAR_ASSIGN = "`*=`"
    SLASH_ASSIGN = "`/=`"
    DSLASH_ASSIGN = "`//=`"
    PERCENT_ASSIGN = "`%=`"
    CARET_ASSIGN = "`^=`"

    # Other operators and punctuation
    PIPE_GT = "`|>`"
    DOTDOT = "`..`"
    DOTDOTLT = "`..<`"
    FAT_ARROW = "`=>`"
    ARROW = "`->`"
    DOT = "`.`"
    COLON = "`:`"
    COMMA = "`,`"
    SEMI = "`;`"
    LPAREN = "`(`"
    RPAREN = "`)`"
    LBRACKET = "`[`"
    RBRACKET = "`]`"
    LBRACE = "`{`"
    RBRACE = "`}`"
    NEWLINE = "end of line"
    EOF = "end of input"


KEYWORDS: dict[str, T] = {
    "let": T.LET,
    "const": T.CONST,
    "fn": T.FN,
    "return": T.RETURN,
    "if": T.IF,
    "elif": T.ELIF,
    "else": T.ELSE,
    "while": T.WHILE,
    "for": T.FOR,
    "in": T.IN,
    "break": T.BREAK,
    "continue": T.CONTINUE,
    "true": T.TRUE,
    "false": T.FALSE,
    "nil": T.NIL,
    "and": T.AND,
    "or": T.OR,
    "not": T.NOT,
    "try": T.TRY,
    "catch": T.CATCH,
    "throw": T.THROW,
    "import": T.IMPORT,
}

COMPOUND_ASSIGN: dict[T, T] = {
    T.PLUS_ASSIGN: T.PLUS,
    T.MINUS_ASSIGN: T.MINUS,
    T.STAR_ASSIGN: T.STAR,
    T.SLASH_ASSIGN: T.SLASH,
    T.DSLASH_ASSIGN: T.DSLASH,
    T.PERCENT_ASSIGN: T.PERCENT,
    T.CARET_ASSIGN: T.CARET,
}


@dataclass(slots=True)
class Token:
    kind: T
    value: Any
    span: Span
    # True if a space or new line comes before the token (2x is 2 * x, but 2 x is not)
    spaced: bool = True

    @property
    def text(self) -> str:
        return self.span.text

    def __repr__(self) -> str:
        if self.kind in (T.INT, T.FLOAT, T.IDENT):
            return f"{self.kind.name}({self.value!r})"
        if self.kind is T.STRING:
            return f"STRING({self.text})"
        return self.kind.name
