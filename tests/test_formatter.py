"""Tests for the formatter (jarlang fmt)."""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from jarlang import ast
from jarlang.errors import JarLangError
from jarlang.formatter import format_code, format_source
from jarlang.lexer import Lexer
from jarlang.parser import parse
from jarlang.source import Source

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


# Helpers

def dumps(text: str) -> list[str]:
    return [ast.dump(s) for s in parse(text).stmts]


def comment_texts(text: str) -> Counter[str]:
    lexer = Lexer(Source(text))
    lexer.tokenize()
    return Counter(c.text.strip() for c in lexer.comments)


# Format text and check that the tree and comments don't change
# and that formatting again gives the same result
def check(text: str, *, whitespace: bool = True) -> str:
    out = format_code(text)
    assert dumps(out) == dumps(text), f"AST changed:\n--- input\n{text}\n--- output\n{out}"
    again = format_code(out)
    assert again == out, f"not idempotent:\n--- first\n{out}\n--- second\n{again}"
    assert comment_texts(out) == comment_texts(text), f"comments changed:\n{text}\n---\n{out}"
    if out:
        assert out.endswith("\n") and not out.endswith("\n\n")
        assert "\r" not in out
    if whitespace:  # multi-line strings can keep trailing spaces
        assert all(line == line.rstrip() for line in out.split("\n")), out
    return out


def fmt(text: str) -> str:
    return check(text)


# Exact output for small snippets

