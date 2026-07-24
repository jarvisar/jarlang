"""Tests for the native x86-64 compiler."""

from __future__ import annotations

import pytest

from jarlang.errors import JarLangError
from jarlang.native.driver import compile_to_asm, explain, run_native
from jarlang.native.peephole import optimize
from jarlang.native.typecheck import typecheck
from jarlang.optimizer import fold_constants
from jarlang.parser import parse
from jarlang.resolver import Resolver
from jarlang.source import Source
from support import EXAMPLES, native_toolchain, requires_native, run_program

NATIVE_EXAMPLES = ["hello", "primes", "mandelbrot", "sorting", "life", "numerics", "benchmark"]


def asm(code: str, target: str = "linux") -> str:
    return compile_to_asm(Source(code, "test.jlang"), target=target)


def checker_for(code: str):
    program = parse(code)
    r = Resolver()
    r.resolve_program(program)
    assert not r.errors
    return typecheck(program)


def native_error(code: str) -> JarLangError:
    with pytest.raises(JarLangError) as info:
        asm(code)
    return info.value


# Code generation (no C toolchain needed)

@pytest.mark.parametrize("target", ["linux", "windows", "macos"])
@pytest.mark.parametrize("name", NATIVE_EXAMPLES)
def test_examples_compile_to_assembly_for_every_target(name, target):
    code = (EXAMPLES / f"{name}.jlang").read_text(encoding="utf-8")
    text = asm(code, target)
    main = "_jl_main" if target == "macos" else "jl_main"
    assert f"{main}:" in text


def test_target_specific_details():
    code = "print(sqrt(2.0))"
    linux, windows, macos = asm(code, "linux"), asm(code, "windows"), asm(code, "macos")
    assert "call jl_print_float@PLT" in linux and ".note.GNU-stack" in linux
    assert "subq $32, %rsp" in windows and ".rdata" in windows  # shadow space, COFF read-only data
    assert "call _jl_print_float" in macos


def test_assembly_is_annotated_with_source_lines():
    text = asm("x = 40\nprint(x + 2)")
    assert "# 1: x = 40" in text and "# 2: print(x + 2)" in text


def test_integer_arithmetic_is_overflow_checked():
    text = asm("fn f(a, b) => a * b + 1\nprint(f(2, 3))")
    assert "jo .Lpanic" in text and "jl_panic_overflow" in text


def test_monomorphization():
    c = checker_for("fn sq(x) => x * x\nprint(sq(3), sq(1.5))")
    sigs = sorted(inst.signature for inst in c.live_instances)
    assert sigs == ["sq(x: float) -> float", "sq(x: int) -> int"]


def test_recursive_return_type_inferred():
    c = checker_for("fn fib(n) { if n < 2 { return n }; fib(n - 1) + fib(n - 2) }\nprint(fib(10))")
    (inst,) = c.live_instances
    assert str(inst.ret) == "int"


def test_list_element_type_inferred_from_push():
    c = checker_for("xs = []\nfor i in 1..3 { push(xs, i * 0.5) }\nprint(xs)")
    assert str(c.globals["xs"]) == "list[float]"


def test_explain_lists_instances():
    text = explain(Source("fn sq(x) => x * x\nprint(sq(2))", "t.jlang"))
    assert "fn sq(x: int) -> int" in text


@pytest.mark.parametrize("code, message", [
    ("f = fn(x) => x\nprint(f(1))", "anonymous functions are not supported"),
    ("d = {a: 1}\nprint(d)", "dicts are not supported"),
    ("try { print(1) } catch e { print(e) }", "`try`/`catch` blocks are not supported"),
    ("fn outer() { n = 1; fn inner() => n; inner() }\nprint(outer())", "nested functions are not supported"),
    ("x = 1\nx = 2.5", "`x` holds int but is assigned float"),
    ("x = 1\nx = \"s\"", "`x` holds int but is assigned str"),
    ("print([1, 2.5])", "list mixes int and float"),
    ("print(1 and 2)", "`and` needs bool operands"),
    ("fn f(n) { if n > 0 { return 1 }; 2.5 }\nprint(f(1))", "returns"),
    ("print(max(1, 2.5))", "mixes int and float"),
    ("print(99999999999999999999)", "too large for native code"),
    ("r = 1..10\nprint(r)", "ranges can only be used in `for` loops"),
    ("print([1] in [[1]], contains([[1]], [1]))", "comparing lists is not supported"),
    ("print(argv)", "`argv` is not supported"),
    ('print("{1.5:x}")', "invalid format spec 'x' for float"),
    ('print("{"a":,}")', "invalid format spec ',' for str"),
    ('x = 5\nprint("{x:.2}")', "invalid format spec '.2' for int"),
    ('print(len("a\\0b"))', "a zero character in a string is not supported"),
])
def test_unsupported_programs_get_clear_errors(code, message):
    err = native_error(code)
    text = err.render() if hasattr(err, "render") else str(err)
    assert message in text


