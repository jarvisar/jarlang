"""The JarLang language server (jarlang lsp)."""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable
from urllib.parse import quote, unquote, urlparse

from . import __version__, ast
from .analysis import analyze, resolve
from .builtins import BUILTINS, CONSTANT_DOCS
from .errors import Diagnostic, JarLangError
from .lexer import Comment, Interp, Lexer
from .parser import Parser
from .resolver import Resolver, Symbol, module_path
from .source import Source, Span
from .tokens import KEYWORDS, T, Token
from .values import Builtin

# Protocol constants

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002
REQUEST_FAILED = -32803

SEVERITY = {"error": 1, "warning": 2, "note": 3}
TAG_UNNECESSARY = 1

# CompletionItemKind
CK_METHOD, CK_FUNCTION, CK_FIELD, CK_VARIABLE = 2, 3, 5, 6
CK_KEYWORD, CK_SNIPPET, CK_CONSTANT, CK_TYPE = 14, 15, 21, 25
# SymbolKind
SK_MODULE, SK_FUNCTION, SK_VARIABLE, SK_CONSTANT = 2, 12, 13, 14

SUPPORTED_ENCODINGS = ("utf-16", "utf-32", "utf-8")

SEMANTIC_TYPES = ["namespace", "function", "parameter", "variable", "property"]
SEMANTIC_MODIFIERS = ["declaration", "readonly", "defaultLibrary"]
_ST = {name: i for i, name in enumerate(SEMANTIC_TYPES)}
_SM = {name: 1 << i for i, name in enumerate(SEMANTIC_MODIFIERS)}


class LspError(Exception):
    """Raised by a handler to answer a request with a JSON-RPC error."""

    def __init__(self, message: str, code: int = REQUEST_FAILED) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Positions

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")


# LSP counts characters in UTF-16 code units by default, so emoji count as two
class LineIndex:
    """Converts between string offsets and LSP line/character positions."""

    def __init__(self, text: str, encoding: str = "utf-16") -> None:
        if encoding not in SUPPORTED_ENCODINGS:
            raise ValueError(f"unsupported position encoding {encoding!r}")
        self.text = text
        self.encoding = encoding
        self.line_starts = [0] + [m.end() for m in _LINE_BREAK.finditer(text)]

    @property
    def line_count(self) -> int:
        return len(self.line_starts)

    def line_of(self, offset: int) -> int:
        offset = max(0, min(offset, len(self.text)))
        return bisect.bisect_right(self.line_starts, offset) - 1

    def line_end(self, line: int) -> int:
        """Offset of the end of a line, before its line break."""
        if line + 1 >= len(self.line_starts):
            return len(self.text)
        end = self.line_starts[line + 1]
        if self.text[end - 1] == "\n":
            end -= 1
            if end > self.line_starts[line] and self.text[end - 1] == "\r":
                end -= 1
        else:  # a lone \r
            end -= 1
        return end

    def units(self, s: str) -> int:
        """Length of s in the position encoding's code units."""
        if self.encoding == "utf-16":
            return len(s) + len(_ASTRAL.findall(s))
        if self.encoding == "utf-8":
            return len(s.encode("utf-8", "surrogatepass"))
        return len(s)

    def position(self, offset: int) -> dict[str, int]:
        offset = max(0, min(offset, len(self.text)))
        line = bisect.bisect_right(self.line_starts, offset) - 1
        start = self.line_starts[line]
        return {"line": line, "character": self.units(self.text[start:offset])}

    def offset(self, position: dict[str, Any]) -> int:
        """Offset for an LSP position (clamped, and snapped to the start of a character)."""
        line = int(position.get("line", 0))
        character = int(position.get("character", 0))
        if line < 0:
            return 0
        if line >= len(self.line_starts):
            return len(self.text)
        start = self.line_starts[line]
        end = self.line_end(line)
        if character <= 0:
            return start
        segment = self.text[start:end]
        if self.units(segment) == len(segment):
            return start + min(character, len(segment))
        count = 0
        for i, ch in enumerate(segment):
            if count >= character:
                return start + i
            count += self.units(ch)
            if count > character:
                return start + i
        return end

    def range(self, start: int, end: int) -> dict[str, dict[str, int]]:
        return {"start": self.position(start), "end": self.position(end)}


# URIs