EXACT = [
    # Spacing and operators
    ("spacing", "x=1+2*3-4/5//6%7\n", "x = 1 + 2 * 3 - 4 / 5 // 6 % 7\n"),
    ("power-tight", "y = x ^ 2 + x**3\n", "y = x^2 + x^3\n"),
    ("range-tight", "r = 0 ..< n\ns = 1 .. 10\n", "r = 0..<n\ns = 1..10\n"),
    ("compare-logic", "ok = 0<x<=10 and x in [1,2] or not done\n",
     "ok = (0 < x <= 10 and x in [1, 2]) or not done\n"),
    ("not-in", "ok = x not  in xs\n", "ok = x not in xs\n"),
    ("pipe-inline", "total = 1..10 |> filter(fn(n)=>n%2==0) |> map(fn(n) => n*n)|>sum\n",
     "total = 1..10 |> filter(fn(n) => n % 2 == 0) |> map(fn(n) => n * n) |> sum\n"),
    # Implicit multiplication
    ("implicit", "print(2x, 2x^2, 3(x+1), 2√x, 2pi, 2π, 2x(y))\n",
     "print(2x, 2x^2, 3(x + 1), 2√x, 2pi, 2π, 2x(y))\n"),
    ("implicit-wrap", "print(2(-x), 2(e5), 0xF(a), 2(3), 2(true), 1(_0))\n",
     "print(2(-x), 2(e5), 0xF(a), 2(3), 2(true), 1(_0))\n"),
    ("implicit-parens", "a = (2x)^2\nb = -2x\nc = (2x)!\nd = 2x!\n",
     "a = (2x)^2\nb = -2x\nc = (2x)!\nd = 2x!\n"),
    ("explicit-stays", "a = 2 * x\nb = -2 * x\n", "a = 2 * x\nb = -2 * x\n"),
    # Unary operators and other spellings
    ("unary", "a = - x\nb = √ x\nc = n !\nd = !done\ne = not  done\n",
     "a = -x\nb = √x\nc = n!\nd = not done\ne = not done\n"),
    ("spellings", "z = a ** b && c || d\n", "z = (a^b and c) or d\n"),
    ("unicode-ops", "w = a × b ÷ c · d ≤ e\nok = a ≥ b and c ≠ d\nv = 5 − 3\n",
     "w = a * b / c * d <= e\nok = a >= b and c != d\nv = 5 - 3\n"),
    ("unicode-kept", "c = 2π * r\nbig = ∞\nroot = √2\n", "c = 2π * r\nbig = ∞\nroot = √2\n"),
    ("bang-not-precedence", "x = !a == b\ny = !(a and b)\nz = !a and b\n",
     "x = (not a) == b\ny = not (a and b)\nz = not a and b\n"),
    # Literals
    ("numbers-verbatim", "n = 1_000 + 0xFF + 1e-3 + 0b101 + 6.02e23\n",
     "n = 1_000 + 0xFF + 1e-3 + 0b101 + 6.02e23\n"),
    ("strings-verbatim", "s = 'raw {x}' + \"t\\t{x:>4}\\n\" + \"{d.name} is {d.age:>4}\"\n",
     "s = 'raw {x}' + \"t\\t{x:>4}\\n\" + \"{d.name} is {d.age:>4}\"\n"),
    # Collections
    ("dict", "d = {name:\"Ada\",\"age\":36,(1+1):true, (k): 1, -1: 0}\n",
     "d = {name: \"Ada\", \"age\": 36, (1 + 1): true, (k): 1, -1: 0}\n"),
    ("empty", "a = [ ]\nb = { }\nf( )\n", "a = []\nb = {}\nf()\n"),
    ("call-list", "f( a ,b )\nxs = [1,2 ,3,]\n", "f(a, b)\nxs = [1, 2, 3]\n"),
    ("slices", "s = xs[ 1 : 3 ]\nt = xs[::2]\nu = xs[:]\nv = xs[1:]\nw = xs[:2:3]\n",
     "s = xs[1:3]\nt = xs[::2]\nu = xs[:]\nv = xs[1:]\nw = xs[:2:3]\n"),
    ("comprehension", "ys = [ n^2 for n in 1..<20 if is_prime( n ) ]\n",
     "ys = [n^2 for n in 1..<20 if is_prime(n)]\n"),
    ("method-and-field", "n = d . name\nm = xs . map( f ) . sum( )\n",
     "n = d.name\nm = xs.map(f).sum()\n"),
    ("field-call", "(a.b)(1)\na.b (1)\n(2)(x)\n2 (x)\n", "(a.b)(1)\n(a.b)(1)\n(2)(x)\n(2)(x)\n"),
    # Statements
    ("multi-assign", "a,b=b,a+b\nc, d = [1, 2]\ne, f = pair\n",
     "a, b = b, a + b\nc, d = [1, 2]\ne, f = pair\n"),
    ("compound", "xs[i]+=1\nx **= 2\ny //= 3\n", "xs[i] += 1\nx ^= 2\ny //= 3\n"),
    ("let-const", "let z : float=1\nlet w\nlet t: list[int]\nconst G=9.81\n",
     "let z: float = 1\nlet w\nlet t: list[int]\nconst G = 9.81\n"),
    ("import", "import  \"lib.jlang\"  as  lib\nimport 'other.jlang'\n",
     "import \"lib.jlang\" as lib\nimport 'other.jlang'\n"),
    ("semicolons", "a = 1; b = 2;\n", "a = 1\nb = 2\n"),
    ("crlf", "a = 1\r\nif a {\r\n  b()\r\n}\r\n", "a = 1\nif a {\n  b()\n}\n"),
    # Functions
    ("fn-block", "fn fib(n:int)->int{if n<2{return n}\nfib(n-1)+fib(n-2)}\n",
     "fn fib(n: int) -> int {\n  if n < 2 { return n }\n  fib(n - 1) + fib(n - 2)\n}\n"),
    ("fn-arrow", "fn square(x)=>x*x\n", "fn square(x) => x * x\n"),
    ("fn-math", "f(x)=x^2+1\ng(a, b) = a*b\nh() = 1\n", "f(x) = x^2 + 1\ng(a, b) = a * b\nh() = 1\n"),
    ("fn-params", "fn f(n: int, k: float = 1.0) -> int { n * k }\n",
     "fn f(n: int, k: float = 1.0) -> int {\n  n * k\n}\n"),
    ("fn-empty", "fn f() { }\nfn g() {\n\n}\n", "fn f() {}\nfn g() {}\n"),
    ("lambdas", "add = fn(a,b)=>a+b\ng = fn(x) { x * 2 }\nh = fn() {}\n",
     "add = fn(a, b) => a + b\ng = fn(x) { x * 2 }\nh = fn() {}\n"),
    ("lambda-block-multiline", "g = fn(x) {\n  x * 2\n}\n", "g = fn(x) {\n  x * 2\n}\n"),
    ("control-flow", "fn f(xs) {\nfor x in xs { if x { continue } }\nwhile true { break }\nthrow \"no\"\nreturn\n}\n",
     "fn f(xs) {\n  for x in xs {\n    if x { continue }\n  }\n  while true {\n    break\n  }\n"
     "  throw \"no\"\n  return\n}\n"),
    ("for-targets", "for i,v in enumerate(xs) { print(i, v) }\n",
     "for i, v in enumerate(xs) {\n  print(i, v)\n}\n"),
    ("try", "try { risky() } catch err { print(err) }\ntry { a() }\ncatch { b() }\n",
     "try {\n  risky()\n} catch err {\n  print(err)\n}\ntry {\n  a()\n} catch {\n  b()\n}\n"),
    # If
    ("if-else-if", "if x > 1 {\ny\n} else if x < 0 {\nz\n}\nelse {\nw\n}\n",
     "if x > 1 {\n  y\n} elif x < 0 {\n  z\n} else {\n  w\n}\n"),
    ("if-one-line-kept", "if done { break }\nif a { b() } elif c { d() } else { e() }\n",
     "if done { break }\nif a { b() } elif c { d() } else { e() }\n"),
    ("if-nested-not-one-line", "while true { if done { break } }\n",
     "while true {\n  if done { break }\n}\n"),
    ("if-one-line-too-long", "if a_long_condition_name > 1 { return a_long_result_name + another_long_name + yet_another_name_here }\n",
     "if a_long_condition_name > 1 {\n  return a_long_result_name + another_long_name + yet_another_name_here\n}\n"),
    ("if-expr-inline", "label = if x > 10 {\n  \"big\"\n} else {\n  \"small\"\n}\nprint(if x { 1 } elif y { 2 } else { 3 })\n",
     "label = if x > 10 { \"big\" } else { \"small\" }\nprint(if x { 1 } elif y { 2 } else { 3 })\n"),
    ("if-expr-block-form", "x = if c { a = 1; a } else { 2 }\n",
     "x = if c {\n  a = 1\n  a\n} else {\n  2\n}\n"),
    ("if-expr-arrow-body", "fn sign(x) => if x > 0 { 1 } else { -1 }\n",
     "fn sign(x) => if x > 0 { 1 } else { -1 }\n"),
    ("if-empty-branch", "if x {\n} else {\n  y\n}\n", "if x {} else {\n  y\n}\n"),
    # Keep multi-line layout
    ("list-multiline", "xs = [1,\n  2, 3]\n", "xs = [\n  1,\n  2,\n  3,\n]\n"),
    ("list-nested", "m = [\n[1, 2],\n[3,\n4]]\n", "m = [\n  [1, 2],\n  [\n    3,\n    4,\n  ],\n]\n"),
    ("dict-multiline", "d = {\nname: \"Ada\", age: 36}\n", "d = {\n  name: \"Ada\",\n  age: 36,\n}\n"),
    ("args-multiline", "print(a,\n  b)\n", "print(\n  a,\n  b,\n)\n"),
    ("args-hug", "print({\n  a: 1,\n})\nitems.map(fn(x) {\n  y = x\n  y\n})\n",
     "print({\n  a: 1,\n})\nitems.map(fn(x) {\n  y = x\n  y\n})\n"),
    ("params-multiline", "fn f(\na,\nb = 2) {\na\n}\n", "fn f(\n  a,\n  b = 2,\n) {\n  a\n}\n"),
    ("comprehension-multiline", "ys = [n^2\n  for n in xs\n  if n > 1]\n",
     "ys = [\n  n^2\n  for n in xs\n  if n > 1\n]\n"),
    ("pipe-multiline", "total = xs\n|> map(f)\n  |> sum\n", "total = xs\n  |> map(f)\n  |> sum\n"),
    ("pipe-multiline-trailing-op", "total = xs |>\n  map(f) |> sum\n", "total = xs\n  |> map(f)\n  |> sum\n"),
    ("pipe-hug", "y = xs |> map(fn(x) {\n  x\n}) |> sum\n", "y = xs |> map(fn(x) {\n  x\n}) |> sum\n"),
    ("chain-multiline", "nums\n.map(fn(x) => x^2)\n    .sum()\n", "nums\n  .map(fn(x) => x^2)\n  .sum()\n"),
    ("chain-breaks-every-method", "t = nums.map(f)\n  .sum()\n", "t = nums\n  .map(f)\n  .sum()\n"),
    ("chain-fields-stay", "t = self.items\n  .map(f).total\n", "t = self.items\n  .map(f).total\n"),
    ("chain-in-block", "fn f() {\n  return xs\n    .map(g)\n    .sum()\n}\n", "fn f() {\n  return xs\n    .map(g)\n    .sum()\n}\n"),
    ("binary-joined", "x = a +\n  b *\n  c\n", "x = a + b * c\n"),
    # Blank lines
    ("blank-collapse", "\n\n\na = 1\n\n\n\nb = 2\n\n\n", "a = 1\n\nb = 2\n"),
    ("blank-block-edges", "fn f() {\n\n\n  a = 1\n\n\n  b = 2\n\n}\n", "fn f() {\n  a = 1\n\n  b = 2\n}\n"),
    ("blank-around-comments", "a = 1\n\n\n# about b\nb = 2\n# about c\n\nc = 3\n",
     "a = 1\n\n# about b\nb = 2\n# about c\n\nc = 3\n"),
    ("empty-program", "\n\n  \n", ""),
]


