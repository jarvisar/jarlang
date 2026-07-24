"""Tests for error messages, locations and hints."""

import pytest

from jarlang.analysis import check_source
from jarlang.errors import JarLangError
from jarlang.source import Source
from support import run_error


def helps(err: JarLangError) -> str:
    return " | ".join(err.diagnostic.helps)


@pytest.mark.parametrize("code, message, underline, hint", [
    ("total = 3\nprint(totl)", "undefined variable `totl`", "totl", "did you mean `total`?"),
    ("print(sqrtt(4))", "undefined variable `sqrtt`", "sqrtt", "did you mean `sqrt`?"),
    ("fn area(w, h) => w * h\nprint(area(3))", "`area` takes 2 arguments but 1 was given", "area(3)", "area(w, h)"),
    ("print(sqrt(1, 2))", "`sqrt` takes 1 argument but 2 were given", "sqrt(1, 2)", "sqrt(x)"),
    ("xs = [1, 2, 3]\nprint(xs[5])", "index 5 is out of range for list of length 3", "xs[5]", ""),
    ('print("n = " + 3)', "cannot apply `+` to str and int", '"n = " + 3', "interpolation"),
    ("print(1 / 0)", "division by zero", "1 / 0", ""),
    ("print(5 % 0)", "modulo by zero", "5 % 0", ""),
    ("const G = 9.8\nG = 10", "cannot assign to constant `G`", "G", "let G"),
    ("pi = 3", "cannot assign to built-in constant `pi`", "pi", "let pi"),
    ("for i in 10 { }", "cannot iterate over int", "10", "1..10"),
    ("x = 5\nprint(x(2))", "int value is not callable", "x(2)", "explicit `*`"),
    ("d = {name: 1}\nprint(d.nme)", "dict has no key `nme`", "nme", "did you mean `name`?"),
    ("xs = [1]\nxs.pussh(2)", "no function or field named `pussh` for list", "pussh", "did you mean `push`?"),
    ("break", "`break` outside of a loop", "break", ""),
    ("return 1", "`return` outside of a function", "return 1", ""),
    ("print(sqrt(-4))", "square root of a negative number (-4)", "sqrt(-4)", ""),
    ("print(ln(0))", "ln(0) is undefined", "ln(0)", "positive"),
    ("print(asin(2))", "asin(2) is undefined", "asin(2)", "between -1 and 1"),
    ("print((-3)!)", "factorial of a negative number (-3)", "(-3)!", ""),
    ("print(10^10^10)", "is too large", "10^10^10", "float base"),
    ("print(2 < \"a\")", "cannot compare int and str with `<`", '2 < "a"', ""),
    ("fn f(x: int) => x\nf(1.5)", "`f` expects `x` to be int, but got float", "f(1.5)", ""),
    ("fn f() -> str => 1\nf()", "`f` should return str, but returned int", "f()", ""),
    ("let t: integer = 1", "unknown type `integer`", "integer", "did you mean `int`?"),
    ("x = [1, 2]\nx.y = 3", "cannot set field `y` on list", "x.y", ""),
    ('"abc"[0] = "z"', "strings are immutable", "", ""),
    ('d = {}\nprint(d["missing"])', 'key "missing" not found in dict', 'd["missing"]', "get(d, key, default)"),
    ("throw \"custom\"", "uncaught error: custom", 'throw "custom"', ""),
    ("assert(1 > 2, \"one is not greater\")", "one is not greater", 'assert(1 > 2, "one is not greater")', ""),
    ("a, b = [1, 2, 3]", "cannot unpack 3 values into 2 variables", "", ""),
    ("x = nil + 1", "cannot apply `+` to nil and int", "nil + 1", "forget to return"),
])
def test_error_messages(code, message, underline, hint):
    err = run_error(code)
    assert message in err.message
    if underline:
        assert err.span is not None and err.span.text == underline
    if hint:
        assert hint in helps(err)


def test_runtime_error_has_call_trace():
    err = run_error("fn inner(x) => 1 / x\nfn outer() => inner(0)\nouter()")
    trace = err.diagnostic.trace
    assert len(trace) == 2
    assert "inner" in trace[0] and "outer" in trace[1]


def test_recursive_trace_is_collapsed():
    err = run_error("fn f(n) { if n == 0 { return 1 / 0 }; f(n - 1) }\nf(50)")
    assert len(err.diagnostic.trace) <= 10
    assert any("×" in t for t in err.diagnostic.trace)


def test_rendering_looks_like_rust():
    err = run_error("total = 3\nprint(totl)")
    text = err.render(color=False)
    assert text.splitlines() == [
        "error: undefined variable `totl`",
        " --> <test>:2:7",
        "  |",
        "2 | print(totl)",
        "  |       ^^^^ not found in this scope",
        "  |",
        "  = help: did you mean `total`?",
    ]


def test_rendering_with_color_has_ansi_codes():
    err = run_error("print(x)")
    assert "\x1b[" in err.render(color=True)


def test_multiple_resolve_errors_are_all_reported():
    diags = check_source(Source("print(a)\nprint(b)\nbreak"))
    errors = [d for d in diags if d.severity == "error"]
    assert len(errors) == 3


def test_warnings():
    code = "fn f(x) {\n  unused = 1\n  return x\n  print(\"never\")\n}\n_ok = 2\nf(1)"
    diags = check_source(Source(code))
    messages = [d.message for d in diags if d.severity == "warning"]
    assert "unused variable `unused`" in messages
    assert "unreachable code" in messages
    assert not any("_ok" in m for m in messages)


@pytest.mark.parametrize("expr", ["y + 1", "y * 2", "y < 3", "y >= 0", "y + y", "-y"])
def test_unassigned_local_on_fast_paths(expr):
    err = run_error(f"fn f() {{\n  if false {{ y = 1 }}\n  {expr}\n}}\nf()")
    assert "variable `y` is used before it is assigned" in err.message


def test_type_errors_on_fast_paths():
    for code, msg in [("fn f(s) => s + 1\nf(\"a\")", "cannot apply `+` to str and int"),
                      ("fn f(s) => s < 3\nf(\"a\")", "cannot compare str and int"),
                      ("fn f(a, b) => a * b\nf(true, 2)", "cannot apply `*` to bool and int")]:
        assert msg in run_error(code).message


def test_use_before_definition_is_runtime_error():
    err = run_error("print(later)\nlater = 1")
    assert "used before it is defined" in err.message


def test_names_defined_later_in_functions_are_fine():
    from support import run_program
    assert run_program("fn show() => print(value)\nvalue = 42\nshow()") == "42\n"


def test_marker_lines_up_after_tabs():
    err = run_error("\tprint(nope)")
    lines = err.render().splitlines()
    assert lines[3].index("nope") == lines[4].index("^")


def test_method_arity_is_only_a_warning():
    diags = check_source(Source("xs = [1]\nxs.len(2)"))
    assert [d.severity for d in diags if "takes" in d.message] == ["warning"]