def test_negative_default_without_constant_folding():
    text = compile_to_asm(Source("fn f(x = -1) => x\nprint(f())", "t.jlang"), target="linux", opt=False)
    assert "jlf_f__i:" in text


def test_peephole_optimizer():
    before = "\n".join([
        "f:", "    pushq %rax", "    popq %rcx", "    movq %rax, -8(%rbp)", "    movq -8(%rbp), %rax",
        "    jmp .L1", "    movq $1, %rax", ".L1:", "    ret"])
    after = optimize(before).split("\n")
    assert after == ["f:", "    movq %rax, %rcx", "    movq %rax, -8(%rbp)", ".L1:", "    ret"]


def test_constant_folding():
    program = fold_constants(parse("x = 2^10 + 3 * 4 - -1\ny = 1 / 0\nz = 20!"))
    from jarlang.ast import dump
    assert [dump(s) for s in program.stmts] == ["(assign x 1037)", "(assign y (/ 1 0))", "(assign z 2432902008176640000)"]


# Native output must match the interpreter
# These need a C toolchain and are skipped without one (pip install ziglang)

PROGRAMS = {
    "arith": "print(1 + 2 * 3, 7 / 2, 7 // 2, -7 // 2, 7 % 3, -7 % 3, 7 % -3, -7 // -2, 2^10, 2^0, (-3)^3)",
    "floats": "print(7.5 // 2, -7.5 // 2, 7.5 % 2, -7.5 % 2, 2.5^2, 2.0^-1, 1e300 * 10.0 > 1e300)",
    "compare": "a = 3\nb = 3.0\nprint(a == b, a < 4, 2.5 >= a, a != 3, 1 < a <= 3 < 4, \"a\" < \"b\", \"x\" == \"x\", true == true)",
    "logic": "t = true\nf = false\nprint(t and f, t or f, not t, not (1 > 2), 3 > 2 and 2 > 1 or f)",
    "if_expr": "fn sign(n) { if n > 0 { 1 } elif n < 0 { -1 } else { 0 } }\nprint(sign(9), sign(-9), sign(0))",
    "while": "i = 0\ns = 0\nwhile true { i += 1; if i % 3 == 0 { continue }; if i > 20 { break }; s += i }\nprint(i, s)",
    "for_ranges": "t = 0\nfor i in 1..10 { t += i }\nfor i in 0..<5 { t += i }\nfor i in range(10, 0, -2) { t += i }\nfor i in range(3) { t += i }\nprint(t)",
    "recursion": "fn ack(m, n) { if m == 0 { return n + 1 }; if n == 0 { return ack(m - 1, 1) }; ack(m - 1, ack(m, n - 1)) }\nprint(ack(2, 3))",
    "mutual": "fn is_even(n) { if n == 0 { return true }; is_odd(n - 1) }\nfn is_odd(n) { if n == 0 { return false }; is_even(n - 1) }\nprint(is_even(10), is_odd(7))",
    "globals": "count = 0\nfn bump(k) { count += k }\nbump(2)\nbump(3)\nprint(count)",
    "strings": 'name = "JarLang"\nprint(name + "!", len(name), upper(name), lower("ABC"), name[0], name[-1], "ab" * 3, trim("  x  "))',
    "interp": 'x = 3\ny = 2.5\nprint("x={x} y={y} sum={x + y} ok={x > 2} list={[x, x]} s={"q"}")',
    "format_specs": 'print("{3.14159:.2f}|{42:>6}|{42:<6}|{7:03}|{255:x}|{255:08b}|{1234567:,}|{0.25:.1%}|{"ab":^6}|{2.5:e}|{12345.678:,.1f}|{-5:+}|{5:+}")',
    "lists": "xs = [5, 3, 8]\npush(xs, 1)\nxs[0] = 9\nprint(xs, len(xs), xs[-1], sum(xs), max(xs), min(xs), pop(xs), xs)",
    "list_ops": "print([1, 2] + [3], [0] * 3, [true] * 2, 3 in [1, 2, 3], 5 not in [1, 2], [[1, 2], [3]], [\"a\", \"b\"], [1.5, 2.0])",
    "comprehension": "print([x^2 for x in 1..8 if x % 2 == 0], [c for c in [3, 1, 2]], [s + \"!\" for s in [\"a\", \"b\"]])",
    "nested_lists": "grid = [[0] * 3 for _ in 1..3]\ngrid[1][2] = 5\ngrid[0][0] += 1\nprint(grid, grid[1][2])",
    "parallel_assign": "a, b = 0, 1\nfor _ in 1..30 { a, b = b, a + b }\nprint(a, b)",
    "math": "print(sqrt(2), √9, sin(1), cos(1), tan(1), atan2(1.0, 2.0), hypot(3, 4), exp(2), ln(10), log2(8), log10(0.001), log(9, 3))",
    "math2": "print(abs(-3), abs(-2.5), floor(2.7), ceil(-2.7), trunc(-2.7), round(2.5), round(-3.5), round(2.675, 2), round(1250, -2), sign(-4), sign(0.5))",
    "intfuncs": "print(gcd(84, 36), lcm(4, 6), isqrt(99), is_prime(1000003), is_prime(1000001), 12!, factorial(10), clamp(12, 0, 10), min(3, 1, 2), max(2.5, 3.5))",
    "conversions": 'print(int(3.99), int(-3.99), int("42") + 1, float(3), float("2.5"), str(12) + str(1.5) + str(true), int(true))',
    "float_repr": "x = 1.0\nfor i in 1..60 { x = x * 1.7 + 0.13; print(x, 1 / x, x * 1e-9, x * 1e11) }",
    "float_edges": "print(0.1 + 0.2, 1 / 3, 2 / 3, 1e16, 1e15, 1e-4, 1e-5, 123456789.123, -0.0, 5e-324, 1.7976931348623157e308)",
    "swap_elements": "xs = [1, 2, 3]\nxs[0], xs[2] = xs[2], xs[0]\nfs = [0.5, 1.5]\nfs[0], fs[1] = 2.0, fs[0]\nprint(xs, fs)",
    "implicit_mult":"x = 4\nprint(2x, 3x^2, 2(x + 1), 2√x, 0.5x)",
    "annotations": "fn half(x: float) -> float => x / 2\nlet r: float = 3\nprint(half(3), r)",
    "defaults": "fn greet(n, suffix = \"!\") => \"hi \" + n + suffix\nprint(greet(\"a\"), greet(\"b\", \"?\"))",
    "methods_and_pipes": "fn double(x) => 2x\nprint(4.double(), 3 |> double, [1, 2, 3].sum(), \"abc\".upper())",
    "string_iteration": 'n = 0\nfor ch in "hello" { if ch == "l" { n += 1 } }\nprint(n, "ell" in "hello")',
    "text_builtins": 'words = split("  the quick  brown fox ")\nprint(words, split("a,b,,c", ","), split("abc", ""), chars("héllo"))\n'
                     'print(join(words, "-"), join([1, 2, 3], ", "), join([2.5, 1.0], "; "), join([true, false], "|"), join(", ", ["x", "y"]), join([[1, 2], [3]], ";"), join([["a"], ["b", "c"]], " "))\n'
                     'print(replace("banana", "an", "AN"), replace("ab", "", "."), starts_with("jarlang", "jar"), ends_with("jarlang", "ng"))\n'
                     'print(ord("A"), ord("é"), chr(9731), chr(65) + chr(233), reverse("héllo"), reverse([1, 2, 3]))\n'
                     'print(sort([3, 1, 2]), sort([2.5, -1.0, 0.5]), sort(["pear", "apple", "fig"]), sort([true, false]))\n'
                     'print(index_of([5, 6, 7], 7), index_of([5, 6], 9), index_of(["a", "b"], "b"), index_of([1.5, 2.5], 2.5), index_of("héllo", "llo"), index_of("abc", "z"))\n'
                     'xs = [3, 1, 2]\nys = sort(xs)\nprint(xs, ys, [w.upper() for w in words].join(" "))',
    "bool_print":"print(true, false, 1 == 1, nil)",
    "write": 'write("a", 1)\nwrite(" b")\nprint()\nprint("done")',
    "chained": "fn check(v) => 0 <= v < 10\nprint(check(-1), check(0), check(9), check(10))",
    "shadowed_global_constants": "let e = 5\nprint(e + 1, pi > 3)",
    "nil_results": "fn f() { x = 5 }\ny = f()\nprint(f() == nil, y != nil, not f())\nif f() { print(1) } else { print(2) }",
    "user_range": "fn range(n) => [n, n]\nfor i in range(3) { print(i) }\nprint([i for i in range(2)])",
    "range_ends": "x = 9223372036854775806\nfor i in x..9223372036854775807 { print(i) }\n"
                  "for i in range(x, 9223372036854775807, 5) { print(i) }\n"
                  "for i in range(-x, -x - 2, -5) { print(i) }",
    "negative_default": "fn f(x = -1, y = -2.5) => x * y\nprint(f(), f(2))",
    "float_factorial": "print(20.0!, 25.0!, 170.0!, factorial(100.0))",
    "format_fixes": 'x = 65\ny = -inf\nprint("{x:5c}|{y:%}")',
    "round_edges": "print(round(1.5, 9223372036854775807), round(-5000000000000000000, -18), "
                   "round(4999999999999999999, -19))",
}


