import pytest

from jarlang import ast
from jarlang.ast import dump
from jarlang.errors import JarLangError, MultipleErrors, ParseError
from jarlang.parser import parse, parse_expression


def e(text: str) -> str:
    return dump(parse_expression(text))


def s(text: str) -> list[str]:
    return [dump(stmt) for stmt in parse(text).stmts]


@pytest.mark.parametrize("text, expected", [
    ("1 + 2 * 3", "(+ 1 (* 2 3))"),
    ("(1 + 2) * 3", "(* (+ 1 2) 3)"),
    ("2 ^ 3 ^ 2", "(^ 2 (^ 3 2))"),
    ("2 ** 3", "(^ 2 3)"),
    ("-2^2", "(- (^ 2 2))"),
    ("2^-1", "(^ 2 (- 1))"),
    ("2^3!", "(^ 2 (! 3))"),
    ("-3!", "(- (! 3))"),
    ("a - b - c", "(- (- a b) c)"),
    ("a / b // c % d", "(% (// (/ a b) c) d)"),
    ("not a == b", "(not (cmp a == b))"),
    ("!a and b", "(and (not a) b)"),
    ("a or b and c", "(or a (and b c))"),
    ("a || b && c", "(or a (and b c))"),
    ("x |> f |> g(1)", "(pipe (pipe x f) (call g [1]))"),
    ("1..n + 1", "(rangeexpr 1 (+ n 1) True)"),
    ("0..<10", "(rangeexpr 0 10)"),
    ("√16 + 1", "(+ (√ 16) 1)"),
])
def test_precedence(text, expected):
    assert e(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("2x", "(* 2 x)"),
    ("2x^2", "(* 2 (^ x 2))"),
    ("3(x + 1)", "(* 3 (+ x 1))"),
    ("2√x", "(* 2 (√ x))"),
    ("1/2x", "(/ 1 (* 2 x))"),
    ("2pi r", None),  # not valid: `r` is a separate token
])
def test_implicit_multiplication(text, expected):
    if expected is None:
        with pytest.raises(JarLangError):
            parse_expression(text)
    else:
        assert e(text) == expected
        node = parse_expression(text)
        assert any(isinstance(n, ast.Binary) and n.implicit for n in ast.walk(node))


def test_spaced_number_and_name_is_not_multiplication():
    with pytest.raises(JarLangError):
        parse_expression("2 x")


def test_chained_comparison():
    node = parse_expression("0 < x <= 10 != y")
    assert isinstance(node, ast.Compare)
    assert node.ops == ["<", "<=", "!="]
    assert len(node.operands) == 4


def test_in_and_not_in():
    assert e("x in xs") == "(cmp x in xs)"
    assert e("x not in xs") == "(cmp x not in xs)"


def test_calls_indexing_fields_methods():
    assert e("f(1, 2)(3)") == "(call (call f [1 2]) [3])"
    assert e("xs[0][1]") == "(index (index xs 0) 1)"
    assert e("d.name") == "(field d 'name')"
    assert e("xs.map(f).sum()") == "(methodcall (methodcall xs map [f]) sum)"
    assert e("xs[1:3]") == "(slice xs 1 3)"
    assert e("xs[::2]") == "(slice xs 2)"
    assert e("n!") == "(! n)"


def test_literals():
    assert e("[1, 2, 3,]") == "(listlit [1 2 3])"
    assert e("[]") == "(listlit)"
    assert e("{}") == "(dictlit)"
    node = parse_expression('{name: "Ada", "age": 36, (1 + 1): true}')
    assert isinstance(node, ast.DictLit)
    assert node.bare_keys == [True, False, False]
    assert e("true") == "true" and e("nil") == "nil"


def test_comprehension():
    assert e("[x^2 for x in xs if x > 1]") == "(comprehension (^ x 2) [x] xs (cmp x > 1))"


def test_lambda_forms():
    assert e("fn(x) => x * 2") == "(lambda (function [(param 'x')] {(* x 2)}))"
    node = parse_expression("fn(a, b = 2) { a + b }")
    assert isinstance(node, ast.Lambda)
    assert node.func.params[1].default is not None


def test_if_expression():
    node = parse_expression("if a { 1 } elif b { 2 } else { 3 }")
    assert isinstance(node, ast.IfExpr)
    assert len(node.branches) == 2 and node.else_ is not None


def test_else_on_next_line_and_else_if():
    (stmt,) = parse("if a {\n  1\n}\nelse if b {\n  2\n}\nelse {\n  3\n}").stmts
    assert isinstance(stmt.expr, ast.IfExpr)
    assert len(stmt.expr.branches) == 2