@pytest.mark.parametrize("src,expected", [pytest.param(s, e, id=i) for i, s, e in EXACT])
def test_exact_output(src: str, expected: str) -> None:
    assert check(src) == expected
    assert format_code(expected) == expected  # expected text is already formatted


# Parentheses (only where needed)

PRECEDENCE = [
    ("(a + b) * c", "(a + b) * c"),
    ("a + (b * c)", "a + b * c"),
    ("(a * b) + c", "a * b + c"),
    ("a - (b - c)", "a - (b - c)"),
    ("(a - b) - c", "a - b - c"),
    ("a - (b + c)", "a - (b + c)"),
    ("a / (b * c)", "a / (b * c)"),
    ("(a / b) * c", "a / b * c"),
    ("a % (b % c)", "a % (b % c)"),
    ("a // b * c", "a // b * c"),
    ("a ^ (b ^ c)", "a^b^c"),
    ("(a ^ b) ^ c", "(a^b)^c"),
    ("2 ^ 3 ^ 2", "2^3^2"),
    ("-2 ^ 2", "-2^2"),
    ("-(2 ^ 2)", "-2^2"),
    ("(-2) ^ 2", "(-2)^2"),
    ("2 ^ (-1)", "2^-1"),
    ("2 ^ -x ^ 2", "2^-x^2"),
    ("-(a * b)", "-(a * b)"),
    ("(-a) * b", "-a * b"),
    ("-(-a)", "--a"),
    ("-x ^ y", "-x^y"),
    ("√(x + 1)", "√(x + 1)"),
    ("√(x ^ 2)", "√x^2"),
    ("(√x) ^ 2", "(√x)^2"),
    ("√(√x)", "√√x"),
    ("(n + 1)!", "(n + 1)!"),
    ("(-n)!", "(-n)!"),
    ("-(n!)", "-n!"),
    ("(n!)!", "n!!"),
    ("(x ^ 2)!", "(x^2)!"),
    ("(2x)^2", "(2x)^2"),
    ("a ^ (2x)", "a^2x"),
    ("2(x + 1)^2", "2(x + 1)^2"),
    ("(2(x + 1))^2", "(2(x + 1))^2"),
    ("(a < b) == c", "(a < b) == c"),
    ("a < (b < c)", "a < (b < c)"),
    ("a < b < c", "a < b < c"),
    ("(a < b) < c", "(a < b) < c"),
    ("a in (b in c)", "a in (b in c)"),
    ("x not in (1..5)", "x not in 1..5"),
    ("not (a == b)", "not a == b"),
    ("(not a) == b", "(not a) == b"),
    ("not (a and b)", "not (a and b)"),
    ("(not a) and b", "not a and b"),
    ("not not x", "not not x"),
    ("!!x", "not not x"),
    ("-(not x) + y", "-(not x) + y"),
    ("a + (not b)", "a + not b"),
    ("(a + not b) + c", "a + (not b) + c"),
    ("a and (b or c)", "a and (b or c)"),
    ("(a and b) or c", "(a and b) or c"),  # parens kept for clarity
    ("a or (b and c)", "a or (b and c)"),
    ("(a or b) and c", "(a or b) and c"),
    ("a or (b or c)", "a or (b or c)"),
    ("(a == b) and (c == d)", "a == b and c == d"),
    ("(1..10) |> sum", "1..10 |> sum"),
    ("1..(n + 1)", "1..n + 1"),
    ("(1..n) + 1", "(1..n) + 1"),
    ("(a..b)..c", "(a..b)..c"),
    ("a..(b..c)", "a..(b..c)"),
    ("(0..n) == r", "0..n == r"),
    ("-1..-1", "-1..-1"),
    ("x |> (y |> z)", "x |> (y |> z)"),
    ("(x |> y) |> z", "x |> y |> z"),
    ("(a or b) |> f", "a or b |> f"),
    ("a or (b |> f)", "a or (b |> f)"),
    ("(fn(x) => x + 1)(2)", "(fn(x) => x + 1)(2)"),
    ("(fn(x) => x) + 1", "(fn(x) => x) + 1"),
    ("x |> (fn(y) => y) |> g", "x |> (fn(y) => y) |> g"),
    ("x |> fn(y) => y |> g", "x |> fn(y) => y |> g"),
    ("fn(x) { x }(3)", "fn(x) { x }(3)"),
    ("(if c { a } else { b }) + 1", "if c { a } else { b } + 1"),
    ("f(x)(y)", "f(x)(y)"),
    ("xs[0][1]", "xs[0][1]"),
    ("(a + b)[0]", "(a + b)[0]"),
    ("(a + b).c", "(a + b).c"),
    ("(a + b).m(1)", "(a + b).m(1)"),
    ("-(a.b)", "-a.b"),
    ("(-a).b", "(-a).b"),
    ("(-f)(x)", "(-f)(x)"),
    ("(a.b)(c)", "(a.b)(c)"),
    ("(a.b.c)(d)", "(a.b.c)(d)"),
    ("a.b(c)", "a.b(c)"),
    ("(2)(x)", "(2)(x)"),
    ("2.5.floor()", "2.5.floor()"),
    ("(x)", "x"),
    ("((((x))))", "x"),
]