@pytest.mark.parametrize("name", sorted(PROGRAMS))
def test_differential(name):
    requires_native()
    code = PROGRAMS[name]
    expected = run_program(code)
    result = run_native(Source(code, f"{name}.jlang"))
    assert result.stderr == ""
    assert result.returncode == 0
    assert result.stdout == expected


@pytest.mark.parametrize("name", NATIVE_EXAMPLES)
def test_examples_differential(name):
    requires_native()
    path = EXAMPLES / f"{name}.jlang"
    code = path.read_text(encoding="utf-8")
    expected = run_program(code, str(path))
    result = run_native(Source(code, str(path)))
    assert (result.returncode, result.stdout) == (0, expected)


@pytest.mark.parametrize("code, message", [
    ("x = 10\nprint(x // (x - 10))", "division by zero"),
    ("x = 7\nprint(x % 0)", "modulo by zero"),
    ("x = 2.0\nprint(x / 0)", "division by zero"),
    ("x = 9223372036854775807\nprint(x + 1)", "integer overflow"),
    ("fn f(n) => n * n * n * n\nprint(f(100000))", "integer overflow"),
    ("xs = [1, 2, 3]\nprint(xs[3])", "index 3 is out of range for list of length 3"),
    ("xs = [1, 2, 3]\nprint(xs[-4])", "index -4 is out of range for list of length 3"),
    ("x = -4.0\nprint(√x)", "square root of a negative number (-4.0)"),
    ("print(ln(0.0))", "ln(0.0) is undefined"),
    ("x = -1\nprint(2^x)", "negative exponent"),
    ("n = 25\nprint(n!)", "integer overflow"),
    ("xs = []\npush(xs, 1)\npop(xs)\npop(xs)", "pop() from an empty list"),
    ("assert(1 > 2, \"nope\")", "nope"),
    ('print(int("12x"))', 'cannot parse "12x" as an integer'),
    ('print(int("99999999999999999999"))', "integer overflow"),
    ("x = -9223372036854775807 - 1\nprint(gcd(x, 0))", "integer overflow"),
    ("print(round(9223372036854775807, -19))", "integer overflow"),
    ('x = 200000000\nprint(len("ab" * x))', "result of `*` is too large (400,000,000 items)"),
    ("x = 4611686018427387904\nprint(len([1, 2] * x))", "result of `*` is too large (9,223,372,036,854,775,808 items)"),
    ("print(sqrt(-1))", "square root of a negative number (-1)\n"),
    ("print(exp(710))", "exp(710) is undefined"),
    ("print(log(0))", "log(0) is undefined"),
    ("print(log(8, 1))", "invalid logarithm base 1\n"),
    ("print(floor(inf))", "cannot take floor of inf"),
    ("print(trunc(nan))", "cannot truncate nan"),
    ('x = 1234567\nprint("{x:c}")', "invalid format spec 'c' for int"),
    ("x = 0\nprint(len(chr(x)))", "chr(0) is not supported by the native compiler"),
    ("fn d(n) => if n == 0 { 0 } else { 1 + d(n - 1) }\nprint(d(1000000))", "maximum recursion depth exceeded"),
])
def test_native_runtime_errors(code, message):
    requires_native()
    result = run_native(Source(code, "err.jlang"))
    assert result.returncode == 1
    assert message in result.stderr
    assert "err.jlang:" in result.stderr


