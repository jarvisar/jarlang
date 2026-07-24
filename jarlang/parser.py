"""Parses the token stream into a tree of nodes, using operator precedence."""

from __future__ import annotations

from . import ast
from .errors import Label, MultipleErrors, ParseError
from .lexer import Interp, Lexer
from .source import Source, Span
from .tokens import COMPOUND_ASSIGN, T, Token

# Operator precedence, from loosest to tightest
BP_PIPE = 10
BP_OR = 20
BP_AND = 30
BP_NOT = 40
BP_COMPARE = 50
BP_RANGE = 60
BP_ADD = 70
BP_MUL = 80
BP_UNARY = 90
BP_POWER = 100
BP_POSTFIX = 110

_INFIX_BP: dict[T, int] = {
    T.PIPE_GT: BP_PIPE,
    T.OR: BP_OR, T.PIPEPIPE: BP_OR,
    T.AND: BP_AND, T.AMPAMP: BP_AND,
    T.EQ: BP_COMPARE, T.NE: BP_COMPARE, T.LT: BP_COMPARE, T.LE: BP_COMPARE,
    T.GT: BP_COMPARE, T.GE: BP_COMPARE, T.IN: BP_COMPARE, T.NOT: BP_COMPARE,
    T.DOTDOT: BP_RANGE, T.DOTDOTLT: BP_RANGE,
    T.PLUS: BP_ADD, T.MINUS: BP_ADD,
    T.STAR: BP_MUL, T.SLASH: BP_MUL, T.DSLASH: BP_MUL, T.PERCENT: BP_MUL,
    T.CARET: BP_POWER,
    T.LPAREN: BP_POSTFIX, T.LBRACKET: BP_POSTFIX, T.DOT: BP_POSTFIX, T.BANG: BP_POSTFIX,
}

_BINARY_OPS = {T.PLUS: "+", T.MINUS: "-", T.STAR: "*", T.SLASH: "/", T.DSLASH: "//",
               T.PERCENT: "%", T.CARET: "^"}
_COMPARE_OPS = {T.EQ: "==", T.NE: "!=", T.LT: "<", T.LE: "<=", T.GT: ">", T.GE: ">=", T.IN: "in"}
_STMT_END = (T.NEWLINE, T.SEMI, T.RBRACE, T.EOF)
_OPENERS = frozenset({T.LPAREN, T.LBRACKET, T.LBRACE})
_CLOSERS = frozenset({T.RPAREN, T.RBRACKET, T.RBRACE})