@pytest.mark.parametrize("src,expected", PRECEDENCE)
def test_precedence(src: str, expected: str) -> None:
    out = check(f"r = {src}\n")
    assert out == f"r = {expected}\n"


# Comments

def test_comment_standalone_and_trailing() -> None:
    src = "# header\nx = 1 # one\n\n  # about y\ny = 2    #two\n# end\n"
    assert fmt(src) == "# header\nx = 1  # one\n\n# about y\ny = 2  # two\n# end\n"


def test_comment_normalisation() -> None:
    src = "#!/usr/bin/env jarlang\n#plain\n##  double\n#---------\n#   indented code\n#\nx = 1 #tight\n"
    assert fmt(src) == ("#!/usr/bin/env jarlang\n# plain\n##  double\n#---------\n#   indented code\n#\n"
                        "x = 1  # tight\n")


def test_comment_in_blocks() -> None:
    src = ("fn f(x) {  # header\n\n  # first\n  a = 1   # trail a\n  # last\n\n}  # after\n"
           "fn g() {\n  # only comment\n}\n"
           "fn h() {  # todo\n}\n")
    assert fmt(src) == ("fn f(x) {  # header\n  # first\n  a = 1  # trail a\n  # last\n}  # after\n"
                        "fn g() {\n  # only comment\n}\n"
                        "fn h() {  # todo\n}\n")


