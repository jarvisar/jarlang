"""Formats JarLang code (jarlang fmt)."""

from __future__ import annotations

import bisect
from typing import Callable, Sequence

from . import ast
from .errors import JarLangError
from .lexer import Comment, Lexer
from .parser import (
    BP_ADD, BP_AND, BP_COMPARE, BP_MUL, BP_NOT, BP_OR, BP_PIPE, BP_POSTFIX, BP_POWER,
    BP_RANGE, BP_UNARY, Parser,
)
from .source import Source, Span
from .tokens import KEYWORDS, T, Token

__all__ = ["format_source", "format_code", "INDENT", "MAX_WIDTH"]

INDENT = "  "
MAX_WIDTH = 100

_CLOSED = 1_000  # binding power of something nothing can split (atoms, closed forms)

_BINARY_BP = {"+": BP_ADD, "-": BP_ADD, "*": BP_MUL, "/": BP_MUL, "//": BP_MUL, "%": BP_MUL,
              "^": BP_POWER}
_LOGICAL_BP = {"and": BP_AND, "or": BP_OR}
_DECORATIVE = set("-=*~_+/<>.:|")


def format_code(text: str, name: str = "<input>") -> str:
    return format_source(Source(text, name))


def format_source(source: Source) -> str:
    """Return the formatted code (raises JarLangError if it doesn't parse)."""
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    program = Parser(list(tokens), source).parse_program()
    return _align_trailing_comments(_Printer(source, tokens, lexer.comments).program(program))


MAX_ALIGN_COLUMN = 100


def _align_trailing_comments(text: str) -> str:
    """Line up trailing comments on consecutive lines with the same indentation."""
    if "#" not in text:
        return text
    lexer = Lexer(Source(text))
    try:
        tokens = lexer.tokenize()
    except JarLangError:
        return text
    source = lexer.source
    code_end: dict[int, int] = {}  # line -> column just after its last code token
    for tok in tokens:
        if tok.kind in (T.NEWLINE, T.EOF):
            continue
        line, col = source.line_col(tok.span.end)
        if source.line_col(tok.span.start)[0] == line:
            code_end[line] = max(code_end.get(line, 0), col)
    trailing: dict[int, int] = {}  # line -> column of '#'
    for c in lexer.comments:
        line, col = source.line_col(c.span.start)
        if line in code_end and code_end[line] < col:
            trailing[line] = col
    lines = text.split("\n")

    def indent(i: int) -> int:
        return len(lines[i - 1]) - len(lines[i - 1].lstrip(" "))

    groups: list[list[int]] = []
    for line in sorted(trailing):
        if groups and groups[-1][-1] == line - 1 and indent(line) == indent(line - 1):
            groups[-1].append(line)
        else:
            groups.append([line])
    for group in groups:
        if len(group) < 2:
            continue
        target = max(code_end[ln] for ln in group) + 1  # 0-based column where '#' goes
        width = max(target + len(lines[ln - 1]) - trailing[ln] + 1 for ln in group)
        if width > MAX_ALIGN_COLUMN:
            continue
        for ln in group:
            row = lines[ln - 1]
            code = row[:code_end[ln] - 1].rstrip()
            comment = row[trailing[ln] - 1:]
            lines[ln - 1] = code + " " * (target - len(code)) + comment
    return "\n".join(lines)


# Helpers

def _is_ident(text: str) -> bool:
    return bool(text) and (text[0] == "_" or text[0].isalpha()) and \
        all(ch == "_" or ch.isalnum() for ch in text)


def _ident_prefix(text: str) -> str:
    if not text or not (text[0] == "_" or text[0].isalpha()):
        return ""
    i = 1
    while i < len(text) and (text[i] == "_" or text[i].isalnum()):
        i += 1
    return text[:i]


def _quote(value: str) -> str:
    """Quote a string as a JarLang string literal (only used as a fallback)."""
    out = []
    for ch in value:
        if ch in '\\"{}':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def comment_text(comment: Comment) -> str:
    """Format a comment (#foo becomes # foo)."""
    body = comment.text.rstrip()
    if not body or body[0] in " \t!#" or all(ch in _DECORATIVE for ch in body):
        return "#" + body
    return "# " + body


def _only_expr(block: ast.Block) -> ast.Expr:
    """The expression of a block that holds one expression statement."""
    stmt = block.stmts[0]
    assert isinstance(stmt, ast.ExprStmt)
    return stmt.expr