def uri_to_path(uri: str) -> str | None:
    """Filesystem path of a file: URI (None for other schemes)."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    if os.name == "nt":
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        path = path.replace("/", "\\")
    return path


def path_to_uri(path: str) -> str:
    try:
        return Path(os.path.abspath(path)).as_uri()
    except ValueError:  # pragma: no cover - odd paths
        return "file://" + quote(path.replace("\\", "/"))


# Documentation tables

KEYWORD_DOCS: dict[str, str] = {
    "let": "Declare a variable in the current scope (shadowing any outer variable).\n\n"
           "```jarlang\nlet x = 10\nlet total: int = 0\n```",
    "const": "Declare a constant: assigning to it later is an error.\n\n```jarlang\nconst G = 9.81\n```",
    "fn": "Define a function with a block body, an arrow body, or anonymously.\n\n"
          "```jarlang\nfn area(r) { π * r^2 }\nfn sq(x) => x * x\nxs.map(fn(x) => x + 1)\n```\n\n"
          "Math style works too: `f(x) = x^2 + 1`.",
    "return": "Return a value from the enclosing function. A function's last expression is "
              "returned automatically, so `return` is only needed to leave early.",
    "if": "Conditional. `if` is an expression, so it can produce a value:\n\n"
          "```jarlang\nsign = if x > 0 { 1 } elif x < 0 { -1 } else { 0 }\n```",
    "elif": "Another condition in an `if` chain (`else if` works too).",
    "else": "The fallback branch of an `if`.",
    "while": "Loop while a condition is truthy.\n\n```jarlang\nwhile n > 1 { n = n // 2 }\n```",
    "for": "Loop over a list, range, string or dict (its keys).\n\n"
           "```jarlang\nfor i in 1..10 { print(i) }\nfor i, x in enumerate(xs) { print(i, x) }\n```",
    "in": "Membership test (`x in xs`, `x not in xs`), or the iterable of a `for` loop.",
    "break": "Leave the innermost loop.",
    "continue": "Skip to the next iteration of the innermost loop.",
    "true": "The boolean value true.",
    "false": "The boolean value false.",
    "nil": "The absence of a value (what functions without a result return).",
    "and": "Logical and (short-circuiting). Also written `&&`.",
    "or": "Logical or (short-circuiting). Also written `||`.",
    "not": "Logical negation (also `!x`). `x not in xs` tests non-membership.",
    "try": "Run a block and handle any error it throws in the `catch` block.\n\n"
           "```jarlang\ntry { risky() } catch err { print(\"failed: {err}\") }\n```",
    "catch": "Handle an error thrown inside the `try` block; the name after `catch` holds the error.",
    "throw": "Raise an error (any value). Catch it with `try { } catch e { }`.",
    "import": "Run another JarLang file and use its globals.\n\n"
              "```jarlang\nimport \"lib/geometry.jlang\"\nimport \"lib/geometry.jlang\" as geo\n```",
    "as": "Name the module in an `import`: `import \"lib.jlang\" as lib`.",
}

TYPE_DOCS: dict[str, str] = {
    "int": "integer (arbitrary precision)",
    "float": "floating-point number",
    "num": "any number (int or float)",
    "bool": "true or false",
    "str": "text string",
    "list": "list of values (`list[int]` for a list of integers)",
    "dict": "dictionary (`dict[str, int]`)",
    "fn": "function",
    "any": "any value",
    "nil": "nil (no value)",
}

OPERATOR_DOCS: dict[str, str] = {
    "|>": "Pipeline: `x |> f` is `f(x)`, and `x |> f(a)` is `f(x, a)`.",
    "..": "Inclusive range: `1..5` is 1, 2, 3, 4, 5.",
    "..<": "Exclusive range: `0..<5` is 0, 1, 2, 3, 4.",
    "=>": "Arrow body: `fn sq(x) => x * x`.",
    "->": "Return type annotation: `fn f(x: int) -> int { ... }`.",
    "^": "Power (right-associative): `2^10 == 1024`, `2^3^2 == 2^9`.",
    "**": "Power (same as `^`).",
    "√": "Square root: `√16 == 4`; `2√x` means `2 * √x`.",
    "//": "Floor division: `7 // 2 == 3`.",
    "%": "Remainder: `7 % 3 == 1`.",
    "×": "Multiplication (same as `*`).",
    "·": "Multiplication (same as `*`).",
    "÷": "Division (same as `/`).",
    "−": "Minus (same as `-`).",
    "≤": "Less than or equal (same as `<=`).",
    "≥": "Greater than or equal (same as `>=`).",
    "≠": "Not equal (same as `!=`).",
    "&&": "Logical and (same as `and`).",
    "||": "Logical or (same as `or`).",
}

_CONSTANT_ALIASES = {"π": "pi", "τ": "tau", "φ": "phi", "∞": "inf"}

SNIPPETS: list[tuple[str, str, str]] = [
    ("fn", "fn ${1:name}(${2:params}) {\n\t$0\n}", "function declaration"),
    ("fn =>", "fn ${1:name}(${2:params}) => ${0:expr}", "arrow function"),
    ("if", "if ${1:condition} {\n\t$0\n}", "if statement"),
    ("if else", "if ${1:condition} {\n\t$2\n} else {\n\t$0\n}", "if / else"),
    ("for", "for ${1:x} in ${2:xs} {\n\t$0\n}", "for loop"),
    ("for range", "for ${1:i} in ${2:1}..${3:10} {\n\t$0\n}", "for loop over a range"),
    ("while", "while ${1:condition} {\n\t$0\n}", "while loop"),
    ("try", "try {\n\t$1\n} catch ${2:err} {\n\t$0\n}", "try / catch"),
]

_DID_YOU_MEAN = re.compile(r"did you mean `([^`]+)`\?")
_SPEC_RE = re.compile(r"^(?:(?P<fill>.)?(?P<align>[<>^=]))?(?P<sign>[+\- ])?(?P<alt>#)?(?P<zero>0)?"
                      r"(?P<width>\d+)?(?P<group>[,_])?(?:\.(?P<prec>\d+))?(?P<type>[bcdeEfFgGnosxX%])?$")
_SPEC_TYPES = {
    "b": "binary", "c": "character", "d": "decimal integer", "e": "scientific notation",
    "E": "scientific notation (uppercase)", "f": "fixed-point", "F": "fixed-point",
    "g": "general format", "G": "general format (uppercase)", "n": "number", "o": "octal",
    "s": "string", "x": "hexadecimal", "X": "hexadecimal (uppercase)", "%": "percentage",
}


def is_identifier(name: str) -> bool:
    """True if name is a valid JarLang identifier (not a keyword)."""
    if not name or name in KEYWORDS:
        return False
    if not (name[0] == "_" or name[0].isalpha()):
        return False
    return all(ch == "_" or ch.isalnum() for ch in name)


# Analysis

@dataclass(eq=False)
class Occurrence:
    """A span in the document that names a user symbol."""

    start: int
    end: int
    symbol: Symbol
    decl: bool = False


def _lex(source: Source) -> tuple[list[Token], list[Comment], bool]:
    """Tokenize the source, restarting on the next line after a lexer error."""
    text = source.text
    tokens: list[Token] = []
    comments: list[Comment] = []
    pos = 0
    ok = True
    while True:
        lexer = Lexer(source, pos)
        try:
            tokens.extend(lexer.tokenize())
            comments.extend(lexer.comments)
            return tokens, comments, ok
        except JarLangError as err:
            ok = False
            tokens.extend(lexer.tokens)
            comments.extend(lexer.comments)
            where = err.span.start if err.span is not None else lexer.pos
            nl = text.find("\n", max(where, pos))
            if nl == -1:
                break
            tokens.append(Token(T.NEWLINE, "\n", Span(source, nl, nl + 1), True))
            pos = nl + 1
        except Exception:  # pragma: no cover - lexer bug, keep what we have
            ok = False
            tokens.extend(lexer.tokens)
            break
    tokens.append(Token(T.EOF, None, Span(source, len(text), len(text)), True))
    return tokens, comments, ok


def _recover(source: Source, tokens: list[Token]
             ) -> tuple[ast.Program | None, Resolver | None, list[Diagnostic]]:
    """Parse and resolve the valid statements, skipping broken ones, plus the syntax errors found."""
    errors: list[Diagnostic] = []
    try:
        parser = Parser(tokens, source)
        stmts: list[ast.Stmt] = []
        parser.skip_separators()
        while not parser.at(T.EOF):
            before = parser.pos
            try:
                stmt = parser.parse_statement()
                stmts.append(stmt)
                parser.end_statement()
            except JarLangError as err:
                errors.append(err.diagnostic)
                parser.synchronize()
            except Exception:  # parser bug, skip this statement
                errors.append(Diagnostic("error", "cannot parse this statement", parser.tokens[before].span,
                                         notes=["the JarLang parser crashed here; please report it"]))
                parser.synchronize()
            if parser.pos == before:
                parser.advance()
            parser.skip_separators()
        program = ast.Program(stmts, span=Span(source, 0, len(source.text)))
    except Exception:
        return None, None, errors
    try:
        resolver = resolve(program, source.name)
    except Exception:
        return program, None, errors
    return program, resolver, errors


def _decl_span(sym: Symbol, source: Source) -> tuple[int, int] | None:
    """The span of the name where sym is declared, if it is in this source."""
    span = sym.span
    if span is None or span.source is not source:
        return None
    text = source.text
    start, end = span.start, span.start + len(sym.name)
    if text.startswith(sym.name, start) and not (end < len(text) and (text[end] == "_" or text[end].isalnum())):
        return start, end
    return None


_WORD_KINDS = frozenset({T.IDENT, T.INT, T.FLOAT} | set(KEYWORDS.values()))
# With the cursor at foo|( the user means foo, not the parenthesis
_PUNCTUATION = frozenset({T.LPAREN, T.RPAREN, T.LBRACKET, T.RBRACKET, T.LBRACE, T.RBRACE,
                          T.COMMA, T.DOT, T.SEMI, T.COLON, T.NEWLINE, T.EOF})


def _token_at(tokens: list[Token], starts: list[int], offset: int) -> Token | None:
    i = bisect.bisect_right(starts, offset) - 1
    candidates = [tokens[j] for j in (i, i - 1) if 0 <= j < len(tokens)]
    hit = None
    for tok in candidates:
        if tok.kind not in (T.NEWLINE, T.EOF) and tok.span.start <= offset < tok.span.end:
            hit = tok
            break
    if hit is None or hit.kind in _PUNCTUATION:
        for tok in candidates:  # cursor just after a word
            if tok.kind in _WORD_KINDS and tok.span.end == offset:
                return tok
    return hit


class Analysis:
    """Everything the server knows about one version of a document."""

    def __init__(self, text: str, name: str = "<input>", encoding: str = "utf-16") -> None:
        self.text = text
        self.name = name
        self.source = Source(text, name)
        self.lines = LineIndex(text, encoding)
        self.tokens, self.comments, self.lexed_ok = _lex(self.source)
        self._starts = [t.span.start for t in self.tokens]
        self.internal_error: str | None = None
        try:
            program, resolver, diagnostics = analyze(self.source, self.tokens if self.lexed_ok else None)
        except Exception:
            self.internal_error = traceback.format_exc()
            program, resolver, diagnostics = None, None, []
        self.diagnostics: list[Diagnostic] = diagnostics
        # False if there are syntax errors (program then only has the statements that parsed)
        self.complete = program is not None and resolver is not None
        if not self.complete:
            program, resolver, recovered = _recover(self.source, self.tokens)
            if self.internal_error is not None:
                self.diagnostics = recovered + (resolver.diagnostics if resolver is not None and not recovered else [])
        self.program: ast.Program | None = program
        self.resolver: Resolver | None = resolver
        self._index()

    # Indexing
    def _index(self) -> None:
        self.occurrences: list[Occurrence] = []
        self.functions: list[ast.Function] = []  # pre-order: outer before inner
        self.comprehensions: list[ast.Comprehension] = []
        self.type_refs: list[ast.TypeRef] = []
        self.imports: list[ast.Import] = []
        self.field_names: set[str] = set()
        self.decl_values: dict[int, ast.Expr] = {}  # name start -> assigned value
        self.fn_at: dict[int, ast.Function] = {}  # name start -> function it is bound to
        self.scope_of: dict[int, Any] = {}  # id(Function | Comprehension) -> Scope
        self.scopes: list[Any] = []
        if self.program is None:
            return
        for node in ast.walk(self.program):
            if isinstance(node, ast.Function):
                self.functions.append(node)
            elif isinstance(node, ast.FnDecl):
                start = node.target.span.start
                self.fn_at[start] = node.func
            elif isinstance(node, (ast.Let, ast.Assign)):
                target = node.target
                if isinstance(target, ast.Name) and getattr(node, "op", None) is None:
                    start = target.span.start
                    if node.value is not None:
                        self.decl_values[start] = node.value
                        if isinstance(node.value, ast.Lambda):
                            self.fn_at[start] = node.value.func
                elif isinstance(target, ast.Field):
                    self.field_names.add(target.name)
            elif isinstance(node, ast.Comprehension):
                self.comprehensions.append(node)
            elif isinstance(node, ast.TypeRef):
                self.type_refs.append(node)
            elif isinstance(node, ast.Import):
                self.imports.append(node)
            elif isinstance(node, ast.Field):
                self.field_names.add(node.name)
            elif isinstance(node, ast.DictLit):
                for (key, _value), bare in zip(node.entries, node.bare_keys):
                    if bare and isinstance(key, ast.StringLit):
                        self.field_names.add(key.plain_value)

        res = self.resolver
        if res is None:
            return
        found: dict[tuple[int, int, int], Occurrence] = {}

        def add(start: int, end: int, sym: Symbol, decl: bool = False) -> None:
            key = (start, end, id(sym))
            occ = found.get(key)
            if occ is None:
                found[key] = Occurrence(start, end, sym, decl)
            elif decl:
                occ.decl = True

        scopes: dict[int, Any] = {id(res.global_scope): res.global_scope}
        for ref in res.references:
            if ref.span.source is not self.source:
                continue
            add(ref.span.start, ref.span.end, ref.symbol)
            scope = ref.symbol.scope
            while scope is not None and id(scope) not in scopes:
                scopes[id(scope)] = scope
                scope = scope.parent
        self.scopes = list(scopes.values())
        for scope in self.scopes:
            if scope.node is not None:
                self.scope_of[id(scope.node)] = scope
            for sym in scope.symbols.values():
                decl = _decl_span(sym, self.source)
                if decl is not None:
                    add(decl[0], decl[1], sym, decl=True)
        # Add parameters of functions that have no scope, so hover and rename still work
        for func in self.functions:
            if id(func) in self.scope_of:
                continue
            for p in func.params:
                sym = Symbol(p.name, "parameter", p.span, None)  # type: ignore[arg-type]
                sym.type = p.type
                add(p.span.start, p.span.start + len(p.name), sym, decl=True)
        self.occurrences = sorted(found.values(), key=lambda o: (o.start, o.end))

    # Queries
    def token_at(self, offset: int) -> Token | None:
        """The token at offset, looking inside string interpolations."""
        tok = _token_at(self.tokens, self._starts, offset)
        depth = 0
        while tok is not None and tok.kind is T.STRING and depth < 8:
            part = self.interp_at(tok, offset)
            if part is None:
                break
            lexer = Lexer(self.source, part.start, part.end)
            try:
                sub = lexer.tokenize()
            except JarLangError:
                sub = lexer.tokens
            inner = _token_at(sub, [t.span.start for t in sub], offset)
            if inner is None:
                return tok
            tok = inner
            depth += 1
        return tok

    @staticmethod
    def interp_at(tok: Token, offset: int) -> Interp | None:
        if tok.kind is not T.STRING or not isinstance(tok.value, list):
            return None
        for part in tok.value:
            if isinstance(part, Interp) and part.start <= offset <= part.end:
                return part
        return None

    def occurrence_at(self, offset: int) -> Occurrence | None:
        touching = None
        for occ in self.occurrences:
            if occ.start > offset:
                break
            if offset < occ.end:
                return occ
            if offset == occ.end and touching is None:
                touching = occ
        return touching

    def symbol_at(self, offset: int) -> Symbol | None:
        occ = self.occurrence_at(offset)
        return occ.symbol if occ is not None else None

    def occurrences_of(self, sym: Symbol) -> list[Occurrence]:
        return [o for o in self.occurrences if o.symbol is sym]

    def decl_range(self, sym: Symbol) -> tuple[int, int] | None:
        return _decl_span(sym, self.source)

    def function_for(self, sym: Symbol) -> ast.Function | None:
        """The function sym is bound to, if known."""
        if sym.func is not None:
            return sym.func
        decl = _decl_span(sym, self.source)
        if decl is not None:
            return self.fn_at.get(decl[0])
        return None

    def enclosing_functions(self, offset: int) -> list[ast.Function]:
        return [f for f in self.functions if f.span.start <= offset <= f.span.end]

    def visible_symbols(self, offset: int) -> dict[str, Symbol]:
        """Names in scope at offset (inner declarations win)."""
        out: dict[str, Symbol] = {}
        if self.resolver is not None:
            out.update(self.resolver.global_scope.symbols)
        for func in self.enclosing_functions(offset):
            scope = self.scope_of.get(id(func))
            if scope is not None:
                out.update(scope.symbols)
            else:
                for p in func.params:
                    sym = Symbol(p.name, "parameter", p.span, None)  # type: ignore[arg-type]
                    sym.type = p.type
                    out[p.name] = sym
        for comp in self.comprehensions:
            if comp.span.start <= offset <= comp.span.end:
                scope = self.scope_of.get(id(comp))
                if scope is not None:
                    out.update(scope.symbols)
        return out

    def import_for(self, sym: Symbol) -> ast.Import | None:
        """The import statement for a name from an import without an alias."""
        if sym.span is None:
            return None
        for imp in self.imports:
            if imp.span.start == sym.span.start and imp.alias is None:
                return imp
        return None

    def doc_comment(self, offset: int) -> str:
        """Comments above the line of offset, plus a trailing comment on it."""
        line = self.lines.line_of(offset)
        own: dict[int, str] = {}
        trailing = ""
        for c in self.comments:
            cl = self.lines.line_of(c.span.start)
            start = self.lines.line_starts[cl]
            if self.text[start:c.span.start].strip() == "":
                own[cl] = c.text
            elif cl == line:
                trailing = c.text.strip()
        lines: list[str] = []
        ln = line - 1
        while ln in own:
            lines.append(own[ln])
            ln -= 1
        lines.reverse()
        # Strip the common one-space indent after '#'
        doc = "\n".join(s[1:] if s.startswith(" ") else s for s in lines).strip()
        if trailing:
            doc = f"{doc}\n\n{trailing}" if doc else trailing
        return doc

    def in_type_annotation(self, offset: int) -> ast.TypeRef | None:
        for ref in self.type_refs:
            if ref.span.start <= offset <= ref.span.end:
                return ref
        return None


# Rendering helpers

def render_function(name: str | None, func: ast.Function) -> str:
    """Function header such as fn name(a, b: int = 2) -> int."""
    head = f"fn {name}" if name else "fn"
    return f"{head}({_params_text(func)})" + (f" -> {func.ret_type}" if func.ret_type is not None else "")


def _params_text(func: ast.Function) -> str:
    return ", ".join(_param_text(p) for p in func.params)


def _param_text(p: ast.Param) -> str:
    text = p.name
    if p.type is not None:
        text += f": {p.type}"
    if p.default is not None:
        default = p.default.span.text if p.default.span is not None else "…"
        text += f" = {_shorten(default)}"
    return text


def _shorten(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _show_number(value: float) -> str:
    return repr(value)


def builtin_markdown(name: str) -> str | None:
    value = BUILTINS.get(name)
    if value is None:
        return None
    if isinstance(value, Builtin):
        parts = [f"```jarlang\n{value.signature}\n```"]
        if value.doc:
            parts.append(value.doc)
        parts.append(f"*built-in function · {value.category}*")
        return "\n\n".join(parts)
    if callable(value):  # pragma: no cover - future builtin kinds
        return f"```jarlang\n{name}\n```\n\n*built-in*"
    key = _CONSTANT_ALIASES.get(name, name)
    parts = [f"```jarlang\nconst {name} = {_show_number(value)}\n```"]
    if CONSTANT_DOCS.get(key):
        parts.append(CONSTANT_DOCS[key])
    if key != name:
        parts.append(f"Same as `{key}`.")
    parts.append("*built-in constant*")
    return "\n\n".join(parts)


def _split_signature(label: str) -> list[tuple[int, int]]:
    """Character ranges of the parameters in a name(a, b=1, ...rest) label."""
    open_ = label.find("(")
    if open_ == -1:
        return []
    ranges: list[tuple[int, int]] = []
    depth = 0
    quote = ""
    start = open_ + 1
    for i in range(open_ + 1, len(label)):
        ch = label[i]
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                if label[start:i].strip():
                    ranges.append(_strip_range(label, start, i))
                return ranges
            depth -= 1
        elif ch == "," and depth == 0:
            ranges.append(_strip_range(label, start, i))
            start = i + 1
    return ranges


def _strip_range(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _explain_spec(spec: str) -> str:
    m = _SPEC_RE.match(spec)
    lines = [f"Format spec `{spec}`"]
    if not m:
        return lines[0] + " (uses Python's format mini-language)"
    align = {"<": "left-aligned", ">": "right-aligned", "^": "centered", "=": "padded after the sign"}
    if m.group("align"):
        fill = m.group("fill")
        lines.append(f"- {align[m.group('align')]}" + (f", padded with `{fill}`" if fill else ""))
    if m.group("sign") == "+":
        lines.append("- always show the sign")
    elif m.group("sign") == " ":
        lines.append("- a space before positive numbers")
    if m.group("alt"):
        lines.append("- alternate form (`0x`, `0b`, ... prefixes)")
    if m.group("zero"):
        lines.append("- zero-padded")
    if m.group("width"):
        lines.append(f"- width {m.group('width')}")
    if m.group("group"):
        lines.append(f"- `{m.group('group')}` as the thousands separator")
    if m.group("prec"):
        lines.append(f"- precision {m.group('prec')}")
    if m.group("type"):
        lines.append(f"- {_SPEC_TYPES.get(m.group('type'), m.group('type'))}")
    return "\n".join(lines)


def _number_markdown(tok: Token) -> str:
    text = tok.text
    value = tok.value
    if tok.kind is T.FLOAT:
        return f"```jarlang\n{text}\n```\n\nfloat `{value!r}`"
    lines = [f"```jarlang\n{text}\n```"]
    info = [f"= **{value}**"] if not text.replace("_", "").isdigit() else [f"int **{value}**"]
    if value >= 0 and value.bit_length() <= 128:
        info.append(f"hex `{hex(value)}`")
        if value.bit_length() <= 64:
            info.append(f"binary `{bin(value)}`")
    lines.append(" · ".join(info))
    return "\n\n".join(lines)


def _scan_context(text: str, offset: int) -> tuple[str, int]:
    """Returns ("code", start), ("string", -1) or ("comment", -1) for the offset."""
    # start is 0, or the first character inside a string interpolation
    # Stack entries: ["str", quote] or ["interp", depth, start]
    stack: list[list[Any]] = []
    i = 0
    n = min(offset, len(text))
    while i < n:
        ch = text[i]
        top = stack[-1] if stack else None
        if top is not None and top[0] == "str":
            if ch == "\\":
                i += 2
                continue
            if ch == top[1]:
                stack.pop()
            elif ch == "{" and top[1] == '"':
                stack.append(["interp", 0, i + 1])
            i += 1
            continue
        if ch == "#":
            nl = text.find("\n", i)
            if nl == -1 or nl >= offset:
                return "comment", -1
            i = nl
            continue
        if ch in "\"'":
            stack.append(["str", ch])
        elif top is not None and top[0] == "interp":
            if ch in "([{":
                top[1] += 1
            elif ch in ")]":
                top[1] -= 1
            elif ch == "}":
                if top[1] <= 0:
                    stack.pop()
                else:
                    top[1] -= 1
            elif ch == "\n":  # interpolations can't span lines
                stack.pop()
                if stack:
                    stack.pop()
        i += 1
    if stack and stack[-1][0] == "str":
        return "string", -1
    for entry in reversed(stack):
        if entry[0] == "interp":
            return "code", entry[2]
    return "code", 0


# The server

@dataclass(eq=False)
class Document:
    uri: str
    text: str
    version: int | None = None
    analysis: Analysis | None = None

    @property
    def path(self) -> str | None:
        return uri_to_path(self.uri)

    @property
    def name(self) -> str:
        return self.path or self.uri


class LanguageServer:
    """Handles the JSON-RPC messages for the language server."""

    def __init__(self, send: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.outbox: list[dict[str, Any]] = []
        self._send = send if send is not None else self.outbox.append
        self.documents: dict[str, Document] = {}
        self._last_complete: dict[str, Analysis] = {}
        self._logged: set[str] = set()
        self.initialized = False
        self.shutdown_requested = False
        self.exited = False
        self.encoding = "utf-16"
        self.snippets = True
        self.client_capabilities: dict[str, Any] = {}
        self.requests: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self.on_initialize,
            "shutdown": self.on_shutdown,
            "textDocument/hover": self.on_hover,
            "textDocument/definition": self.on_definition,
            "textDocument/references": self.on_references,
            "textDocument/documentHighlight": self.on_document_highlight,
            "textDocument/prepareRename": self.on_prepare_rename,
            "textDocument/rename": self.on_rename,
            "textDocument/completion": self.on_completion,
            "textDocument/signatureHelp": self.on_signature_help,
            "textDocument/documentSymbol": self.on_document_symbol,
            "textDocument/formatting": self.on_formatting,
            "textDocument/foldingRange": self.on_folding_range,
            "textDocument/codeAction": self.on_code_action,
            "textDocument/semanticTokens/full": self.on_semantic_tokens,
        }
        self.notifications: dict[str, Callable[[dict[str, Any]], None]] = {
            "initialized": lambda params: None,
            "exit": self.on_exit,
            "textDocument/didOpen": self.on_did_open,
            "textDocument/didChange": self.on_did_change,
            "textDocument/didSave": self.on_did_save,
            "textDocument/didClose": self.on_did_close,
        }

    # JSON-RPC
    def send(self, message: dict[str, Any]) -> None:
        self._send(message)

    def notify(self, method: str, params: Any) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def log(self, message: str, type_: int = 3) -> None:
        """Send window/logMessage (type: 1 error, 2 warning, 3 info, 4 log)."""
        self.notify("window/logMessage", {"type": type_, "message": message})

    def _respond(self, id_: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        self.send(message)
        return message

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Handle one incoming message and return the response for requests."""
        if not isinstance(message, dict):
            return self._respond(None, error={"code": INVALID_REQUEST, "message": "invalid request"})
        method = message.get("method")
        if not isinstance(method, str):
            return None  # ignore responses (the server never sends requests)
        params = message.get("params")
        if params is None:
            params = {}
        if "id" not in message:
            self._handle_notification(method, params)
            return None
        return self._handle_request(message["id"], method, params)

    def _handle_notification(self, method: str, params: Any) -> None:
        if method != "exit" and not self.initialized:
            return
        handler = self.notifications.get(method)
        if handler is None:
            return  # ignore unknown notifications
        try:
            handler(params)
        except Exception:
            self.log(f"jarlang-lsp: error handling {method}:\n{traceback.format_exc()}", 1)

    def _handle_request(self, id_: Any, method: str, params: Any) -> dict[str, Any]:
        if not self.initialized and method != "initialize":
            return self._respond(id_, error={"code": SERVER_NOT_INITIALIZED, "message": "server not initialized"})
        if self.shutdown_requested and method != "shutdown":
            return self._respond(id_, error={"code": INVALID_REQUEST, "message": "server is shutting down"})
        handler = self.requests.get(method)
        if handler is None:
            return self._respond(id_, error={"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"})
        try:
            result = handler(params)
        except LspError as err:
            return self._respond(id_, error={"code": err.code, "message": err.message})
        except Exception as exc:
            self.log(f"jarlang-lsp: error handling {method}:\n{traceback.format_exc()}", 1)
            return self._respond(id_, error={"code": INTERNAL_ERROR, "message": f"internal error: {exc}"})
        return self._respond(id_, result)

    # Lifecycle
    def on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        caps = params.get("capabilities") or {}
        self.client_capabilities = caps
        offered = ((caps.get("general") or {}).get("positionEncodings")) or []
        self.encoding = next((e for e in offered if e in SUPPORTED_ENCODINGS), "utf-16")
        options = params.get("initializationOptions") or {}
        if isinstance(options, dict) and options.get("snippets") is False:
            self.snippets = False
        self.initialized = True
        prepare = bool(((caps.get("textDocument") or {}).get("rename") or {}).get("prepareSupport"))
        return {
            "capabilities": {
                "positionEncoding": self.encoding,
                # Incremental changes, so big files aren't sent in full on every keystroke
                "textDocumentSync": {"openClose": True, "change": 2, "save": {"includeText": False}},
                "hoverProvider": True,
                "definitionProvider": True,
                "referencesProvider": True,
                "documentHighlightProvider": True,
                "renameProvider": {"prepareProvider": True} if prepare else True,
                "completionProvider": {"triggerCharacters": ["."], "resolveProvider": False},
                "signatureHelpProvider": {"triggerCharacters": ["(", ","], "retriggerCharacters": [","]},
                "documentSymbolProvider": True,
                "documentFormattingProvider": True,
                "foldingRangeProvider": True,
                "codeActionProvider": {"codeActionKinds": ["quickfix"]},
                "semanticTokensProvider": {
                    "legend": {"tokenTypes": SEMANTIC_TYPES, "tokenModifiers": SEMANTIC_MODIFIERS},
                    "full": True,
                },
            },
            "serverInfo": {"name": "jarlang-lsp", "version": __version__},
        }

    def on_shutdown(self, params: Any) -> None:
        self.shutdown_requested = True
        return None

    def on_exit(self, params: Any) -> None:
        self.exited = True

    @property
    def exit_code(self) -> int:
        return 0 if self.shutdown_requested else 1

    # Documents
    def on_did_open(self, params: dict[str, Any]) -> None:
        item = params["textDocument"]
        doc = Document(item["uri"], item.get("text", ""), item.get("version"))
        self.documents[doc.uri] = doc
        self.publish_diagnostics(doc)

    def on_did_change(self, params: dict[str, Any]) -> None:
        ident = params["textDocument"]
        doc = self.documents.get(ident["uri"])
        if doc is None:
            return
        text = doc.text
        for change in params.get("contentChanges") or []:
            if "range" in change and change["range"] is not None:
                index = LineIndex(text, self.encoding)
                start = index.offset(change["range"]["start"])
                end = index.offset(change["range"]["end"])
                text = text[:start] + change.get("text", "") + text[end:]
            else:
                text = change.get("text", "")
        doc.text = text
        doc.version = ident.get("version", doc.version)
        doc.analysis = None
        self.publish_diagnostics(doc)

    def on_did_save(self, params: dict[str, Any]) -> None:
        uri = params["textDocument"]["uri"]
        doc = self.documents.get(uri)
        if doc is None:
            return
        if isinstance(params.get("text"), str):
            doc.text = params["text"]
        # Other open files may import this one, so check them all again
        for other in self.documents.values():
            other.analysis = None
            self.publish_diagnostics(other)

    def on_did_close(self, params: dict[str, Any]) -> None:
        uri = params["textDocument"]["uri"]
        self.documents.pop(uri, None)
        self._last_complete.pop(uri, None)
        self.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})

    def _doc(self, params: dict[str, Any]) -> Document:
        uri = (params.get("textDocument") or {}).get("uri")
        doc = self.documents.get(uri)
        if doc is None:
            raise LspError(f"unknown document: {uri}", INVALID_PARAMS)
        return doc

    def analysis(self, doc: Document) -> Analysis:
        if doc.analysis is None or doc.analysis.lines.encoding != self.encoding:
            doc.analysis = Analysis(doc.text, doc.name, self.encoding)
            error = doc.analysis.internal_error
            if error and error.strip().splitlines()[-1] not in self._logged:  # once per distinct error
                self._logged.add(error.strip().splitlines()[-1])
                self.log(f"jarlang-lsp: checking {doc.uri} failed (showing recovered results):\n{error}", 1)
            if doc.analysis.complete:
                self._last_complete[doc.uri] = doc.analysis
        return doc.analysis

    def _at(self, params: dict[str, Any]) -> tuple[Document, Analysis, int]:
        doc = self._doc(params)
        a = self.analysis(doc)
        return doc, a, a.lines.offset(params.get("position") or {})

    # Diagnostics
    def publish_diagnostics(self, doc: Document) -> None:
        a = self.analysis(doc)
        diags = [self.lsp_diagnostic(a, d, doc.uri) for d in
                 sorted(a.diagnostics, key=lambda d: d.span.start if d.span is not None else -1)]
        params: dict[str, Any] = {"uri": doc.uri, "diagnostics": diags}
        if doc.version is not None:
            params["version"] = doc.version
        self.notify("textDocument/publishDiagnostics", params)

    @staticmethod
    def _diagnostic_offsets(text: str, span: Span | None) -> tuple[int, int]:
        n = len(text)
        if span is None:
            start = end = 0
        else:
            start = max(0, min(span.start, n))
            end = max(start, min(span.end, n))
        while end > start and text[end - 1] in "\r\n":
            end -= 1
        if end == start:
            if end < n and text[end] not in "\r\n":
                end += 1
            else:  # at the end of a line or file, use the character before it
                j = start
                while j > 0 and text[j - 1] in " \t\r\n":
                    j -= 1
                if j > 0:
                    start, end = j - 1, j
        return start, end

    def lsp_diagnostic(self, a: Analysis, d: Diagnostic, uri: str) -> dict[str, Any]:
        related: list[dict[str, Any]] = []
        if d.span is not None and d.span.source is not a.source:
            # Error is in another file, such as an imported module
            related.append({"location": self._location_in(d.span, uri, a), "message": d.message})
            start = end = 0
        else:
            start, end = self._diagnostic_offsets(a.text, d.span)
        message = d.message
        for note in d.notes:
            message += f"\nnote: {note}"
        for help_ in d.helps:
            message += f"\nhelp: {help_}"
        out: dict[str, Any] = {
            "range": a.lines.range(start, end),
            "severity": SEVERITY.get(d.severity, 1),
            "source": "jarlang",
            "message": message,
        }
        if d.code:
            out["code"] = d.code
        if d.severity == "warning" and (d.message.startswith("unused variable") or d.message == "unreachable code"):
            out["tags"] = [TAG_UNNECESSARY]
        for label in d.secondary:
            related.append({"location": self._location_in(label.span, uri, a),
                            "message": label.message or d.message})
        if related:
            out["relatedInformation"] = related
        for help_ in d.helps:
            m = _DID_YOU_MEAN.search(help_)
            if m:
                out["data"] = {"suggestion": m.group(1)}
                break
        return out

    def _location_in(self, span: Span, uri: str, a: Analysis) -> dict[str, Any]:
        if span.source is a.source:
            start, end = self._diagnostic_offsets(a.text, span)
            return {"uri": uri, "range": a.lines.range(start, end)}
        index = LineIndex(span.source.text, self.encoding)
        return {"uri": path_to_uri(span.source.name), "range": index.range(span.start, span.end)}

    # Hover
    def on_hover(self, params: dict[str, Any]) -> dict[str, Any] | None:
        doc, a, offset = self._at(params)
        tok = a.token_at(offset)
        if tok is None:
            return None
        value = self._hover_markdown(a, tok, offset)
        if not value:
            return None
        start, end = tok.span.start, tok.span.end
        if tok.kind is T.STRING:
            part = self._spec_at(a, tok, offset)
            if part is not None:
                start, end = part.end + 1, part.end + 1 + len(part.spec or "")
        return {"contents": {"kind": "markdown", "value": value}, "range": a.lines.range(start, end)}

    @staticmethod
    def _spec_at(a: Analysis, tok: Token, offset: int) -> Interp | None:
        for part in tok.value if isinstance(tok.value, list) else []:
            if isinstance(part, Interp) and part.spec is not None:
                if part.end < offset <= part.end + 1 + len(part.spec):
                    return part
        return None

    def _hover_markdown(self, a: Analysis, tok: Token, offset: int) -> str | None:
        kind = tok.kind
        if kind is T.IDENT:
            name = tok.value
            ref = a.in_type_annotation(offset)
            if ref is not None and name in TYPE_DOCS:
                return f"```jarlang\n{name}\n```\n\ntype: {TYPE_DOCS[name]}"
            occ = a.occurrence_at(offset)
            if occ is not None and tok.span.start <= occ.start and occ.end <= tok.span.end:
                return self._symbol_markdown(a, occ.symbol)
            if name == "argv":
                return "```jarlang\nargv\n```\n\nThe command-line arguments given after the file name (a list of strings)."
            if name == "as" and KEYWORD_DOCS.get("as"):
                return KEYWORD_DOCS["as"]
            return builtin_markdown(name)
        if kind in (T.INT, T.FLOAT):
            return _number_markdown(tok)
        if kind in (T.NIL, T.FN) and a.in_type_annotation(offset) is not None:
            return f"```jarlang\n{tok.text}\n```\n\ntype: {TYPE_DOCS[tok.text]}"
        if tok.text in KEYWORD_DOCS and kind is not T.IDENT and kind is not T.STRING:
            return f"```jarlang\n{tok.text}\n```\n\n{KEYWORD_DOCS[tok.text]}"
        if kind is T.STRING:
            part = self._spec_at(a, tok, offset)
            if part is not None and part.spec is not None:
                return _explain_spec(part.spec)
            imp = next((i for i in a.imports if i.span.start <= tok.span.start < i.span.end), None)
            if imp is not None:
                full = module_path(imp.path, a.name if os.path.isabs(a.name) else None)
                exists = "" if os.path.exists(full) else " (not found)"
                return f"module `{full}`{exists}"
            return None
        if kind is T.BANG:
            prev = self._token_before(a, tok)
            if prev is not None and not tok.spaced and prev.kind in (T.IDENT, T.INT, T.FLOAT, T.RPAREN, T.RBRACKET):
                return "Factorial: `5! == 120` (floats use the gamma function)."
            return "Logical not (same as `not`)."
        return OPERATOR_DOCS.get(tok.text)

    @staticmethod
    def _token_before(a: Analysis, tok: Token) -> Token | None:
        i = bisect.bisect_left(a._starts, tok.span.start)
        return a.tokens[i - 1] if i > 0 else None

    def _symbol_markdown(self, a: Analysis, sym: Symbol) -> str:
        name = sym.name
        func = a.function_for(sym)
        decl = a.decl_range(sym)
        parts: list[str] = []
        scope_kind = sym.scope.kind if sym.scope is not None else "function"
        where = ""
        if sym.kind == "function" and func is not None:
            code = render_function(name, func)
        elif sym.kind == "parameter":
            param = self._param_for(a, sym)
            code = f"(parameter) {_param_text(param) if param is not None else name}"
            if param is None and sym.type is not None:
                code += f": {sym.type}"
        else:
            label = {"constant": "const", "function": "fn"}.get(sym.kind)
            if label is None:
                label = "(global variable)" if scope_kind == "global" else "(local variable)"
            code = f"{label} {name}"
            if sym.type is not None:
                code += f": {sym.type}"
            value = a.decl_values.get(decl[0]) if decl is not None else None
            if func is not None and isinstance(value, ast.Lambda):
                code += f" = {render_function(None, func)}"
            elif value is not None and value.span is not None and "\n" not in value.span.text \
                    and len(value.span.text) <= 60:
                code += f" = {value.span.text}"
        parts.append(f"```jarlang\n{code}\n```")
        if decl is not None:
            doc = a.doc_comment(decl[0]) if sym.kind != "parameter" else ""
            if doc:
                parts.append(doc)
            line = a.lines.line_of(decl[0]) + 1
            owner = self._owner_name(a, sym)
            where = f"declared on line {line}" + (f" in `{owner}`" if owner else "")
        elif sym.span is None:
            where = "predefined global"
        else:
            imp = a.import_for(sym)
            if imp is not None:
                where = f"imported from `\"{imp.path}\"`"
        if where:
            parts.append(f"*{where}*")
        return "\n\n".join(parts)

    @staticmethod
    def _param_for(a: Analysis, sym: Symbol) -> ast.Param | None:
        if sym.span is None:
            return None
        for func in a.functions:
            for p in func.params:
                if p.span.start == sym.span.start:
                    return p
        return None

    @staticmethod
    def _owner_name(a: Analysis, sym: Symbol) -> str | None:
        scope = sym.scope
        node = scope.node if scope is not None else None
        if node is None and sym.kind == "parameter" and sym.span is not None:
            for func in a.functions:
                if any(p.span.start == sym.span.start for p in func.params):
                    node = func
                    break
        if isinstance(node, ast.Function):
            return node.name or "anonymous function"
        if isinstance(node, ast.Comprehension):
            return "comprehension"
        return None

    # Navigation
    def on_definition(self, params: dict[str, Any]) -> Any:
        doc, a, offset = self._at(params)
        tok = a.token_at(offset)
        if tok is not None and tok.kind is T.STRING:
            imp = next((i for i in a.imports if i.span.start <= tok.span.start < i.span.end), None)
            if imp is not None:
                target = self._module_file(a, imp)
                if target is not None:
                    return {"uri": path_to_uri(target), "range": LineIndex("").range(0, 0)}
            return None
        sym = a.symbol_at(offset)
        if sym is None:
            return None
        decl = a.decl_range(sym)
        if decl is not None:
            return {"uri": doc.uri, "range": a.lines.range(*decl)}
        imp = a.import_for(sym)
        if imp is not None:
            return self._definition_in_module(a, imp, sym.name)
        return None

    def _module_file(self, a: Analysis, imp: ast.Import) -> str | None:
        full = module_path(imp.path, a.name if os.path.isabs(a.name) else None)
        return full if os.path.exists(full) else None

    def _definition_in_module(self, a: Analysis, imp: ast.Import, name: str) -> dict[str, Any] | None:
        path = self._module_file(a, imp)
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read()
            from .parser import parse
            source = Source(text, path)
            program = parse(source)
            resolver = Resolver()
            resolver._collect(program.stmts, resolver.global_scope)
        except Exception:
            return {"uri": path_to_uri(path), "range": LineIndex("").range(0, 0)}
        sym = resolver.global_scope.symbols.get(name)
        index = LineIndex(text, self.encoding)
        if sym is None or sym.span is None:
            return {"uri": path_to_uri(path), "range": index.range(0, 0)}
        decl = _decl_span(sym, source) or (sym.span.start, sym.span.end)
        return {"uri": path_to_uri(path), "range": index.range(*decl)}

    def on_references(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc, a, offset = self._at(params)
        sym = a.symbol_at(offset)
        if sym is None:
            return []
        include_decl = bool((params.get("context") or {}).get("includeDeclaration", True))
        return [{"uri": doc.uri, "range": a.lines.range(o.start, o.end)}
                for o in a.occurrences_of(sym) if include_decl or not o.decl]

    def on_document_highlight(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc, a, offset = self._at(params)
        sym = a.symbol_at(offset)
        if sym is None:
            return []
        return [{"range": a.lines.range(o.start, o.end), "kind": 3 if o.decl else 2}
                for o in a.occurrences_of(sym)]

    # Rename
    def _renamable(self, a: Analysis, offset: int) -> tuple[Symbol, Occurrence]:
        tok = a.token_at(offset)
        occ = a.occurrence_at(offset)
        if occ is not None and tok is not None and not (tok.span.start <= occ.start and occ.end <= tok.span.end):
            occ = None
        if occ is None or tok is None or tok.kind is not T.IDENT:
            if tok is not None and tok.kind in KEYWORDS.values():
                raise LspError(f"`{tok.text}` is a keyword and cannot be renamed")
            if tok is not None and tok.kind is T.IDENT and tok.value in BUILTINS:
                raise LspError(f"`{tok.text}` is a built-in and cannot be renamed")
            raise LspError("nothing to rename here: place the cursor on a variable or function name")
        sym = occ.symbol
        if a.decl_range(sym) is None:
            if a.import_for(sym) is not None:
                raise LspError(f"`{sym.name}` is defined in an imported file; rename it there")
            raise LspError(f"`{sym.name}` is predefined and cannot be renamed")
        return sym, occ

    def on_prepare_rename(self, params: dict[str, Any]) -> dict[str, Any] | None:
        doc, a, offset = self._at(params)
        sym, occ = self._renamable(a, offset)
        return {"range": a.lines.range(occ.start, occ.end), "placeholder": sym.name}

    def on_rename(self, params: dict[str, Any]) -> dict[str, Any]:
        doc, a, offset = self._at(params)
        new_name = str(params.get("newName", ""))
        sym, _ = self._renamable(a, offset)
        if new_name in KEYWORDS:
            raise LspError(f"`{new_name}` is a keyword", INVALID_PARAMS)
        if not is_identifier(new_name):
            raise LspError(f"`{new_name}` is not a valid name", INVALID_PARAMS)
        if new_name != sym.name and sym.scope is not None and new_name in sym.scope.symbols:
            raise LspError(f"`{new_name}` is already defined in this scope")
        return {"changes": {doc.uri: self._rename_edits(a, sym, new_name)}}

    @staticmethod
    def _rename_edits(a: Analysis, sym: Symbol, new_name: str) -> list[dict[str, Any]]:
        seen: set[tuple[int, int]] = set()
        edits = []
        for occ in a.occurrences_of(sym):
            if (occ.start, occ.end) in seen:
                continue
            seen.add((occ.start, occ.end))
            edits.append({"range": a.lines.range(occ.start, occ.end), "newText": new_name})
        return edits

    # Completion
    def on_completion(self, params: dict[str, Any]) -> dict[str, Any]:
        doc, a, offset = self._at(params)
        context, _ = _scan_context(a.text, offset)
        if context != "code":
            return {"isIncomplete": False, "items": []}
        line_start = a.lines.line_starts[a.lines.line_of(offset)]
        before = a.text[line_start:offset]
        if re.search(r"(?:^|[^.])\.[^\W\d]?\w*$", before):
            m = re.search(r"(\S*?)\.\w*$", before)
            receiver = m.group(1) if m else ""
            after_value = bool(receiver) and not receiver.isdigit() and re.search(r"[\w)\]\"'}]$", receiver)
            continued = not receiver and before.strip().startswith(".")  # `.method` on a continuation line
            if after_value or continued:
                return {"isIncomplete": False, "items": self._method_items(doc, a, offset)}
            if receiver.isdigit() or not receiver:
                return {"isIncomplete": False, "items": []}
        if re.search(r"(->\s*|\b(?:let|const)\s+[^\W\d]\w*\s*:\s*|\bfn\b[^)]*[(,]\s*[^\W\d]\w*\s*:\s*)[^\W\d]?\w*$",
                     before):
            return {"isIncomplete": False, "items": self._type_items()}
        return {"isIncomplete": False, "items": self._general_items(doc, a, offset)}

    def _user_items(self, doc: Document, a: Analysis, offset: int) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}

        def add_symbol(name: str, sym: Symbol, analysis: Analysis, rank: str) -> None:
            if name in items or name.startswith("<"):
                return
            func = analysis.function_for(sym)
            if sym.kind == "function" or func is not None:
                kind, detail = CK_FUNCTION, render_function(name, func) if func is not None else f"fn {name}"
            elif sym.kind == "constant":
                kind, detail = CK_CONSTANT, "constant"
            elif sym.kind == "parameter":
                kind, detail = CK_VARIABLE, "parameter" + (f": {sym.type}" if sym.type is not None else "")
            else:
                kind, detail = CK_VARIABLE, "variable" + (f": {sym.type}" if sym.type is not None else "")
            items[name] = {"label": name, "kind": kind, "detail": detail, "sortText": f"{rank}{name}"}

        visible = a.visible_symbols(offset)
        globals_ = a.resolver.global_scope.symbols if a.resolver is not None else {}
        for name, sym in visible.items():
            add_symbol(name, sym, a, "1" if globals_.get(name) is sym else "0")
        if not a.complete:
            last = self._last_complete.get(doc.uri)
            if last is not None and last.resolver is not None:
                for name, sym in last.resolver.global_scope.symbols.items():
                    add_symbol(name, sym, last, "1")
            # Add other names in the file (the function being edited may not parse yet)
            word = re.search(r"[^\W\d]\w*$", a.text[:offset])
            current = word.group(0) if word else ""
            for m in re.finditer(r"[^\W\d]\w*", _strip_strings_and_comments(a.text)):
                name = m.group(0)
                if name in items or name in KEYWORDS or name in BUILTINS:
                    continue
                if name == current and m.end() == offset:
                    continue
                items[name] = {"label": name, "kind": CK_VARIABLE, "detail": "name in this file",
                               "sortText": f"2{name}"}
        return items

    def _general_items(self, doc: Document, a: Analysis, offset: int) -> list[dict[str, Any]]:
        items = self._user_items(doc, a, offset)
        for item in _builtin_items():
            items.setdefault(item["label"], item)
        for kw in KEYWORDS:
            items.setdefault(kw, {"label": kw, "kind": CK_KEYWORD, "sortText": f"4{kw}"})
        result = list(items.values())
        if self.snippets:
            for label, body, detail in SNIPPETS:
                result.append({"label": label, "kind": CK_SNIPPET, "detail": detail,
                               "insertText": body, "insertTextFormat": 2,
                               "filterText": label.split()[0], "sortText": f"5{label}"})
        return result

    def _method_items(self, doc: Document, a: Analysis, offset: int) -> list[dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        for name in sorted(a.field_names):
            items[name] = {"label": name, "kind": CK_FIELD, "detail": "field", "sortText": f"0{name}"}
        for name, sym in a.visible_symbols(offset).items():
            func = a.function_for(sym)
            if func is not None and func.params and name not in items:
                items[name] = {"label": name, "kind": CK_METHOD, "detail": render_function(name, func),
                               "sortText": f"1{name}"}
        for name, value in BUILTINS.items():
            if not isinstance(value, Builtin) or name in items:
                continue
            if value.max_args is not None and value.max_args < 1:
                continue
            items[name] = {"label": name, "kind": CK_METHOD, "detail": value.signature,
                           "documentation": {"kind": "markdown", "value": builtin_markdown(name) or ""},
                           "sortText": f"2{name}"}
        return list(items.values())

    @staticmethod
    def _type_items() -> list[dict[str, Any]]:
        return [{"label": name, "kind": CK_TYPE, "detail": doc} for name, doc in TYPE_DOCS.items()]

    # Signature help
    def on_signature_help(self, params: dict[str, Any]) -> dict[str, Any] | None:
        doc, a, offset = self._at(params)
        context, start = _scan_context(a.text, offset)
        if context != "code":
            return None
        if start == 0:
            # Reuse the document's tokens instead of lexing everything before the cursor again
            tokens = a.tokens[:bisect.bisect_left(a._starts, offset)]
        else:  # inside a string interpolation
            lexer = Lexer(Source(a.text[:offset], a.name), start, offset)
            try:
                tokens = lexer.tokenize()
            except JarLangError:
                tokens = lexer.tokens
        stack: list[list[Any]] = []  # [index of opener, commas]
        for i, tok in enumerate(tokens):
            if tok.kind in (T.LPAREN, T.LBRACKET, T.LBRACE):
                stack.append([i, 0])
            elif tok.kind in (T.RPAREN, T.RBRACKET, T.RBRACE):
                if stack:
                    stack.pop()
            elif tok.kind is T.COMMA and stack:
                stack[-1][1] += 1
        for opener, commas in reversed(stack):
            if tokens[opener].kind is not T.LPAREN or opener == 0:
                continue
            callee = tokens[opener - 1]
            if callee.kind is not T.IDENT:
                continue
            before = tokens[opener - 2] if opener >= 2 else None
            if before is not None and before.kind is T.FN:
                return None  # parameter list of a declaration, not a call
            shift = 1 if before is not None and before.kind in (T.DOT, T.PIPE_GT) else 0
            info = self._signature(doc, a, callee, offset)
            if info is None:
                return None
            label, doc_md, params_ranges, variadic = info
            active = commas + shift
            if variadic and params_ranges and active >= len(params_ranges):
                active = len(params_ranges) - 1
            parameters = [{"label": [a.lines.units(label[:s]), a.lines.units(label[:e])]} for s, e in params_ranges]
            signature: dict[str, Any] = {"label": label, "parameters": parameters, "activeParameter": active}
            if doc_md:
                signature["documentation"] = {"kind": "markdown", "value": doc_md}
            return {"signatures": [signature], "activeSignature": 0, "activeParameter": active}
        return None

    def _signature(self, doc: Document, a: Analysis, callee: Token,
                   offset: int) -> tuple[str, str, list[tuple[int, int]], bool] | None:
        name = callee.value
        sym = a.symbol_at(callee.span.start)
        if sym is None or sym.name != name:
            sym = a.visible_symbols(offset).get(name)
        if sym is None and not a.complete:
            last = self._last_complete.get(doc.uri)
            if last is not None and last.resolver is not None:
                gsym = last.resolver.global_scope.symbols.get(name)
                if gsym is not None:
                    func = last.function_for(gsym)
                    if func is not None:
                        return self._user_signature(last, gsym, func)
        if sym is not None:
            func = a.function_for(sym)
            if func is None:
                return None
            return self._user_signature(a, sym, func)
        value = BUILTINS.get(name)
        if isinstance(value, Builtin):
            label = value.signature
            ranges = _split_signature(label)
            variadic = bool(ranges) and label[ranges[-1][0]:ranges[-1][1]].startswith("...")
            return label, value.doc, ranges, variadic
        return None

    @staticmethod
    def _user_signature(a: Analysis, sym: Symbol, func: ast.Function) -> tuple[str, str, list[tuple[int, int]], bool]:
        label = f"{sym.name}("
        ranges = []
        for i, p in enumerate(func.params):
            if i:
                label += ", "
            text = _param_text(p)
            ranges.append((len(label), len(label) + len(text)))
            label += text
        label += ")"
        if func.ret_type is not None:
            label += f" -> {func.ret_type}"
        decl = a.decl_range(sym)
        doc = a.doc_comment(decl[0]) if decl is not None else ""
        return label, doc, ranges, False

    # Document symbols
    def on_document_symbol(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc = self._doc(params)
        a = self.analysis(doc)
        if a.program is None:
            return []
        return self._symbols(a, a.program.stmts, top=True)

    def _symbols(self, a: Analysis, stmts: list[ast.Stmt], top: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_globals: set[str] = set()

        def entry(name: str, kind: int, node: ast.Node, name_span: Span, detail: str = "",
                  children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
            start = min(node.span.start, name_span.start)
            end = max(node.span.end, name_span.end)
            item: dict[str, Any] = {"name": name, "kind": kind, "range": a.lines.range(start, end),
                                    "selectionRange": a.lines.range(name_span.start, name_span.end)}
            if detail:
                item["detail"] = detail
            if children:
                item["children"] = children
            return item

        for stmt in stmts:
            if isinstance(stmt, ast.FnDecl):
                children = self._symbols(a, stmt.func.body.stmts, top=False)
                out.append(entry(stmt.target.name, SK_FUNCTION, stmt, stmt.target.span,
                                 render_function(stmt.target.name, stmt.func), children))
                continue
            if isinstance(stmt, (ast.Let, ast.Assign)):
                targets: list[ast.Name] = []
                if isinstance(stmt.target, ast.Name):
                    targets = [stmt.target]
                elif isinstance(stmt.target, ast.ListLit) and top:
                    targets = [t for t in stmt.target.items if isinstance(t, ast.Name)]
                value = stmt.value
                for target in targets:
                    if isinstance(value, ast.Lambda) and len(targets) == 1:
                        children = self._symbols(a, value.func.body.stmts, top=False)
                        out.append(entry(target.name, SK_FUNCTION, stmt, target.span,
                                         render_function(None, value.func), children))
                        continue
                    if not top:
                        continue
                    if isinstance(stmt, ast.Assign):
                        sym = a.symbol_at(target.span.start)
                        decl = a.decl_range(sym) if sym is not None else None
                        if target.name in seen_globals or (decl is not None and decl[0] != target.span.start):
                            continue
                    seen_globals.add(target.name)
                    const = isinstance(stmt, ast.Let) and stmt.const
                    detail = ""
                    if isinstance(stmt, ast.Let) and stmt.type is not None:
                        detail = str(stmt.type)
                    elif value is not None and len(targets) == 1 and value.span is not None:
                        detail = _shorten(value.span.text, 40)
                    out.append(entry(target.name, SK_CONSTANT if const else SK_VARIABLE, stmt, target.span, detail))
                continue
            if isinstance(stmt, ast.Import) and stmt.alias is not None and top:
                out.append(entry(stmt.alias.name, SK_MODULE, stmt, stmt.alias.span, f'"{stmt.path}"'))
                continue
            # Functions declared inside control flow
            for block in _blocks_of(stmt):
                out.extend(self._symbols(a, block.stmts, top=False))
        return out

    # Formatting
    def on_formatting(self, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        from .formatter import format_source

        doc = self._doc(params)
        a = self.analysis(doc)
        try:
            formatted = format_source(Source(doc.text, doc.name))
        except JarLangError:
            return None  # syntax errors are already shown as diagnostics
        except Exception:  # log formatter bugs instead of showing an error on every save
            error = traceback.format_exc()
            if error.strip().splitlines()[-1] not in self._logged:
                self._logged.add(error.strip().splitlines()[-1])
                self.log(f"jarlang-lsp: formatting {doc.uri} failed:\n{error}", 1)
            return None
        if formatted == doc.text:
            return []
        return [{"range": a.lines.range(0, len(doc.text)), "newText": formatted}]

    # Folding
    def on_folding_range(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc = self._doc(params)
        a = self.analysis(doc)
        ranges: list[dict[str, Any]] = []
        seen: set[int] = set()
        stack: list[Token] = []
        openers = {T.LBRACE: T.RBRACE, T.LBRACKET: T.RBRACKET, T.LPAREN: T.RPAREN}
        for tok in a.tokens:
            if tok.kind in openers:
                stack.append(tok)
            elif tok.kind in (T.RBRACE, T.RBRACKET, T.RPAREN) and stack:
                opener = stack.pop()
                start_line = a.lines.line_of(opener.span.start)
                end_line = a.lines.line_of(tok.span.start) - 1
                if end_line > start_line and start_line not in seen:
                    seen.add(start_line)
                    ranges.append({"startLine": start_line, "endLine": end_line})
        # Fold runs of comment lines and "# region" / "# endregion" markers
        run_start = run_end = -1
        regions: list[int] = []
        for c in a.comments:
            line = a.lines.line_of(c.span.start)
            if a.text[a.lines.line_starts[line]:c.span.start].strip():
                continue  # trailing comment
            marker = c.text.strip().lower()
            if marker.startswith("region"):
                regions.append(line)
            elif marker.startswith("endregion") and regions:
                start = regions.pop()
                if line > start:
                    ranges.append({"startLine": start, "endLine": line, "kind": "region"})
            if line == run_end + 1 and run_start >= 0:
                run_end = line
            else:
                if run_end > run_start >= 0:
                    ranges.append({"startLine": run_start, "endLine": run_end, "kind": "comment"})
                run_start = run_end = line
        if run_end > run_start >= 0:
            ranges.append({"startLine": run_start, "endLine": run_end, "kind": "comment"})
        return ranges

    # Code actions
    def on_code_action(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        doc = self._doc(params)
        context = params.get("context") or {}
        only = context.get("only")
        if only and not any(k in ("", "quickfix") for k in only):
            return []
        a = self.analysis(doc)
        diagnostics = context.get("diagnostics")
        if not diagnostics:
            want = params.get("range")
            ours = [self.lsp_diagnostic(a, d, doc.uri) for d in a.diagnostics]
            if want:
                lo, hi = a.lines.offset(want["start"]), a.lines.offset(want["end"])
                diagnostics = [d for d in ours if a.lines.offset(d["range"]["start"]) <= hi
                               and a.lines.offset(d["range"]["end"]) >= lo]
            else:
                diagnostics = ours
        actions: list[dict[str, Any]] = []
        for diag in diagnostics:
            if diag.get("source", "jarlang") != "jarlang":
                continue
            message = diag.get("message", "")
            data = diag.get("data") if isinstance(diag.get("data"), dict) else {}
            suggestion = data.get("suggestion")
            if not suggestion:
                m = _DID_YOU_MEAN.search(message)
                suggestion = m.group(1) if m else None
            if suggestion:
                actions.append({
                    "title": f"Change to `{suggestion}`",
                    "kind": "quickfix",
                    "diagnostics": [diag],
                    "isPreferred": True,
                    "edit": {"changes": {doc.uri: [{"range": diag["range"], "newText": suggestion}]}},
                })
                continue
            m = re.match(r"unused variable `([^`]+)`", message)
            if m:
                sym = a.symbol_at(a.lines.offset(diag["range"]["start"]))
                new_name = "_" + m.group(1)
                if sym is not None and sym.name == m.group(1) and a.decl_range(sym) is not None \
                        and (sym.scope is None or new_name not in sym.scope.symbols):
                    actions.append({
                        "title": f"Prefix `{sym.name}` with an underscore",
                        "kind": "quickfix",
                        "diagnostics": [diag],
                        "edit": {"changes": {doc.uri: self._rename_edits(a, sym, new_name)}},
                    })
        return actions


    # Semantic tokens
    def on_semantic_tokens(self, params: dict[str, Any]) -> dict[str, Any]:
        """Color the names the grammar can't (parameters, constants, functions, built-ins, fields)."""
        doc = self._doc(params)
        a = self.analysis(doc)
        entries: dict[int, tuple[int, int, int]] = {}  # start -> (end, type, modifiers)
        alias_starts = {imp.alias.span.start for imp in a.imports if imp.alias is not None}
        for occ in a.occurrences:
            sym = occ.symbol
            decl = a.decl_range(sym)
            modifiers = _SM["declaration"] if occ.decl else 0
            if decl is not None and decl[0] in alias_starts:
                kind = "namespace"
            elif sym.kind == "function" or a.function_for(sym) is not None:
                kind = "function"
            elif sym.kind == "parameter":
                kind = "parameter"
            else:
                kind = "variable"
                if sym.kind == "constant":
                    modifiers |= _SM["readonly"]
            entries[occ.start] = (occ.end, _ST[kind], modifiers)
        for tokens in self._token_streams(a):
            for i, tok in enumerate(tokens):
                if tok.kind is not T.IDENT or tok.span.start in entries:
                    continue
                prev = tokens[i - 1] if i > 0 else None
                nxt = tokens[i + 1] if i + 1 < len(tokens) else None
                after_dot = prev is not None and prev.kind is T.DOT
                value = BUILTINS.get(tok.value)
                if after_dot and not (nxt is not None and nxt.kind is T.LPAREN and not nxt.spaced):
                    entries[tok.span.start] = (tok.span.end, _ST["property"], 0)
                elif isinstance(value, Builtin):
                    entries[tok.span.start] = (tok.span.end, _ST["function"], _SM["defaultLibrary"])
                elif value is not None and not after_dot:
                    entries[tok.span.start] = (tok.span.end, _ST["variable"], _SM["readonly"] | _SM["defaultLibrary"])
        data: list[int] = []
        prev_line = prev_char = 0
        for start in sorted(entries):
            end, kind, modifiers = entries[start]
            position = a.lines.position(start)
            line, char = position["line"], position["character"]
            length = a.lines.units(a.text[start:end])
            if length <= 0 or "\n" in a.text[start:end]:
                continue
            data += [line - prev_line, char - prev_char if line == prev_line else char, length, kind, modifiers]
            prev_line, prev_char = line, char
        return {"data": data}

    @staticmethod
    def _token_streams(a: Analysis) -> list[list[Token]]:
        """The document's tokens plus the tokens of every string interpolation."""
        streams = [a.tokens]
        pending = [t for t in a.tokens if t.kind is T.STRING]
        while pending:
            tok = pending.pop()
            for part in tok.value if isinstance(tok.value, list) else []:
                if isinstance(part, Interp):
                    lexer = Lexer(a.source, part.start, part.end)
                    try:
                        sub = lexer.tokenize()
                    except JarLangError:
                        sub = lexer.tokens
                    streams.append(sub)
                    pending.extend(t for t in sub if t.kind is T.STRING)
        return streams


def _blocks_of(stmt: ast.Stmt) -> list[ast.Block]:
    if isinstance(stmt, (ast.While, ast.For)):
        return [stmt.body]
    if isinstance(stmt, ast.Try):
        return [stmt.body, stmt.handler]
    if isinstance(stmt, ast.ExprStmt):
        expr = stmt.expr
        if isinstance(expr, ast.IfExpr):
            blocks = [block for _, block in expr.branches]
            if expr.else_ is not None:
                blocks.append(expr.else_)
            return blocks
    return []


def _strip_strings_and_comments(text: str) -> str:
    """Blank out comments and the literal parts of strings (keeping interpolations)."""
    out = list(text)
    stack: list[list[Any]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        top = stack[-1] if stack else None
        if top is not None and top[0] == "str":
            if ch == "\\":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
                continue
            if ch == top[1]:
                stack.pop()
            elif ch == "{" and top[1] == '"':
                stack.append(["interp", 0])
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch in "\"'":
            stack.append(["str", ch])
            out[i] = " "
        elif top is not None and top[0] == "interp":
            if ch in "([{":
                top[1] += 1
            elif ch in ")]":
                top[1] -= 1
            elif ch == "}":
                if top[1] <= 0:
                    stack.pop()
                    out[i] = " "
                else:
                    top[1] -= 1
        i += 1
    return "".join(out)


_BUILTIN_ITEMS: list[dict[str, Any]] | None = None


def _builtin_items() -> list[dict[str, Any]]:
    global _BUILTIN_ITEMS
    if _BUILTIN_ITEMS is None:
        items = []
        for name, value in BUILTINS.items():
            doc = builtin_markdown(name) or ""
            if isinstance(value, Builtin):
                items.append({"label": name, "kind": CK_FUNCTION, "detail": value.signature,
                              "documentation": {"kind": "markdown", "value": doc}, "sortText": f"3{name}"})
            elif isinstance(value, (int, float)):
                items.append({"label": name, "kind": CK_CONSTANT, "detail": f"const {name} = {_show_number(value)}",
                              "documentation": {"kind": "markdown", "value": doc}, "sortText": f"3{name}"})
        _BUILTIN_ITEMS = items
    return _BUILTIN_ITEMS


# Stdio transport

def read_message(stream: BinaryIO) -> bytes | None:
    """Read one message body (after its Content-Length header), or None at end of input."""
    length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is None:
                continue  # stray blank line between messages
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                length = None
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return body


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    stream.flush()


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Run the server until exit. Returns 0 if shutdown was requested first, otherwise 1."""
    real_stdout = None
    if stdin is None:
        stdin = sys.stdin.buffer
    if stdout is None:
        stdout = sys.stdout.buffer
        # Send stray prints to stderr so they don't break the protocol
        real_stdout, sys.stdout = sys.stdout, sys.stderr
    out = stdout
    server = LanguageServer(send=lambda message: write_message(out, message))
    try:
        while not server.exited:
            try:
                body = read_message(stdin)
            except (OSError, ValueError):
                break
            if body is None:
                break
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                server.send({"jsonrpc": "2.0", "id": None,
                             "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
                continue
            if isinstance(message, list):
                for item in message:
                    server.handle_message(item)
            else:
                server.handle_message(message)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except BrokenPipeError:  # pragma: no cover - client went away
        pass
    finally:
        if real_stdout is not None:
            sys.stdout = real_stdout
    return server.exit_code


__all__ = ["LanguageServer", "LineIndex", "Analysis", "serve", "read_message", "write_message",
           "uri_to_path", "path_to_uri", "is_identifier"]