def test_comment_nested_indentation() -> None:
    src = "while a {\nif b {\n# deep\nc()\n# deep end\n}\n# loop end\n}\n"
    assert fmt(src) == "while a {\n  if b {\n    # deep\n    c()\n    # deep end\n  }\n  # loop end\n}\n"


def test_comment_between_branches_moves_into_next_block() -> None:
    src = "if a {\n  b\n} # after then\n# more\nelse {\n  c\n}\ntry {\n  x()\n}\n# handler\ncatch e {\n  y()\n}\n"
    assert fmt(src) == ("if a {\n  b\n} else {\n  # after then\n  # more\n  c\n}\n"
                        "try {\n  x()\n} catch e {\n  # handler\n  y()\n}\n")


def test_comment_in_collections() -> None:
    src = ("x = [  # opener\n  1,  # one\n  # before two\n  2\n  # dangling\n]\n"
           "d = {\n  name: \"x\",   # nm\n  age: 3\n}\n"
           "y = foo(a,  # after a\n  b)\n"
           "empty = [\n  # nothing yet\n]\n")
    assert fmt(src) == ("x = [  # opener\n  1,  # one\n  # before two\n  2,\n  # dangling\n]\n"
                        "d = {\n  name: \"x\",  # nm\n  age: 3,\n}\n"
                        "y = foo(\n  a,  # after a\n  b,\n)\n"
                        "empty = [\n  # nothing yet\n]\n")