def _is_number(e: ast.Expr) -> bool:
    return isinstance(e, ast.Literal) and not isinstance(e.value, bool) and \
        isinstance(e.value, (int, float))


class _Printer:
    def __init__(self, source: Source, tokens: list[Token], comments: list[Comment]) -> None:
        self.source = source
        self.text = source.text
        self.tokens = tokens
        self.tok_starts = [t.span.start for t in tokens]
        self.comments = sorted(comments, key=lambda c: c.span.start)
        self.c_starts = [c.span.start for c in self.comments]
        self.used = [False] * len(self.comments)
        self.journal: list[int] = []  # indexes of used comments, in order (for rollback)
        self.blank_lines = [n for n in range(1, source.line_count + 1)
                            if not source.line_text(n).strip()]

    # Positions
    def line(self, pos: int) -> int:
        return self.source.line_col(pos)[0]

    def last_line(self, start: int, end: int) -> int:
        """Line of the last character in [start, end)."""
        return self.line(max(start, end - 1))

    def span_last_line(self, span: Span) -> int:
        return self.last_line(span.start, span.end)

    def blank_between(self, line_a: int, line_b: int) -> bool:
        i = bisect.bisect_right(self.blank_lines, line_a)
        return i < len(self.blank_lines) and self.blank_lines[i] < line_b

    def token_from(self, pos: int, kind: T) -> Token | None:
        """First token of this kind at or after pos."""
        i = bisect.bisect_left(self.tok_starts, pos)
        while i < len(self.tokens):
            if self.tokens[i].kind is kind:
                return self.tokens[i]
            i += 1
        return None

    def token_before(self, pos: int, kind: T) -> Token | None:
        """Last token of this kind before pos."""
        i = bisect.bisect_left(self.tok_starts, pos) - 1
        while i >= 0:
            if self.tokens[i].kind is kind:
                return self.tokens[i]
            i -= 1
        return None

    # Comments
    def take(self, lo: int, hi: int) -> list[Comment]:
        """Use up and return every unused comment in [lo, hi)."""
        out = []
        i = bisect.bisect_left(self.c_starts, lo)
        while i < len(self.comments) and self.c_starts[i] < hi:
            if not self.used[i]:
                self.used[i] = True
                self.journal.append(i)
                out.append(self.comments[i])
            i += 1
        return out

    def has_comments(self, lo: int, hi: int) -> bool:
        i = bisect.bisect_left(self.c_starts, lo)
        while i < len(self.comments) and self.c_starts[i] < hi:
            if not self.used[i]:
                return True
            i += 1
        return False

    def take_trailing(self, lo: int, hi: int, line: int) -> Comment | None:
        """Use up the first unused comment in [lo, hi) if it is on this line."""
        i = bisect.bisect_left(self.c_starts, lo)
        while i < len(self.comments) and self.c_starts[i] < hi:
            if not self.used[i]:
                if self.line(self.c_starts[i]) == line:
                    self.used[i] = True
                    self.journal.append(i)
                    return self.comments[i]
                return None
            i += 1
        return None

    def mark(self) -> int:
        """Save a checkpoint before trying a layout (see rollback)."""
        return len(self.journal)

    def rollback(self, mark: int) -> None:
        """Give back the comments taken since mark, when a layout is dropped."""
        while len(self.journal) > mark:
            self.used[self.journal.pop()] = False

    @staticmethod
    def with_comment(text: str, comment: Comment | None) -> str:
        return text + "  " + comment_text(comment) if comment is not None else text

    # Program and statements
    def program(self, prog: ast.Program) -> str:
        chunks = self.stmts(prog.stmts, 0, 0, len(self.text) + 1)
        # Never drop a comment (this shouldn't happen)
        chunks.extend(comment_text(c) for c in self.take(0, len(self.text) + 1))
        return "\n".join(chunks) + "\n" if chunks else ""

    def stmts(self, stmts: Sequence[ast.Stmt], indent: int, lo: int, hi: int) -> list[str]:
        """Format statements and the comments in [lo, hi) as lines (blank lines are empty strings)."""
        pad = INDENT * indent
        out: list[str] = []
        prev_line: int | None = None

        def emit(text: str, first_line: int, last_line: int) -> None:
            nonlocal prev_line
            if prev_line is not None and self.blank_between(prev_line, first_line):
                out.append("")
            out.append(text)
            prev_line = last_line

        cursor = lo
        for i, stmt in enumerate(stmts):
            start, end = stmt.span.start, stmt.span.end
            for c in self.take(cursor, start):
                ln = self.line(c.span.start)
                emit(pad + comment_text(c), ln, ln)
            body = pad + self.stmt(stmt, indent)
            orphans = self.take(start, end)
            nxt = stmts[i + 1].span.start if i + 1 < len(stmts) else hi
            body = self.with_comment(body, self.take_trailing(end, nxt, self.last_line(start, end)))
            if orphans:
                body = "\n".join(pad + comment_text(c) for c in orphans) + "\n" + body
            emit(body, self.line(start), self.last_line(start, end))
            cursor = end
        for c in self.take(cursor, hi):
            ln = self.line(c.span.start)
            emit(pad + comment_text(c), ln, ln)
        return out

    def block(self, block: ast.Block, indent: int, lo: int | None = None) -> str:
        """Format a block with the closing brace at indent (comments from lo on belong to it)."""
        brace = block.span.start
        lo = brace if lo is None else lo
        first = block.stmts[0].span.start if block.stmts else block.span.end
        header = self.take_trailing(brace + 1, first, self.line(brace))
        items = self.stmts(block.stmts, indent + 1, lo, block.span.end)
        if not items and header is None:
            return "{}"
        head = self.with_comment("{", header)
        body = "\n".join(items) + "\n" if items else ""
        return head + "\n" + body + INDENT * indent + "}"

    def simple_stmt(self, s: ast.Stmt) -> bool:
        if isinstance(s, ast.ExprStmt):
            return not isinstance(s.expr, ast.IfExpr)
        return isinstance(s, (ast.Assign, ast.Let, ast.Return, ast.Break, ast.Continue,
                              ast.Throw, ast.Import))

    # The caller must check that there are no comments inside the block
    def inline_block(self, block: ast.Block, indent: int) -> str | None:
        """{ stmt } or {} for a block with at most one simple statement, else None."""
        if not block.stmts:
            return "{}"
        if len(block.stmts) > 1 or not self.simple_stmt(block.stmts[0]):
            return None
        inner = self.stmt(block.stmts[0], indent)
        return None if "\n" in inner else "{ " + inner + " }"

    def on_one_line(self, span: Span) -> bool:
        return self.line(span.start) == self.span_last_line(span)

    def stmt(self, s: ast.Stmt, indent: int) -> str:
        """Format a statement (the first line has no indentation)."""
        if isinstance(s, ast.ExprStmt):
            if isinstance(s.expr, ast.IfExpr):
                return self.if_stmt(s.expr, indent)
            return self.expr(s.expr, 0, 0, indent)
        if isinstance(s, ast.Assign):
            return self.assign(s, indent)
        if isinstance(s, ast.Let):
            out = ("const " if s.const else "let ") + s.target.name
            if s.type is not None:
                out += f": {s.type}"
            if s.value is not None:
                out += " = " + self.expr(s.value, 0, 0, indent)
            return out
        if isinstance(s, ast.FnDecl):
            return self.fn_decl(s, indent)
        if isinstance(s, ast.While):
            return f"while {self.expr(s.cond, 0, 0, indent)} {self.block(s.body, indent)}"
        if isinstance(s, ast.For):
            targets = ", ".join(t.name for t in s.targets)
            return f"for {targets} in {self.expr(s.iterable, 0, 0, indent)} {self.block(s.body, indent)}"
        if isinstance(s, ast.Return):
            return "return" if s.value is None else "return " + self.expr(s.value, 0, 0, indent)
        if isinstance(s, ast.Break):
            return "break"
        if isinstance(s, ast.Continue):
            return "continue"
        if isinstance(s, ast.Throw):
            return "throw " + self.expr(s.value, 0, 0, indent)
        if isinstance(s, ast.Try):
            body = self.block(s.body, indent)
            catch = "catch" + (" " + s.catch_name.name if s.catch_name is not None else "")
            return f"try {body} {catch} {self.block(s.handler, indent, s.body.span.end)}"
        if isinstance(s, ast.Import):
            tok = self.token_from(s.span.start, T.STRING)
            path = tok.span.text if tok is not None and tok.span.end <= s.span.end else _quote(s.path)
            return f"import {path}" + (f" as {s.alias.name}" if s.alias is not None else "")
        raise TypeError(f"cannot format statement {type(s).__name__}")

    def assign(self, s: ast.Assign, indent: int) -> str:
        if isinstance(s.target, ast.ListLit):  # a, b = ...
            targets = ", ".join(self.expr(t, 0, 0, indent) for t in s.target.items)
            v = s.value
            if isinstance(v, ast.ListLit) and len(v.items) >= 2 and \
                    v.span.start == v.items[0].span.start and v.span.end == v.items[-1].span.end:
                value = ", ".join(self.expr(x, 0, 0, indent) for x in v.items)  # a, b = 1, 2
            else:
                value = self.expr(v, 0, 0, indent)
            return f"{targets} = {value}"
        op = "=" if s.op is None else s.op + "="
        return f"{self.expr(s.target, 0, 0, indent)} {op} {self.expr(s.value, 0, 0, indent)}"

    # Functions
    @staticmethod
    def fn_style(f: ast.Function, allow_math: bool = False) -> str:
        single = len(f.body.stmts) == 1 and isinstance(f.body.stmts[0], ast.ExprStmt)
        if f.style == "math" and allow_math and single and f.ret_type is None and \
                all(p.type is None and p.default is None for p in f.params):
            return "math"
        if f.style in ("arrow", "math") and single:
            return "arrow"
        return "block"

    def params(self, f: ast.Function, after: int, indent: int) -> str:
        open_tok = self.token_from(after, T.LPAREN)
        open_pos = open_tok.span.start if open_tok is not None else after
        last = f.params[-1].span.end if f.params else open_pos + 1
        close_tok = self.token_from(last, T.RPAREN)
        close_pos = close_tok.span.start if close_tok is not None else last

        def render(i: int, ind: int) -> str:
            p = f.params[i]
            out = p.name
            if p.type is not None:
                out += f": {p.type}"
            if p.default is not None:
                out += " = " + self.expr(p.default, 0, 0, ind)
            return out

        spans = [(p.span.start, p.span.end) for p in f.params]
        return self.seq("(", ")", open_pos, close_pos, spans, render, indent)

    @staticmethod
    def ret(f: ast.Function) -> str:
        return f" -> {f.ret_type}" if f.ret_type is not None else ""

    def fn_decl(self, s: ast.FnDecl, indent: int) -> str:
        f = s.func
        style = self.fn_style(f, allow_math=True)
        params = self.params(f, s.target.span.end, indent)
        if style == "math":
            return f"{s.target.name}{params} = " + self.expr(_only_expr(f.body), 0, 0, indent)
        head = f"fn {s.target.name}{params}{self.ret(f)}"
        if style == "arrow":
            return head + " => " + self.expr(_only_expr(f.body), 0, 0, indent)
        return head + " " + self.block(f.body, indent)

    def lambda_(self, e: ast.Lambda, indent: int) -> str:
        f = e.func
        head = "fn" + self.params(f, f.span.start, indent) + self.ret(f)
        if self.fn_style(f) == "arrow":
            return head + " => " + self.expr(_only_expr(f.body), 0, 0, indent)
        if self.on_one_line(f.span) and not self.has_comments(f.span.start, f.span.end):
            mark = self.mark()
            inline = self.inline_block(f.body, indent)
            if inline is not None:
                return head + " " + inline
            self.rollback(mark)
        return head + " " + self.block(f.body, indent)

    # If
    def if_blocks(self, e: ast.IfExpr) -> list[ast.Block]:
        return [b for _, b in e.branches] + ([e.else_] if e.else_ is not None else [])

    def if_stmt(self, e: ast.IfExpr, indent: int) -> str:
        if self.on_one_line(e.span) and not self.has_comments(e.span.start, e.span.end):
            mark = self.mark()
            text = self.if_one_line(e, indent)
            if text is not None and len(INDENT * indent + text) <= MAX_WIDTH:
                return text
            self.rollback(mark)
        return self.if_multiline(e, indent)

    def if_one_line(self, e: ast.IfExpr, indent: int) -> str | None:
        """if c { stmt } else { stmt }, or None when a branch is not simple."""
        parts = []
        for k, (cond, b) in enumerate(e.branches):
            inline = self.inline_block(b, indent)
            if inline is None:
                return None
            parts.append(("if " if k == 0 else "elif ") + self.expr(cond, 0, 0, indent) + " " + inline)
        if e.else_ is not None:
            inline = self.inline_block(e.else_, indent)
            if inline is None:
                return None
            parts.append("else " + inline)
        text = " ".join(parts)
        return None if "\n" in text else text

    def if_expr(self, e: ast.IfExpr, indent: int) -> str:
        blocks = self.if_blocks(e)
        # Only comments from the first block on force the block form, since comments in
        # the first condition are moved before the statement either way
        if all(len(b.stmts) == 1 and isinstance(b.stmts[0], ast.ExprStmt) for b in blocks) and \
                not self.has_comments(blocks[0].span.start, e.span.end):
            mark = self.mark()
            parts = []
            for k, (cond, b) in enumerate(e.branches):
                parts.append(("if " if k == 0 else "elif ") + self.expr(cond, 0, 0, indent) +
                             " { " + self.expr(_only_expr(b), 0, 0, indent) + " }")
            if e.else_ is not None:
                parts.append("else { " + self.expr(_only_expr(e.else_), 0, 0, indent) + " }")
            text = " ".join(parts)
            if "\n" not in text:
                return text
            self.rollback(mark)  # a part is multi-line: use the block form instead
        return self.if_multiline(e, indent)

    def if_multiline(self, e: ast.IfExpr, indent: int) -> str:
        out = []
        prev_end: int | None = None
        for k, (cond, b) in enumerate(e.branches):
            kw = "if" if k == 0 else "elif"
            out.append(f"{kw} {self.expr(cond, 0, 0, indent)} {self.block(b, indent, prev_end)}")
            prev_end = b.span.end
        if e.else_ is not None:
            out.append("else " + self.block(e.else_, indent, prev_end))
        return " ".join(out)

    # Expressions
    def implicit_literal(self, e: ast.Binary) -> str | None:
        """The number text if e is printed as implicit multiplication (2x), else None."""
        left = e.left
        if not (e.implicit and e.op == "*" and isinstance(left, ast.Literal) and _is_number(left)) \
                or left.value < 0:
            return None
        return self.number(left)

    def bps(self, e: ast.Expr) -> tuple[int, int]:
        """Binding powers (lbp, rbp) of e as it will be printed."""
        if isinstance(e, ast.Binary):
            if self.implicit_literal(e) is not None:
                return _CLOSED, BP_UNARY
            bp = _BINARY_BP[e.op]
            return (bp, bp - 1) if e.op == "^" else (bp, bp)
        if isinstance(e, ast.Logical):
            bp = _LOGICAL_BP[e.op]
            return bp, bp
        if isinstance(e, ast.Compare):
            return BP_COMPARE, BP_COMPARE - 1
        if isinstance(e, ast.RangeExpr):
            return BP_RANGE, BP_RANGE - 1
        if isinstance(e, ast.Pipe):
            return BP_PIPE, BP_PIPE
        if isinstance(e, ast.Unary):
            if e.op == "!":
                return BP_POSTFIX, _CLOSED
            if e.op == "not":
                return _CLOSED, BP_NOT
            return _CLOSED, BP_UNARY
        if isinstance(e, (ast.Call, ast.MethodCall, ast.Field, ast.Index, ast.Slice)):
            return BP_POSTFIX, _CLOSED
        if isinstance(e, ast.Lambda) and self.fn_style(e.func) == "arrow":
            return _CLOSED, 0
        return _CLOSED, _CLOSED

    def expr(self, e: ast.Expr, min_bp: int, follow: int, indent: int) -> str:
        """Format e, adding parentheses only when the parser would build a different tree without them."""
        lbp, rbp = self.bps(e)
        if lbp <= min_bp or follow > rbp:
            return "(" + self.raw(e, 0, 0, indent) + ")"
        return self.raw(e, min_bp, follow, indent)

    def raw(self, e: ast.Expr, min_bp: int, follow: int, indent: int) -> str:
        if isinstance(e, ast.Literal):
            if e.value is None:
                return "nil"
            if e.value is True:
                return "true"
            if e.value is False:
                return "false"
            return self.number(e)
        if isinstance(e, ast.StringLit):
            return self.string(e, indent)
        if isinstance(e, ast.Name):
            return "∞" if e.name == "inf" and e.span.text == "∞" else e.name
        if isinstance(e, ast.ListLit):
            spans = [(x.span.start, x.span.end) for x in e.items]
            return self.seq("[", "]", e.span.start, e.span.end - 1, spans,
                            lambda i, ind: self.expr(e.items[i], 0, 0, ind), indent)
        if isinstance(e, ast.DictLit):
            spans = [(k.span.start, v.span.end) for k, v in e.entries]
            return self.seq("{", "}", e.span.start, e.span.end - 1, spans,
                            lambda i, ind: self.dict_entry(e, i, ind), indent)
        if isinstance(e, ast.Comprehension):
            return self.comprehension(e, indent)
        if isinstance(e, ast.RangeExpr):
            op = ".." if e.inclusive else "..<"
            return self.expr(e.start, min_bp, BP_RANGE, indent) + op + \
                self.expr(e.end, BP_RANGE, follow, indent)
        if isinstance(e, ast.Unary):
            if e.op == "!":
                return self.expr(e.operand, min_bp, BP_POSTFIX, indent) + "!"
            if e.op == "not":
                return "not " + self.expr(e.operand, BP_NOT, follow, indent)
            return e.op + self.expr(e.operand, BP_UNARY, follow, indent)
        if isinstance(e, ast.Binary):
            return self.binary(e, min_bp, follow, indent)
        if isinstance(e, ast.Logical):
            return self.infix_run(e, _LOGICAL_BP[e.op], min_bp, follow, indent)
        if isinstance(e, ast.Compare):
            last = len(e.ops) - 1
            out = self.expr(e.operands[0], min_bp, BP_COMPARE, indent)
            for i, (op, rhs) in enumerate(zip(e.ops, e.operands[1:])):
                out += f" {op} " + self.expr(rhs, BP_COMPARE, follow if i == last else BP_COMPARE, indent)
            return out
        if isinstance(e, ast.Pipe):
            return self.pipe(e, min_bp, follow, indent)
        if isinstance(e, ast.Call):
            callee = e.callee
            if isinstance(callee, ast.Field) or _is_number(callee):
                # (a.b)(x) is not the method call a.b(x), and (2)(x) is not 2(x)
                head = "(" + self.raw(callee, 0, 0, indent) + ")"
            else:
                head = self.expr(callee, min_bp, BP_POSTFIX, indent)
            return head + self.args(e.args, callee.span.end, e.span.end - 1, indent)
        if isinstance(e, (ast.MethodCall, ast.Field)):
            return self.chain(e, min_bp, indent)
        if isinstance(e, ast.Index):
            return self.expr(e.obj, min_bp, BP_POSTFIX, indent) + \
                "[" + self.expr(e.index, 0, 0, indent) + "]"
        if isinstance(e, ast.Slice):
            parts = ["" if x is None else self.expr(x, 0, 0, indent) for x in (e.start, e.stop)]
            inner = ":".join(parts)
            if e.step is not None:
                inner += ":" + self.expr(e.step, 0, 0, indent)
            return self.expr(e.obj, min_bp, BP_POSTFIX, indent) + "[" + inner + "]"
        if isinstance(e, ast.Lambda):
            return self.lambda_(e, indent)
        if isinstance(e, ast.IfExpr):
            return self.if_expr(e, indent)
        raise TypeError(f"cannot format expression {type(e).__name__}")

    def number(self, e: ast.Literal) -> str:
        text = e.span.text
        if text and text[0].isdigit() and all(ch.isalnum() or ch in "._+-" for ch in text):
            return text
        return repr(e.value)

    def string(self, e: ast.StringLit, indent: int) -> str:
        text = e.span.text
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            return text
        # Fallback for trees not built from source text
        if e.raw:
            return "'" + e.plain_value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        out = []
        for part in e.parts:
            if isinstance(part, str):
                out.append(_quote(part)[1:-1])
            else:
                spec = ":" + part.spec if part.spec else ""
                out.append("{" + self.expr(part.expr, 0, 0, indent) + spec + "}")
        return '"' + "".join(out) + '"'

    def binary(self, e: ast.Binary, min_bp: int, follow: int, indent: int) -> str:
        lit = self.implicit_literal(e)
        if lit is not None:
            right = self.expr(e.right, BP_UNARY, follow, indent)
            if not self.can_follow_number(lit, right):
                right = "(" + right + ")"
            return lit + right
        if e.op == "^":
            return self.expr(e.left, min_bp, BP_POWER, indent) + "^" + \
                self.expr(e.right, BP_POWER - 1, follow, indent)
        return self.infix_run(e, _BINARY_BP[e.op], min_bp, follow, indent)

    def infix_run(self, e: ast.Binary | ast.Logical, bp: int, min_bp: int, follow: int, indent: int) -> str:
        """Format a run of same-precedence operators down the left side (a + b - c) without recursion."""
        def same(n: ast.Expr) -> bool:
            if isinstance(n, ast.Logical):
                return _LOGICAL_BP[n.op] == bp
            return isinstance(n, ast.Binary) and n.op != "^" and _BINARY_BP[n.op] == bp and \
                self.implicit_literal(n) is None

        run: list[ast.Binary | ast.Logical] = []
        node: ast.Expr = e
        while isinstance(node, (ast.Binary, ast.Logical)) and same(node):
            run.append(node)
            node = node.left
        run.reverse()

        def operand(n: ast.Expr, lo: int, hi: int) -> str:
            # Always write a or (b and c) with parentheses, since it is easier to read
            if bp == BP_OR and isinstance(n, ast.Logical) and n.op == "and":
                return "(" + self.raw(n, 0, 0, indent) + ")"
            return self.expr(n, lo, hi, indent)

        out = operand(node, min_bp, bp)
        for i, n in enumerate(run):
            out += f" {n.op} " + operand(n.right, bp, follow if i == len(run) - 1 else bp)
        return out

    @staticmethod
    def can_follow_number(lit: str, right: str) -> bool:
        """Check that lit + right lexes as the number followed by an unspaced name, ( or √."""
        if not right:
            return False
        if right[0] in "(√∞":
            return True
        word = _ident_prefix(right)
        if not word:
            return False
        try:
            toks = Lexer(Source(lit + word)).tokenize()
        except JarLangError:
            return False
        return len(toks) >= 2 and toks[0].span.text == lit and toks[1].kind is T.IDENT and \
            toks[1].span.text == word and not toks[1].spaced

    def dict_entry(self, d: ast.DictLit, i: int, indent: int) -> str:
        key, value = d.entries[i]
        bare = d.bare_keys[i] if i < len(d.bare_keys) else False
        if isinstance(key, ast.StringLit) and bare and key.is_plain:
            name = key.plain_value
            k = name if _is_ident(name) and name not in KEYWORDS else _quote(name)
        elif isinstance(key, (ast.Literal, ast.StringLit)) or \
                isinstance(key, ast.Unary) and key.op == "-" and _is_number(key.operand):
            k = self.expr(key, 0, 0, indent)
        else:
            # Computed keys keep their parentheses, since x: 1 would be the string key "x"
            k = "(" + self.raw(key, 0, 0, indent) + ")"
        return k + ": " + self.expr(value, 0, 0, indent)

    def args(self, args: list[ast.Expr], after: int, close_pos: int, indent: int) -> str:
        open_tok = self.token_from(after, T.LPAREN)
        open_pos = open_tok.span.start if open_tok is not None and open_tok.span.start < close_pos else after
        spans = [(a.span.start, a.span.end) for a in args]
        return self.seq("(", ")", open_pos, close_pos, spans,
                        lambda i, ind: self.expr(args[i], 0, 0, ind), indent)

    def comprehension(self, e: ast.Comprehension, indent: int) -> str:
        for_tok = self.token_from(e.expr.span.end, T.FOR)
        for_pos = for_tok.span.start if for_tok is not None else e.expr.span.end
        parts: list[tuple[int, int, Callable[[int], str]]] = [
            (e.expr.span.start, e.expr.span.end, lambda ind: self.expr(e.expr, 0, 0, ind)),
            (for_pos, e.iterable.span.end,
             lambda ind: "for " + ", ".join(t.name for t in e.targets) + " in " +
             self.expr(e.iterable, 0, 0, ind)),
        ]
        cond = e.cond
        if cond is not None:
            if_tok = self.token_from(e.iterable.span.end, T.IF)
            if_pos = if_tok.span.start if if_tok is not None else cond.span.start
            parts.append((if_pos, cond.span.end, lambda ind: "if " + self.expr(cond, 0, 0, ind)))
        return self.seq("[", "]", e.span.start, e.span.end - 1, [(s, t) for s, t, _ in parts],
                        lambda i, ind: parts[i][2](ind), indent, sep="")

    # Layout
    def broken(self, open_pos: int, spans: Sequence[tuple[int, int]], close_pos: int) -> bool:
        """Check for a line break at this level of a bracketed sequence."""
        prev = self.line(open_pos)
        for start, end in spans:
            if self.line(start) != prev:
                return True
            prev = self.last_line(start, end)
        return self.line(close_pos) != prev

    def seq(self, opener: str, closer: str, open_pos: int, close_pos: int,
            spans: Sequence[tuple[int, int]], render: Callable[[int, int], str], indent: int,
            sep: str = ",") -> str:
        """Format a bracketed sequence, one element per line if the original had a line break."""
        n = len(spans)
        multi = self.broken(open_pos, spans, close_pos) if n else self.has_comments(open_pos, close_pos)
        if not multi:
            return opener + (sep + " ").join(render(i, indent) for i in range(n)) + closer
        pad = INDENT * (indent + 1)
        first = spans[0][0] if n else close_pos
        head = self.with_comment(opener, self.take_trailing(open_pos + 1, first, self.line(open_pos)))
        lines: list[str] = []
        cursor = open_pos + 1
        for i, (start, end) in enumerate(spans):
            lines.extend(pad + comment_text(c) for c in self.take(cursor, start))
            text = pad + render(i, indent + 1) + sep
            nxt = spans[i + 1][0] if i + 1 < n else close_pos
            lines.append(self.with_comment(text, self.take_trailing(end, nxt, self.last_line(start, end))))
            cursor = end
        lines.extend(pad + comment_text(c) for c in self.take(cursor, close_pos))
        # lines is empty when the only comment is right after the opener, and that shouldn't add a blank line
        return head + "\n" + "".join(line + "\n" for line in lines) + INDENT * indent + closer

    def continuation(self, out: str, cursor: int, cursor_line: int, op_pos: int, indent: int) -> str:
        """End the current line of a multi-line pipeline or chain, keeping its comments."""
        out = self.with_comment(out, self.take_trailing(cursor, op_pos, cursor_line))
        for c in self.take(cursor, op_pos):
            out += "\n" + INDENT * indent + comment_text(c)
        return out

    def pipe(self, e: ast.Pipe, min_bp: int, follow: int, indent: int) -> str:
        funcs: list[ast.Expr] = []
        node: ast.Expr = e
        while isinstance(node, ast.Pipe):
            funcs.append(node.func)
            node = node.value
        funcs.reverse()
        base = node
        prev_line = self.span_last_line(base.span)
        multi = False
        for f in funcs:
            if self.line(f.span.start) != prev_line:
                multi = True
                break
            prev_line = self.span_last_line(f.span)
        out = self.expr(base, min_bp, BP_PIPE, indent)
        cursor, cursor_line = base.span.end, self.span_last_line(base.span)
        for i, f in enumerate(funcs):
            fol = follow if i == len(funcs) - 1 else BP_PIPE
            if multi:
                op = self.token_from(cursor, T.PIPE_GT)
                op_pos = op.span.start if op is not None and op.span.start < f.span.start else f.span.start
                out = self.continuation(out, cursor, cursor_line, op_pos, indent + 1)
                out += "\n" + INDENT * (indent + 1) + "|> " + self.expr(f, BP_PIPE, fol, indent + 1)
            else:
                out += " |> " + self.expr(f, BP_PIPE, fol, indent)
            cursor, cursor_line = f.span.end, self.span_last_line(f.span)
        return out

    def chain(self, e: ast.MethodCall | ast.Field, min_bp: int, indent: int) -> str:
        segs: list[ast.MethodCall | ast.Field] = []
        node: ast.Expr = e
        while isinstance(node, (ast.MethodCall, ast.Field)):
            segs.append(node)
            node = node.receiver if isinstance(node, ast.MethodCall) else node.obj
        segs.reverse()
        base = node

        def name_start(s: ast.MethodCall | ast.Field) -> int:
            if isinstance(s, ast.MethodCall):
                return s.method.span.start
            return s.name_span.start if s.name_span is not None else s.span.end - len(s.name)

        broken = []
        prev_line = self.span_last_line(base.span)
        for s in segs:
            broken.append(self.line(name_start(s)) != prev_line)
            prev_line = self.span_last_line(s.span)
        multi = any(broken)

        out = self.expr(base, min_bp, BP_POSTFIX, indent)
        cursor, cursor_line = base.span.end, self.span_last_line(base.span)
        seg_indent = indent + 1 if multi else indent
        for s, was_broken in zip(segs, broken):
            if isinstance(s, ast.MethodCall):
                text = "." + s.method.name + self.args(s.args, s.method.span.end, s.span.end - 1, seg_indent)
            else:
                text = "." + s.name
            if multi and (isinstance(s, ast.MethodCall) or was_broken):
                dot = self.token_before(name_start(s), T.DOT)
                dot_pos = dot.span.start if dot is not None and dot.span.start >= cursor else name_start(s)
                out = self.continuation(out, cursor, cursor_line, dot_pos, seg_indent)
                out += "\n" + INDENT * seg_indent + text
            else:
                out += text
            cursor, cursor_line = s.span.end, self.span_last_line(s.span)
        return out