@pytest.mark.parametrize("code", [
    "print(k)\nk = 3",
    "fn f(c) {\n  if c { y = 1 }\n  y\n}\nprint(f(true))\nprint(f(false))",
    "for i in 1..0 { }\nprint(i)",
    "fn show() => print(total)\nshow()\ntotal = 5",
    "fn show() => print(total)\ntotal = 5\nshow()",
    "x = 1\nif x > 0 { z = 2 } else { z = 3 }\nprint(z)",
    "n = 0\nwhile n < 3 { if n == 2 { print(w) }; w = n * 10; n += 1 }",
])
def test_use_before_assignment_matches_interpreter(code):
    requires_native()
    from support import run_error
    try:
        expected_out, expected_err = run_program(code), None
    except Exception:
        expected_out, expected_err = None, run_error(code).message
    result = run_native(Source(code, "da.jlang"))
    if expected_err is None:
        assert (result.returncode, result.stdout) == (0, expected_out)
    else:
        assert result.returncode == 1 and expected_err in result.stderr


def test_definitely_assigned_variables_are_not_checked():
    text = asm((EXAMPLES / "benchmark.jlang").read_text(encoding="utf-8"))
    assert "is used before" not in text


def test_output_before_a_runtime_error_is_flushed():
    requires_native()
    result = run_native(Source('print("before")\nx = 0\nprint(1 // x)', "flush.jlang"))
    assert result.stdout == "before\n"
    assert result.returncode == 1