class Parser:
    def __init__(self, tokens: list[Token], source: Source) -> None:
        self.tokens = tokens
        self.source = source
        self.pos = 0
        self.errors: list[ParseError] = []
        # Spans of groups in parentheses, so errors underline the parentheses too
        self.paren_spans: dict[int, Span] = {}
        # Brackets opened but not closed yet, so synchronize knows where the broken statement ends
        self.nesting = 0

    def _full(self, node: ast.Node) -> Span:
        return self.paren_spans.get(id(node), node.span)

    # Token helpers
    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    @property
    def prev(self) -> Token:
        return self.tokens[self.pos - 1] if self.pos > 0 else self.tokens[0]

    def peek(self, offset: int = 1) -> Token:
        i = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[i]

    def at(self, *kinds: T) -> bool:
        return self.tok.kind in kinds

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        kind = tok.kind
        if kind is not T.EOF:
            self.pos += 1
            if kind in _OPENERS:
                self.nesting += 1
            elif kind in _CLOSERS:
                self.nesting -= 1
        return tok

    def accept(self, kind: T) -> Token | None:
        if self.tok.kind is kind:
            return self.advance()
        return None

    def expect(self, kind: T, context: str = "", help_: str | None = None) -> Token:
        if self.tok.kind is kind:
            return self.advance()
        raise self.error_here(f"expected {kind.value}{' ' + context if context else ''}, "
                              f"found {self.describe(self.tok)}", help_=help_)

    def expect_closer(self, kind: T, opener: Token, what: str) -> Token:
        """Expect a closing bracket, pointing back at its opener if it is missing."""
        if self.tok.kind is kind:
            return self.advance()
        err = self.error_here(f"expected {kind.value} to close {what}, found {self.describe(self.tok)}")
        err.diagnostic.secondary.append(Label(opener.span, f"unclosed {opener.kind.value} opened here"))
        raise err

    def skip_newlines(self) -> None:
        while self.tok.kind is T.NEWLINE:
            self.pos += 1

    def span_from(self, start: Span) -> Span:
        end = self.prev.span
        if end.end < start.start:
            return start
        return start.to(end)

    @staticmethod
    def describe(tok: Token) -> str:
        if tok.kind is T.EOF:
            return "end of input"
        if tok.kind is T.NEWLINE:
            return "end of line"
        if tok.kind is T.IDENT:
            return f"`{tok.value}`"
        if tok.kind in (T.INT, T.FLOAT):
            return f"number `{tok.text}`"
        if tok.kind is T.STRING:
            return "a string"
        return tok.kind.value

    def error_here(self, message: str, label: str = "", help_: str | None = None,
                   tok: Token | None = None) -> ParseError:
        tok = tok or self.tok
        incomplete = tok.kind is T.EOF
        span = tok.span
        if tok.kind is T.NEWLINE:
            span = Span(self.source, tok.span.start, tok.span.start + 1)
        err = ParseError(message, span, label, helps=[help_] if help_ else [], incomplete=incomplete)
        return err

    # Program and statements
    def parse_program(self) -> ast.Program:
        start = self.tok.span
        stmts: list[ast.Stmt] = []
        self.skip_separators()
        while not self.at(T.EOF):
            try:
                stmt = self.parse_statement()
                stmts.append(stmt)
                self.end_statement()
            except ParseError as err:
                self.errors.append(err)
                self.synchronize()
            self.skip_separators()
        if self.errors:
            if len(self.errors) == 1:
                raise self.errors[0]
            multi = MultipleErrors([e.diagnostic for e in self.errors])
            raise _with_incomplete(multi, any(e.incomplete for e in self.errors))
        return ast.Program(stmts, span=start.to(self.tok.span))

    def skip_separators(self) -> None:
        while self.tok.kind in (T.NEWLINE, T.SEMI):
            self.pos += 1

    def end_statement(self) -> None:
        if self.tok.kind in _STMT_END:
            return
        tok = self.tok
        prev = self.prev
        help_ = "put each statement on its own line, or separate them with `;`"
        if prev.kind is T.IDENT and tok.kind in (T.STRING, T.INT, T.FLOAT, T.IDENT) and tok.spaced:
            help_ = f"to call `{prev.value}`, use parentheses: {prev.value}(...)"
        elif tok.kind is T.ASSIGN:
            help_ = "only names, indexes (a[i]) and fields (a.b) can be assigned to"
        elif tok.kind is T.COMMA and prev.kind is T.INT and self.peek().kind is T.INT and not self.peek().spaced:
            help_ = (f"JarLang 2 writes digit separators with underscores: "
                     f"{prev.text}_{self.peek().text} (commas separate arguments and list items)")
        raise self.error_here(f"expected end of statement, found {self.describe(tok)}", help_=help_)

    def synchronize(self) -> None:
        """Skip to the next statement after an error, so more errors can be reported."""
        # Skip past the brackets that were open when the error happened too. Otherwise the
        # rest of a broken block is parsed as new statements and reports made-up errors.
        depth = max(self.nesting, 0)
        while not self.at(T.EOF):
            kind = self.tok.kind
            if kind in _OPENERS:
                depth += 1
            elif kind in _CLOSERS:
                depth = max(depth - 1, 0)  # a stray closing bracket is skipped
            elif kind in (T.NEWLINE, T.SEMI) and depth == 0:
                break
            self.pos += 1
        self.nesting = 0

    def parse_statement(self) -> ast.Stmt:
        kind = self.tok.kind
        if kind in (T.LET, T.CONST):
            return self.parse_let()
        if kind is T.FN and self.peek().kind is T.IDENT:
            return self.parse_fn_decl()
        if kind is T.WHILE:
            return self.parse_while()
        if kind is T.FOR:
            return self.parse_for()
        if kind is T.RETURN:
            start = self.advance().span
            value = None if self.at(*_STMT_END) else self.parse_expr()
            return ast.Return(value, span=self.span_from(start))
        if kind is T.BREAK:
            return ast.Break(span=self.advance().span)
        if kind is T.CONTINUE:
            return ast.Continue(span=self.advance().span)
        if kind is T.THROW:
            start = self.advance().span
            return ast.Throw(self.parse_expr(), span=self.span_from(start))
        if kind is T.TRY:
            return self.parse_try()
        if kind is T.IMPORT:
            return self.parse_import()
        return self.parse_expr_statement()

    def parse_let(self) -> ast.Let:
        start = self.tok.span
        const = self.advance().kind is T.CONST
        name_tok = self.expect(T.IDENT, f"after `{'const' if const else 'let'}`")
        target = ast.Name(name_tok.value, span=name_tok.span)
        type_ = None
        if self.accept(T.COLON):
            type_ = self.parse_type()
        value = None
        if self.accept(T.ASSIGN):
            value = self.parse_expr()
        elif const:
            raise self.error_here("a `const` needs a value", help_=f"write `const {target.name} = ...`")
        return ast.Let(target, value, type_, const, span=self.span_from(start))

    def parse_fn_decl(self) -> ast.FnDecl:
        start = self.advance().span  # fn
        name_tok = self.expect(T.IDENT, "after `fn`")
        func = self.parse_function_rest(name_tok.value, start)
        return ast.FnDecl(ast.Name(name_tok.value, span=name_tok.span), func, span=self.span_from(start))

    def parse_function_rest(self, name: str | None, start: Span) -> ast.Function:
        self.expect(T.LPAREN, "to start the parameter list")
        params = self.parse_params()
        ret_type = None
        if self.accept(T.ARROW):
            ret_type = self.parse_type()
        if self.accept(T.FAT_ARROW):
            expr = self.parse_expr()
            body = ast.Block([ast.ExprStmt(expr, span=expr.span)], span=expr.span)
            style = "arrow"
        elif self.at(T.LBRACE):
            body = self.parse_block()
            style = "block"
        else:
            raise self.error_here(f"expected `{{` or `=>` to start the function body, found {self.describe(self.tok)}",
                                  help_="write `fn f(x) { ... }` or `fn f(x) => expr`")
        return ast.Function(name, params, body, ret_type, style, span=self.span_from(start))

    def parse_params(self) -> list[ast.Param]:
        params: list[ast.Param] = []
        seen: set[str] = set()
        while not self.at(T.RPAREN):
            tok = self.expect(T.IDENT, "as a parameter name")
            if tok.value in seen:
                raise self.error_here(f"duplicate parameter `{tok.value}`", tok=tok)
            seen.add(tok.value)
            type_ = self.parse_type() if self.accept(T.COLON) else None
            default = self.parse_expr() if self.accept(T.ASSIGN) else None
            if default is None and params and params[-1].default is not None:
                raise self.error_here("parameters without defaults must come before those with defaults", tok=tok)
            params.append(ast.Param(tok.value, type_, default, span=self.span_from(tok.span)))
            if not self.accept(T.COMMA):
                break
        self.expect(T.RPAREN, "to close the parameter list")
        return params

    def parse_type(self) -> ast.TypeRef:
        tok = self.tok
        if tok.kind is T.NIL:
            self.advance()
            return ast.TypeRef("nil", span=tok.span)
        if tok.kind is T.FN:
            self.advance()
            return ast.TypeRef("fn", span=tok.span)
        tok = self.expect(T.IDENT, "as a type name", help_="types: int, float, num, bool, str, list, dict, fn, any")
        args: list[ast.TypeRef] = []
        if self.accept(T.LBRACKET):
            while not self.at(T.RBRACKET):
                args.append(self.parse_type())
                if not self.accept(T.COMMA):
                    break
            self.expect(T.RBRACKET)
        return ast.TypeRef(tok.value, args, span=self.span_from(tok.span))

    def parse_block(self) -> ast.Block:
        opener = self.expect(T.LBRACE, "to start a block")
        start = opener.span
        stmts: list[ast.Stmt] = []
        self.skip_separators()
        while not self.at(T.RBRACE):
            if self.at(T.EOF):
                self.expect_closer(T.RBRACE, opener, "the block")
            stmts.append(self.parse_statement())
            if not self.at(T.RBRACE):
                self.end_statement()
            self.skip_separators()
        self.expect(T.RBRACE)
        return ast.Block(stmts, span=self.span_from(start))

    def parse_while(self) -> ast.While:
        start = self.advance().span
        cond = self.parse_expr()
        body = self.parse_block()
        return ast.While(cond, body, span=self.span_from(start))

    def parse_for(self) -> ast.For:
        start = self.advance().span
        targets = self.parse_targets("after `for`")
        self.expect(T.IN, "after the loop variable", help_="write `for x in 1..10 { ... }`")
        iterable = self.parse_expr()
        body = self.parse_block()
        return ast.For(targets, iterable, body, span=self.span_from(start))

    def parse_targets(self, context: str) -> list[ast.Name]:
        targets = []
        while True:
            tok = self.expect(T.IDENT, context)
            targets.append(ast.Name(tok.value, span=tok.span))
            if not self.accept(T.COMMA):
                return targets

    def parse_try(self) -> ast.Try:
        start = self.advance().span
        body = self.parse_block()
        self._skip_newlines_before(T.CATCH)
        self.expect(T.CATCH, "after the `try` block", help_="write `try { ... } catch err { ... }`")
        name = None
        if self.at(T.IDENT):
            tok = self.advance()
            name = ast.Name(tok.value, span=tok.span)
        handler = self.parse_block()
        return ast.Try(body, name, handler, span=self.span_from(start))

    def parse_import(self) -> ast.Import:
        start = self.advance().span
        tok = self.expect(T.STRING, "after `import`", help_='write `import "file.jlang"`')
        if not all(isinstance(p, str) for p in tok.value):
            raise self.error_here("import paths cannot contain interpolation", tok=tok)
        path = "".join(tok.value)
        alias = None
        if self.at(T.IDENT) and self.tok.value == "as":
            self.advance()
            name_tok = self.expect(T.IDENT, "after `as`")
            alias = ast.Name(name_tok.value, span=name_tok.span)
        return ast.Import(path, alias, span=self.span_from(start))

    def _skip_newlines_before(self, *kinds: T) -> bool:
        i = self.pos
        while self.tokens[i].kind is T.NEWLINE:
            i += 1
        if self.tokens[i].kind in kinds:
            self.pos = i
            return True
        return False

    def parse_expr_statement(self) -> ast.Stmt:
        start = self.tok.span
        expr = self.parse_expr()

        if self.at(T.COMMA) and self._looks_like_multi_assign():
            targets = [expr]
            while self.accept(T.COMMA):
                targets.append(self.parse_expr())
            assign_tok = self.expect(T.ASSIGN, "in multiple assignment")
            values = [self.parse_expr()]
            while self.accept(T.COMMA):
                values.append(self.parse_expr())
            for t in targets:
                self._check_target(t, assign_tok)
            if len(values) == 1:
                value: ast.Expr = values[0]
            else:
                value = ast.ListLit(values, span=values[0].span.to(values[-1].span))
            target_list = ast.ListLit(targets, span=targets[0].span.to(targets[-1].span))
            return ast.Assign(target_list, value, None, span=self.span_from(start))

        if self.at(T.ASSIGN):
            assign_tok = self.advance()
            # Math-style function definition: f(x, y) = x^2 + y
            if isinstance(expr, ast.Call) and isinstance(expr.callee, ast.Name) and \
                    all(isinstance(a, ast.Name) for a in expr.args):
                args: list[ast.Name] = expr.args  # type: ignore[assignment]
                seen: set[str] = set()
                for arg in args:
                    if arg.name in seen:
                        raise ParseError(f"duplicate parameter `{arg.name}`", arg.span)
                    seen.add(arg.name)
                body_expr = self.parse_expr()
                params = [ast.Param(a.name, span=a.span) for a in args]
                body = ast.Block([ast.ExprStmt(body_expr, span=body_expr.span)], span=body_expr.span)
                func = ast.Function(expr.callee.name, params, body, None, "math", span=self.span_from(start))
                return ast.FnDecl(ast.Name(expr.callee.name, span=expr.callee.span), func, span=self.span_from(start))
            self._check_target(expr, assign_tok)
            value = self.parse_expr()
            return ast.Assign(expr, value, None, span=self.span_from(start))

        if self.tok.kind in COMPOUND_ASSIGN:
            op_tok = self.advance()
            self._check_target(expr, op_tok)
            value = self.parse_expr()
            op = _BINARY_OPS[COMPOUND_ASSIGN[op_tok.kind]]
            return ast.Assign(expr, value, op, span=self.span_from(start))

        return ast.ExprStmt(expr, span=expr.span)

    def _looks_like_multi_assign(self) -> bool:
        depth = 0
        i = self.pos
        while True:
            kind = self.tokens[i].kind
            if kind is T.EOF:
                return False
            if kind in (T.LPAREN, T.LBRACKET, T.LBRACE):
                depth += 1
            elif kind in (T.RPAREN, T.RBRACKET, T.RBRACE):
                depth -= 1
                if depth < 0:
                    return False
            elif depth == 0 and kind is T.ASSIGN:
                return True
            elif depth == 0 and kind in (T.NEWLINE, T.SEMI, T.EOF):
                return False
            i += 1

    def _check_target(self, expr: ast.Expr, op_tok: Token) -> None:
        if isinstance(expr, (ast.Name, ast.Index, ast.Field)):
            return
        help_ = None
        if isinstance(expr, ast.Compare) or op_tok.kind is T.ASSIGN and isinstance(expr, ast.Literal):
            help_ = "to compare values use `==`"
        if isinstance(expr, ast.Call):
            help_ = "to define a function write `f(x) = ...` with plain parameter names, or `fn f(x) { ... }`"
        raise ParseError("cannot assign to this expression", expr.span, "not assignable",
                         helps=[help_] if help_ else [])

    # Expressions
    def parse_expr(self, min_bp: int = 0) -> ast.Expr:
        left = self.parse_prefix()
        while True:
            tok = self.tok
            bp = _INFIX_BP.get(tok.kind)
            if bp is None or bp <= min_bp:
                break
            if tok.kind is T.NOT and self.peek().kind is not T.IN:
                break
            left = self.parse_infix(left, bp)
        return left

    def parse_prefix(self) -> ast.Expr:
        tok = self.tok
        kind = tok.kind
        if kind in (T.INT, T.FLOAT):
            self.advance()
            lit = ast.Literal(tok.value, span=tok.span)
            return self._implicit_multiplication(lit)
        if kind is T.STRING:
            self.advance()
            return self.parse_string(tok)
        if kind is T.IDENT:
            self.advance()
            return ast.Name(tok.value, span=tok.span)
        if kind is T.TRUE:
            self.advance()
            return ast.Literal(True, span=tok.span)
        if kind is T.FALSE:
            self.advance()
            return ast.Literal(False, span=tok.span)
        if kind is T.NIL:
            self.advance()
            return ast.Literal(None, span=tok.span)
        if kind is T.LPAREN:
            self.advance()
            if self.at(T.RPAREN):
                raise self.error_here("empty parentheses `()` are not an expression")
            self.skip_newlines()
            inner = self.parse_expr()
            self.skip_newlines()
            self.expect_closer(T.RPAREN, tok, "the parenthesis")
            self.paren_spans[id(inner)] = tok.span.to(self.prev.span)
            return inner
        if kind is T.LBRACKET:
            return self.parse_list()
        if kind is T.LBRACE:
            return self.parse_dict()
        if kind in (T.MINUS, T.PLUS):
            self.advance()
            operand = self.parse_expr(BP_UNARY)
            return ast.Unary(tok.value if tok.value in "+-" else "-", operand, span=self.span_from(tok.span))
        if kind is T.SQRT:
            self.advance()
            operand = self.parse_expr(BP_UNARY)
            return ast.Unary("√", operand, span=self.span_from(tok.span))
        if kind is T.NOT:
            self.advance()
            operand = self.parse_expr(BP_NOT)
            return ast.Unary("not", operand, span=self.span_from(tok.span))
        if kind is T.BANG:
            self.advance()
            operand = self.parse_expr(BP_UNARY)
            return ast.Unary("not", operand, span=self.span_from(tok.span))
        if kind is T.FN:
            self.advance()
            func = self.parse_function_rest(None, tok.span)
            return ast.Lambda(func, span=func.span)
        if kind is T.IF:
            return self.parse_if()
        if kind is T.EOF:
            raise self.error_here("expected an expression, found end of input")
        if kind is T.NEWLINE:
            raise self.error_here("expected an expression, found end of line")
        help_ = None
        if kind is T.ASSIGN:
            help_ = "assignment needs a name on the left, e.g. `x = 5`"
        elif kind in (T.STAR, T.SLASH, T.CARET, T.DSLASH, T.PERCENT):
            help_ = f"{kind.value} needs a value on its left"
        raise self.error_here(f"expected an expression, found {self.describe(tok)}", help_=help_)

    def _implicit_multiplication(self, left: ast.Expr) -> ast.Expr:
        """Parse 2x as 2 * x, 3(x + 1) as 3 * (x + 1), and 2√x as 2 * √x."""
        nxt = self.tok
        if nxt.spaced or nxt.kind not in (T.IDENT, T.LPAREN, T.SQRT):
            return left
        right = self.parse_expr(BP_UNARY)
        return ast.Binary("*", left, right, implicit=True, span=self._full(left).to(self._full(right)))

    def parse_infix(self, left: ast.Expr, bp: int) -> ast.Expr:
        tok = self.advance()
        kind = tok.kind
        if kind in _BINARY_OPS:
            if kind is T.CARET:
                right = self.parse_expr(bp - 1)  # right-associative
            else:
                right = self.parse_expr(bp)
            return ast.Binary(_BINARY_OPS[kind], left, right, span=self._full(left).to(self._full(right)))
        if kind in (T.AND, T.AMPAMP, T.OR, T.PIPEPIPE):
            right = self.parse_expr(bp)
            op = "and" if kind in (T.AND, T.AMPAMP) else "or"
            return ast.Logical(op, left, right, span=self._full(left).to(self._full(right)))
        if kind in _COMPARE_OPS or kind is T.NOT:
            return self._parse_compare(left, tok)
        if kind in (T.DOTDOT, T.DOTDOTLT):
            right = self.parse_expr(bp)
            if self.at(T.DOTDOT, T.DOTDOTLT):
                raise self.error_here("ranges cannot be chained", help_="write `a..b`")
            return ast.RangeExpr(left, right, kind is T.DOTDOT, span=self._full(left).to(self._full(right)))
        if kind is T.PIPE_GT:
            right = self.parse_expr(bp)
            return ast.Pipe(left, right, span=self._full(left).to(self._full(right)))
        if kind is T.LPAREN:
            args = self.parse_args(T.RPAREN, tok, "the argument list")
            return ast.Call(left, args, span=self._full(left).to(self.prev.span))
        if kind is T.LBRACKET:
            return self._parse_index(left)
        if kind is T.DOT:
            name_tok = self.expect(T.IDENT, "after `.`")
            if self.at(T.LPAREN) and not self.tok.spaced:
                paren = self.advance()
                args = self.parse_args(T.RPAREN, paren, "the argument list")
                method = ast.Name(name_tok.value, span=name_tok.span)
                return ast.MethodCall(left, method, args, span=self._full(left).to(self.prev.span))
            return ast.Field(left, name_tok.value, name_tok.span, span=self._full(left).to(name_tok.span))
        if kind is T.BANG:
            return ast.Unary("!", left, span=self._full(left).to(tok.span))
        raise self.error_here(f"unexpected {self.describe(tok)}", tok=tok)  # pragma: no cover

    def _parse_compare(self, left: ast.Expr, first: Token) -> ast.Expr:
        operands = [left]
        ops: list[str] = []
        tok: Token | None = first
        while tok is not None:
            if tok.kind is T.NOT:
                self.expect(T.IN, "after `not`")
                ops.append("not in")
            else:
                ops.append(_COMPARE_OPS[tok.kind])
            operands.append(self.parse_expr(BP_COMPARE))
            if self.tok.kind in _COMPARE_OPS or (self.at(T.NOT) and self.peek().kind is T.IN):
                tok = self.advance()
            else:
                tok = None
        return ast.Compare(operands, ops, span=self._full(left).to(self._full(operands[-1])))

    def _parse_index(self, obj: ast.Expr) -> ast.Expr:
        start: ast.Expr | None = None
        stop: ast.Expr | None = None
        step: ast.Expr | None = None
        if not self.at(T.COLON):
            start = self.parse_expr()
            if self.accept(T.RBRACKET):
                return ast.Index(obj, start, span=self._full(obj).to(self.prev.span))
        self.expect(T.COLON, "or `]` in index")
        if not self.at(T.COLON, T.RBRACKET):
            stop = self.parse_expr()
        if self.accept(T.COLON) and not self.at(T.RBRACKET):
            step = self.parse_expr()
        self.expect(T.RBRACKET, "to close the slice")
        return ast.Slice(obj, start, stop, step, span=self._full(obj).to(self.prev.span))

    def parse_args(self, closer: T, opener: Token, what: str) -> list[ast.Expr]:
        args: list[ast.Expr] = []
        self.skip_newlines()
        while not self.at(closer):
            args.append(self.parse_expr())
            self.skip_newlines()
            if not self.accept(T.COMMA):
                break
            self.skip_newlines()
        self.expect_closer(closer, opener, what)
        return args

    def parse_list(self) -> ast.Expr:
        opener = self.advance()  # [
        start = opener.span
        if self.accept(T.RBRACKET):
            return ast.ListLit([], span=self.span_from(start))
        first = self.parse_expr()
        if self.accept(T.FOR):
            targets = self.parse_targets("after `for` in comprehension")
            self.expect(T.IN, "in comprehension")
            iterable = self.parse_expr()
            cond = self.parse_expr() if self.accept(T.IF) else None
            self.expect(T.RBRACKET, "to close the comprehension")
            return ast.Comprehension(first, targets, iterable, cond, span=self.span_from(start))
        items = [first]
        while self.accept(T.COMMA):
            if self.at(T.RBRACKET):
                break
            items.append(self.parse_expr())
        self.expect_closer(T.RBRACKET, opener, "the list")
        return ast.ListLit(items, span=self.span_from(start))

    def parse_dict(self) -> ast.Expr:
        opener = self.advance()  # {
        start = opener.span
        entries: list[tuple[ast.Expr, ast.Expr]] = []
        bare: list[bool] = []
        self.skip_newlines()
        while not self.at(T.RBRACE):
            if self.at(T.IDENT) and self.peek().kind is T.COLON:
                tok = self.advance()
                key: ast.Expr = ast.StringLit([tok.value], span=tok.span)
                bare.append(True)
            else:
                key = self.parse_expr()
                bare.append(False)
            self.expect(T.COLON, "after dictionary key", help_="dictionary entries look like `key: value`")
            self.skip_newlines()
            value = self.parse_expr()
            entries.append((key, value))
            self.skip_newlines()
            if not self.accept(T.COMMA):
                break
            self.skip_newlines()
        self.skip_newlines()
        self.expect_closer(T.RBRACE, opener, "the dictionary")
        return ast.DictLit(entries, bare, span=self.span_from(start))

    def parse_if(self) -> ast.IfExpr:
        start = self.advance().span  # if
        branches: list[tuple[ast.Expr, ast.Block]] = []
        cond = self.parse_expr()
        branches.append((cond, self.parse_block()))
        else_ = None
        while self._skip_newlines_before(T.ELIF, T.ELSE):
            if self.accept(T.ELIF):
                cond = self.parse_expr()
                branches.append((cond, self.parse_block()))
                continue
            self.advance()  # else
            if self.at(T.IF):  # `else if` works too
                nested = self.parse_if()
                branches.extend(nested.branches)
                else_ = nested.else_
            else:
                else_ = self.parse_block()
            break
        return ast.IfExpr(branches, else_, span=self.span_from(start))

    def parse_string(self, tok: Token) -> ast.StringLit:
        raw = tok.text.startswith("'")
        parts: list[str | ast.InterpPart] = []
        for part in tok.value:
            if isinstance(part, str):
                parts.append(part)
                continue
            assert isinstance(part, Interp)
            sub_tokens = Lexer(self.source, part.start, part.end).tokenize()
            sub = Parser(sub_tokens, self.source)
            try:
                sub.skip_newlines()
                expr = sub.parse_expr()
                sub.skip_newlines()
                if not sub.at(T.EOF):
                    raise sub.error_here(f"unexpected {self.describe(sub.tok)} in string interpolation")
            except ParseError as err:
                # The end of the {...} is not the end of the input, so another line won't help
                err.incomplete = False
                raise
            parts.append(ast.InterpPart(expr, part.spec, span=Span(self.source, part.start, part.end)))
        return ast.StringLit(parts, raw, span=tok.span)


def _with_incomplete(err: MultipleErrors, incomplete: bool) -> MultipleErrors:
    err.incomplete = incomplete  # type: ignore[attr-defined]
    return err


def parse(source: Source | str, name: str = "<input>") -> ast.Program:
    if isinstance(source, str):
        source = Source(source, name)
    tokens = Lexer(source).tokenize()
    return Parser(tokens, source).parse_program()


def parse_expression(source: Source | str, name: str = "<input>") -> ast.Expr:
    if isinstance(source, str):
        source = Source(source, name)
    tokens = Lexer(source).tokenize()
    parser = Parser(tokens, source)
    parser.skip_newlines()
    expr = parser.parse_expr()
    parser.skip_newlines()
    if not parser.at(T.EOF):
        raise parser.error_here(f"unexpected {Parser.describe(parser.tok)} after expression")
    return expr