def test_comment_after_opener_of_empty_brackets() -> None:
    # Used to leave a blank line before the closing bracket
    src = "x = f( # c\n)\ny = [ # c\n]\nz = { # c\n}\n"
    assert fmt(src) == "x = f(  # c\n)\ny = [  # c\n]\nz = {  # c\n}\n"


def test_comment_in_pipeline_and_chain() -> None:
    src = ("total = xs  # base\n  # before filter\n  |> filter(f)   # keep\n  |> sum  # done\n"
           "n = nums  # start\n  .map(f)  # square\n  .sum()\n")
    assert fmt(src) == ("total = xs  # base\n  # before filter\n  |> filter(f)  # keep\n  |> sum        # done\n"
                        "n = nums  # start\n  .map(f)  # square\n  .sum()\n")


def test_comment_in_comprehension() -> None:
    src = "r = [\n  x  # value\n  for x in xs  # source\n  # filter:\n  if x > 1\n]\n"
    assert fmt(src) == "r = [\n  x            # value\n  for x in xs  # source\n  # filter:\n  if x > 1\n]\n"


def test_comment_inside_expression_moves_before_statement() -> None:
    src = "fn f() {\n  z = (a +  # inside\n    b)\n  if p and  # why\n    q {\n    r()\n  }\n}\n"
    assert fmt(src) == "fn f() {\n  # inside\n  z = a + b\n  # why\n  if p and q {\n    r()\n  }\n}\n"


def test_comment_keeps_if_expression_multiline() -> None:
    src = "v = if c {\n  a  # yes\n} else {\n  b\n}\nw = if c and  # cond\n  d { 1 } else { 2 }\n"
    assert fmt(src) == ("v = if c {\n  a  # yes\n} else {\n  b\n}\n"
                        "# cond\nw = if c and d { 1 } else { 2 }\n")


def test_comment_only_program() -> None:
    assert fmt("# just a note\n\n\n# another\n") == "# just a note\n\n# another\n"
    assert fmt("#no newline at end") == "# no newline at end\n"


def test_comment_order_preserved() -> None:
    src = "# 1\na = [  # 2\n  1,  # 3\n  # 4\n  2,\n]  # 5\n# 6\nif a {  # 7\n  b  # 8\n} else {  # 9\n  c\n}  # 10\n"
    out = fmt(src)
    lexer = Lexer(Source(out))
    lexer.tokenize()
    assert [c.text.strip() for c in lexer.comments] == [str(i) for i in range(1, 11)]


# Other behavior

def test_multiline_string_untouched() -> None:
    src = 'fn f() {\n  s = "line one\n   line two  \n{x}"\n  print(s)\n}\n'
    out = check(src, whitespace=False)
    assert '"line one\n   line two  \n{x}"' in out


def test_one_line_if_respects_indentation_width() -> None:
    body = "return " + "x" * 80
    flat = f"if a {{ {body} }}\n"
    assert fmt(flat) == flat  # 96 columns at the top level
    nested = "fn f() {\n  while t {\n    while u {\n      " + flat + "    }\n  }\n}\n"
    out = fmt(nested)
    assert f"      if a {{\n        {body}\n      }}\n" in out  # would be 102 columns on one line


def test_format_source_and_format_code_agree() -> None:
    text = "x=1\nfn f(a){a}\n"
    assert format_source(Source(text, "demo.jlang")) == format_code(text) == "x = 1\nfn f(a) {\n  a\n}\n"


@pytest.mark.parametrize("bad", ["x = (", "fn f( {", "x = 1 +", "print(\"unterminated)", "x = @"])
def test_syntax_errors_propagate(bad: str) -> None:
    with pytest.raises(JarLangError):
        format_code(bad)


def test_deep_left_chains_do_not_recurse() -> None:
    for op in ("+", "and", "|>", "<"):
        text = "x = " + f" {op} ".join(f"a{i}" for i in range(3000)) + "\n"
        assert format_code(text) == text
    text = "x = a" + ".m()" * 3000 + "\n"
    assert format_code(text) == text