def test_function_declaration_styles():
    prog = parse("fn f(x) { x }\nfn g(x) => x\nh(x, y) = x + y\nfn k(n: int, m: float = 1.0) -> float { n * m }")
    styles = [stmt.func.style for stmt in prog.stmts]
    assert styles == ["block", "arrow", "math", "block"]
    k = prog.stmts[3].func
    assert str(k.params[0].type) == "int" and str(k.ret_type) == "float"


def test_assignment_forms():
    assert s("x = 1") == ["(assign x 1)"]
    assert s("x += 1") == ["(assign x 1 '+')"]
    assert s("a, b = b, a") == ["(assign (listlit [a b]) (listlit [b a]))"]
    assert s("xs[0] = 1") == ["(assign (index xs 0) 1)"]
    assert s("d.k = 1") == ["(assign (field d 'k') 1)"]
    assert s("let y: float = 2") == ["(let y 2 (typeref 'float'))"]
    assert s("const G = 9.81") == ["(let G 9.81 True)"]


def test_statements():
    prog = parse("for i, x in enumerate(xs) { print(i) }\nwhile x < 3 { x += 1 }\n"
                 "try { f() } catch err { g(err) }\nthrow \"bad\"\nimport \"lib\" as lib")
    kinds = [type(stmt).__name__ for stmt in prog.stmts]
    assert kinds == ["For", "While", "Try", "Throw", "Import"]
    assert [t.name for t in prog.stmts[0].targets] == ["i", "x"]


def test_semicolons_separate_statements():
    assert len(parse("a = 1; b = 2; c = 3").stmts) == 3


def test_pipeline_over_multiple_lines():
    prog = parse("total = xs\n  |> filter(f)\n  |> sum\nprint(total)")
    assert len(prog.stmts) == 2


def test_string_interpolation_is_parsed():
    node = parse_expression('"a {x + 1:>4} b"')
    assert isinstance(node, ast.StringLit)
    part = node.parts[1]
    assert isinstance(part, ast.InterpPart) and part.spec == ">4"
    assert dump(part.expr) == "(+ x 1)"


def test_spans_point_at_source():
    source = "x = foo + bar"
    node = parse(source).stmts[0].value
    assert node.span.text == "foo + bar"
    assert node.right.span.text == "bar"


@pytest.mark.parametrize("text, message", [
    ("x = (1 + 2", "expected `)`"),
    ("print \"hi\"", "expected end of statement"),
    ("1 + ", "expected an expression"),
    ("f(x = 2)", "expected `)`"),
    ("fn (x) {}", None),
    ("fn f(a, a) { }", "duplicate parameter"),
    ("1 = x", "cannot assign"),
    ("if x { ", "expected `}`"),
])
def test_syntax_errors(text, message):
    if message is None:
        parse(text)  # a lambda expression statement is fine
        return
    with pytest.raises(JarLangError) as info:
        parse(text)
    assert message in info.value.message


def test_incomplete_input_is_flagged_for_the_repl():
    for text in ("fn f(x) {", "x = [1, 2,", "y = (3 +", "z = 1 +"):
        with pytest.raises((ParseError, MultipleErrors)) as info:
            parse(text)
        assert getattr(info.value, "incomplete", False), text


@pytest.mark.parametrize("text", ["x = 1\na, (b", "a, [", "a, {b: ", "f(a, b", "a, b = (1,", "x = [1, 2", '"{', "fn f(", "if x {"])
def test_truncated_input_never_crashes(text):
    with pytest.raises(JarLangError):
        parse(text)


def test_error_recovery_reports_several_errors():
    with pytest.raises(MultipleErrors) as info:
        parse("a = 2 2\nb = )\nc = 3\nd = [1,, 2]")
    assert len(info.value.diagnostics) >= 2


def test_unclosed_bracket_points_at_opener():
    with pytest.raises(JarLangError) as info:
        parse("x = (1 + 2\ny = 3")
    labels = info.value.diagnostic.secondary
    assert labels and "unclosed" in labels[0].message


def test_interpolation_errors_are_not_incomplete():
    with pytest.raises(ParseError) as info:
        parse('print("{1 +}")')
    assert not info.value.incomplete


def test_recovery_skips_the_rest_of_a_broken_block():
    with pytest.raises(JarLangError) as info:
        parse("if x {\n  print(1 2)\n} else {\n  print(3)\n}\ny = = 1")
    diags = getattr(info.value, "diagnostics", [info.value.diagnostic])
    assert [d.span.line for d in diags] == [2, 6]


def test_math_style_duplicate_parameter_is_named():
    with pytest.raises(ParseError) as info:
        parse("f(x, x) = x")
    assert info.value.message == "duplicate parameter `x`"