def test_exit_code():
    requires_native()
    result = run_native(Source("print(1)\nexit(7)\nprint(2)", "exit.jlang"))
    assert (result.returncode, result.stdout) == (7, "1\n")


def test_toolchain_is_reported_by_doctor():
    from jarlang.native.toolchain import describe_toolchains
    rows = describe_toolchains()
    assert rows and all({"name", "ok", "detail"} <= set(r) for r in rows)
    if native_toolchain() is not None:
        assert any(r["ok"] for r in rows)


def test_recursion_limit_matches_interpreter():
    requires_native()
    from jarlang.interpreter import MAX_DEPTH, run_with_big_stack
    code = f"fn d(n) => if n == 0 {{ 0 }} else {{ 1 + d(n - 1) }}\nprint(d({MAX_DEPTH - 1}))"
    # A big stack like the CLI uses, since Python 3.10 on Windows runs out on the main thread
    expected = run_with_big_stack(lambda: run_program(code))
    assert run_native(Source(code, "deep.jlang")).stdout == expected == f"{MAX_DEPTH - 1}\n"


FORMAT_SPECS = """
a = 123.456
z = 0.0
h = 1.5
c = 100.0
d = 1234.5
s1 = 0.0001234
s2 = 0.00001
t = 2.5
n42 = 42
one = 1
m = -1234
ff = 255
nh = -1.5
three = 3
seven = 7
big = 1e300
nz = -0.0
e16 = 1e16
tw = 12.0
print("[{a:10.3}] [{z:.1}] [{h:.0}] [{c:.3}] [{d:.10}] [{s1:.3}] [{s2:.3}] [{t:.2}]")
print("[{n42:^012X}] [{h:08,.1f}] [{"hi":05}] [{one:05,}] [{one:04,}] [{m:010,}] [{ff:09_x}]")
print("[{nh:09,.2f}] [{d:012,.1f}] [{three:*^05}] [{seven:<05}] [{nz:.3}] [{e16:.17}] [{tw:.2}]")
print(len("{big:.300f}"))
"""


def test_format_specs_match_interpreter():
    requires_native()
    result = run_native(Source(FORMAT_SPECS, "fmt.jlang"))
    assert (result.returncode, result.stdout) == (0, run_program(FORMAT_SPECS))