def test_cli_fmt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from jarlang import cli

    path = tmp_path / "demo.jlang"
    path.write_text("x=1\n", encoding="utf-8")
    assert cli.main(["fmt", "--check", str(path)]) == 1
    assert cli.main(["fmt", "-w", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == "x = 1\n"
    assert cli.main(["fmt", "--check", str(path)]) == 0
    capsys.readouterr()


# Random expressions and programs

_ATOMS = ["x", "y", "2", "3.5", "f(x)", "xs[0]", "a.b", "a.m(1)", "n!", "true", "nil", '"s"',
          "[1, 2]", "{k: 1}", "π", "0xFF", "1_000", "∞"]
_BINARY = ["+", "-", "*", "/", "//", "%", "^", "**", "×", "÷", "==", "!=", "<", "<=", ">", ">=",
           "in", "not in", "and", "or", "&&", "||", "|>", "..", "..<"]
_PREFIX = ["-", "+", "√", "not ", "!"]


# Random fully parenthesized expression, so any tree shape can occur
def _random_expr(r: random.Random, depth: int) -> str:
    x = r.random()
    if depth <= 0 or x < 0.15:
        return r.choice(_ATOMS)
    sub = lambda: _random_expr(r, depth - 1)  # noqa: E731
    if x < 0.55:
        return f"({sub()} {r.choice(_BINARY)} {sub()})"
    if x < 0.7:
        return f"({r.choice(_PREFIX)}{sub()})"
    if x < 0.75:
        return f"({sub()})!"
    if x < 0.8:
        return f"(2{r.choice(['x', '(' + sub() + ')', '√y', 'y^2', 'f(1)'])})"
    if x < 0.85:
        return f"(fn(q) => {sub()})"
    if x < 0.88:
        return f"(if {sub()} {{ {sub()} }} else {{ {sub()} }})"
    if x < 0.92:
        return f"({sub()})({sub()})"
    if x < 0.95:
        return f"({sub()})[{sub()}]"
    return f"({sub()}).m({sub()})"


@pytest.mark.parametrize("seed", range(8))
def test_random_expressions_round_trip(seed: int) -> None:
    r = random.Random(seed)
    for _ in range(80):
        check(f"r = {_random_expr(r, r.randint(1, 5))}\n")


# Random programs with line breaks, comments and blank lines wherever allowed
class _ProgramGen:
    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self.n = 0

    def comment(self) -> str:
        self.n += 1
        return f"#c{self.n}" if self.r.random() < 0.2 else f"# c{self.n}"

    def brk(self) -> str:
        x = self.r.random()
        if x < 0.6:
            return " "
        if x < 0.8:
            return "\n"
        if x < 0.9:
            return "  " + self.comment() + "\n"
        return "\n" + self.comment() + "\n\n"

    def expr(self, d: int) -> str:
        r, B = self.r, self.brk
        x = r.random()
        sub = lambda: self.expr(d - 1)  # noqa: E731
        if d <= 0 or x < 0.2:
            return r.choice(["x", "2", "1_000", "3.5", '"s"', "'r'", '"v{x}"', "true", "nil", "π",
                             "n!", "2x", "3(x + 1)", "√x", "2x^2", "-x", "a.b", "(a.b)(1)"])
        if x < 0.4:
            op = r.choice(["+", "-", "*", "/", "^", "**", "==", "<", "and", "or", "..", "in", "|>", "&&"])
            return f"({sub()} {op}{B()}{sub()})"
        if x < 0.5:
            return "f(" + ",".join(B() + sub() for _ in range(r.randint(0, 3))) + B() + ")"
        if x < 0.58:
            items = [B() + sub() for _ in range(r.randint(0, 3))]
            return "[" + ",".join(items) + ("," if items and r.random() < 0.3 else "") + B() + "]"
        if x < 0.64:
            keys = ["k", '"k"', "(x)", "1", "(1 + 1)"]
            items = [B() + r.choice(keys) + ":" + B() + sub() for _ in range(r.randint(0, 3))]
            return "{" + ",".join(items) + B() + "}"
        if x < 0.7:
            return f"{sub()}{B()}|> {r.choice(['g', 'h(1)', 'fn(z) => z'])}"
        if x < 0.76:
            return f"xs{B()}.m({sub()}){B()}.k(){r.choice(['', '.fld'])}"
        if x < 0.8:
            return f"fn(q) =>{B()}{sub()}"
        if x < 0.84:
            return f"fn(q) {{{B()}{sub()}{B()}}}"
        if x < 0.88:
            return f"(if {sub()} {{{B()}{sub()}{B()}}} else {{{B()}{sub()}{B()}}})"
        if x < 0.92:
            return f"[{B()}{sub()}{B()}for q in {sub()}{B()}if {sub()}{B()}]"
        if x < 0.96:
            return f"xs[{sub()}:{sub()}]"
        return f"(not {sub()})"

    def block(self, d: int) -> str:
        return "{" + self.brk() + self.stmts(d - 1, self.r.randint(0, 3)) + self.brk() + "}"

    def stmt(self, d: int) -> str:
        r, B = self.r, self.brk
        E = lambda: self.expr(r.randint(0, 3))  # noqa: E731
        x = r.random()
        if d <= 0 or x < 0.35:
            return r.choice([f"x = {E()}", f"x +={B()}{E()}", f"a, b = {E()},{B()}{E()}", f"print({E()})",
                             f"return {E()}", "break", "return", f"let v: int = {E()}", f"const K = {E()}",
                             f"xs[i] = {E()}", f"throw {E()}", 'import "m.jlang" as m', E(), "let w"])
        if x < 0.5:
            s = f"if {E()} {self.block(d)}"
            for _ in range(r.randint(0, 2)):
                s += r.choice([" ", "\n", "  " + self.comment() + "\n"]) + f"elif {E()} {self.block(d)}"
            if r.random() < 0.5:
                s += r.choice([" ", "\n"]) + f"else {self.block(d)}"
            return s
        if x < 0.55:
            return f"if {E()} {{ {self.stmt(0)} }}" + r.choice(["", f" else {{ {self.stmt(0)} }}"])
        if x < 0.62:
            return f"while {E()} {self.block(d)}"
        if x < 0.7:
            return f"for i, v in {E()} {self.block(d)}"
        if x < 0.8:
            return f"fn g(a, b = {E()}) -> int {self.block(d)}"
        if x < 0.85:
            return f"fn h(a) =>{B()}{E()}"
        if x < 0.9:
            return f"k(a, b) = {E()}"
        return f"try {self.block(d)}{r.choice([' ', chr(10)])}catch e {self.block(d)}"

    def stmts(self, d: int, n: int) -> str:
        out = []
        for _ in range(n):
            x = self.r.random()
            pre = self.comment() + "\n" if x < 0.15 else "\n\n" if x < 0.25 else ""
            s = self.stmt(d)
            if self.r.random() < 0.1:
                s += ";"
            if self.r.random() < 0.2:
                s += "  " + self.comment() + "\n"
            out.append(pre + s)
        return "\n".join(out)

    def program(self) -> str:
        return self.stmts(3, self.r.randint(1, 6)) + "\n"


@pytest.mark.parametrize("seed", range(10))
def test_random_programs_round_trip(seed: int) -> None:
    for i in range(20):
        src = _ProgramGen(seed * 1000 + i).program()
        parse(src)  # the generator only produces valid programs
        check(src)


# Examples

def _example_files() -> list[Path]:
    return sorted(EXAMPLES.rglob("*.jlang")) if EXAMPLES.is_dir() else []


@pytest.mark.parametrize("path", _example_files(), ids=lambda p: p.name)
def test_examples_format_cleanly(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        parse(text, str(path))
    except JarLangError:
        pytest.skip(f"{path.name} does not parse")
    check(text, whitespace=False)


def test_trailing_comments_on_consecutive_lines_are_aligned() -> None:
    src = "x = 16 # a\ny = 9_000      # b\nprint(y)   # c\n\nz = 1  # alone\n"
    assert fmt(src) == "x = 16     # a\ny = 9_000  # b\nprint(y)   # c\n\nz = 1  # alone\n"


def test_alignment_ignores_hashes_in_strings_and_respects_indentation() -> None:
    src = 'a = "#x"  # one\nbb = 2 # two\nfn f() {\n  c = 1 # three\n}\n'
    assert fmt(src) == 'a = "#x"  # one\nbb = 2    # two\nfn f() {\n  c = 1  # three\n}\n'
