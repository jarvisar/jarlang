import math

import pytest

from support import evaluate, run_error, run_program

# (program, expected output)
CASES = [
    # arithmetic and numbers
    ("print(1 + 2 * 3, (1 + 2) * 3, 2^10, 2**3)", "7 9 1024 8"),
    ("print(7 / 2, 8 / 2, 7 // 2, -7 // 2, 7 % 3, -7 % 3, 7 % -3)", "3.5 4.0 3 -4 1 2 -2"),
    ("print(7.5 // 2, -7.5 // 2, 7.5 % 2, -7.5 % 2)", "3.0 -4.0 1.5 0.5"),
    ("print(2^-1, 2^0.5, (-8)^3, 10^20)", "0.5 1.4142135623730951 -512 100000000000000000000"),
    ("print(5!, 0!, 20!, 0.5!)", f"120 1 2432902008176640000 {math.gamma(1.5)}"),  # last digit varies by libm
    ("print(√16, √2, -√9)", "4.0 1.4142135623730951 -3.0"),
    ("x = 3\nprint(2x, 2x^2, 3(x + 1), 2√16)", "6 18 12 8.0"),
    ("print(1_000 + 0xFF + 0b11 + 0o10, 1.5e3)", "1266 1500.0"),
    ("print(0.1 + 0.2, 1 / 3, 1e16, 1e-5, 1.0, -0.0)", "0.30000000000000004 0.3333333333333333 1e+16 1e-05 1.0 -0.0"),
    ("print(30!)", "265252859812191058636308480000000"),
    ("print(3 × 4, 8 ÷ 2, 1 ≤ 2, 3 ≥ 4, 1 ≠ 2)", "12 4.0 true false true"),
    # comparison and logic
    ("print(1 < 2 < 3, 1 < 3 < 2, 0 < 5 <= 5 != 6)", "true false true"),
    ("print(1 == 1.0, true == 1, [1, 2] == [1, 2], nil == nil, \"a\" < \"b\")", "true false true true true"),
    ("print(true and false, true or false, not true, !false)", "false true false true"),
    ("print(nil or 5, 0 or \"x\", 3 and 4, [] or [1])", "5 x 4 [1]"),
    ("print(2 in [1, 2], 5 not in [1, 2], \"ell\" in \"hello\", 3 in 1..5, \"k\" in {k: 1})", "true true true true true"),
    # strings
    ('name = "Ada"\nprint("Hi {name}! {1 + 1} {name.upper()}")', "Hi Ada! 2 ADA"),
    ("print('raw {x}', \"esc \\{x\\}\", \"tab\\tend\")", "raw {x} esc {x} tab\tend"),
    ('print("{pi:.3f}|{42:>5}|{42:<5}|{42:^6}|{7:03}|{255:x}|{0.25:.0%}|{1234567:,}")',
     "3.142|   42|42   |  42  |007|ff|25%|1,234,567"),
    ('print("{true} {nil} {[1, "a"]} {3.0}")', 'true nil [1, "a"] 3.0'),
    ('print("ab" * 3, "a" + "b", len("héllo"), "abc"[1], "abc"[-1], "hello"[1:3])', "ababab ab 5 b c el"),
    # lists
    ("xs = [3, 1, 2]\nprint(xs[0], xs[-1], len(xs), sort(xs), xs, reverse(xs))", "3 2 3 [1, 2, 3] [3, 1, 2] [2, 1, 3]"),
    # all arguments are evaluated before the call, and lists are shared references
    ("xs = [1, 2]\npush(xs, 3)\nxs[0] = 9\nprint(xs, pop(xs), xs)", "[9, 2] 3 [9, 2]"),
    ("print([1, 2] + [3], [0] * 3, [1, 2, 3, 4][1:3], [1, 2, 3][::-1])", "[1, 2, 3] [0, 0, 0] [2, 3] [3, 2, 1]"),
    ("print([x^2 for x in 1..5 if x % 2 == 1])", "[1, 9, 25]"),
    ("print(sum([1, 2, 3]), product(1..5), min(4, 2, 8), max([4, 2, 8]), mean([1, 2, 3, 4]))", "6 120 2 8 2.5"),
    ("print(zip([1, 2], [\"a\", \"b\"]), enumerate([\"x\", \"y\"], 1))", '[[1, "a"], [2, "b"]] [[1, "x"], [2, "y"]]'),
    ("print(unique([1, 2, 1, 3, 2]), flatten([[1], [2, 3]]), chunk(1..5, 2), take(1..9, 3), drop([1, 2, 3], 2))",
     "[1, 2, 3] [1, 2, 3] [[1, 2], [3, 4], [5]] [1, 2, 3] [3]"),
    ("print(first([7, 8]), last([7, 8]), first([]), index_of([5, 6], 6), count([1, 1, 2], 1))", "7 8 nil 1 2"),
    # dicts
    ('d = {a: 1, "b c": 2}\nd.z = 3\nd["q"] = 4\nprint(d, d.a, d["b c"], len(d))', '{a: 1, "b c": 2, z: 3, q: 4} 1 2 4'),
    ('d = {x: 1}\nprint(keys(d), values(d), items(d), get(d, "y", 0), has(d, "x"))', '["x"] [1] [["x", 1]] 0 true'),
    ("d = {1: \"one\", 2.5: \"x\", nil: 0}\nprint(d[1], d[1.0], d[2.5], d[nil])", "one one x 0"),
    # ranges
    ("print(list(1..5), list(1..<5), list(range(5)), list(range(10, 0, -3)), 1..3)", "[1, 2, 3, 4, 5] [1, 2, 3, 4] [0, 1, 2, 3, 4] [10, 7, 4, 1] 1..<4"),
    # control flow
    ("t = 0\nfor i in 1..10 { if i % 2 == 0 { continue }; if i > 7 { break }; t += i }\nprint(t)", "16"),
    ("i = 0\nwhile true { i += 1; if i == 5 { break } }\nprint(i)", "5"),
    ("x = if 3 > 2 { \"yes\" } else { \"no\" }\nprint(x)", "yes"),
    ("fn sign(n) { if n > 0 { 1 } elif n < 0 { -1 } else { 0 } }\nprint(sign(5), sign(-2), sign(0))", "1 -1 0"),
    ("for k, v in items({a: 1, b: 2}) { write(k, v, \"\") }\nprint()", "a 1 b 2 "),
    ("for c in \"abc\" { write(c) }\nprint()", "abc"),
    # functions
    ("f(x) = x^2 + 1\nprint(f(3))", "10"),
    ("fn add(a, b = 10) => a + b\nprint(add(1), add(1, 2))", "11 3"),
    ("fn fact(n) { if n <= 1 { return 1 }; n * fact(n - 1) }\nprint(fact(25))", "15511210043330985984000000"),
    ("fn f() { return }\nprint(f())", "nil"),
    ("fn f() { }\nprint(f())", "nil"),
    ("print(twice(3))\nfn twice(x) => 2x", "6"),  # functions are hoisted
    ("adder = fn(n) => fn(x) => x + n\nadd5 = adder(5)\nprint(add5(10), adder(1)(1))", "15 2"),
    ("fn counter() { n = 0; fn() { n += 1; n } }\nc = counter()\nc()\nc()\nprint(c())", "3"),
    ("print([1, 2, 3].map(fn(x) => x * 10), 1..6 |> filter(fn(x) => x % 2 == 0) |> sum)", "[10, 20, 30] 12"),
    ("print(map(fn(x) => x + 1, [1, 2]), reduce([1, 2, 3], fn(a, b) => a * b), find([1, 5, 9], fn(x) => x > 3))", "[2, 3] 6 5"),
    ("sq = compose(fn(x) => x + 1, fn(x) => x * x)\nprint(sq(3))", "10"),
    ("fn fib(n) { if n < 2 { return n }; fib(n - 1) + fib(n - 2) }\nfib = memoize(fib)\nprint(fib(80))", "23416728348467685"),
    ("print(sort([\"bb\", \"a\", \"ccc\"], len), sort([3, 1, 2], fn(x) => -x))", '["a", "bb", "ccc"] [3, 2, 1]'),
    # scoping
    ("count = 0\nfn bump() { count += 1 }\nbump()\nbump()\nprint(count)", "2"),
    ("x = 1\nfn f() { let x = 2; x }\nprint(f(), x)", "2 1"),
    ("fn outer() { a = 1; fn inner() { a += 1 }; inner(); a }\nprint(outer())", "2"),
    ("for i in 1..3 { }\nprint(i)", "3"),
    ("print([y for y in 1..3], \"ok\")", "[1, 2, 3] ok"),
    # multiple assignment
    ("a, b = 1, 2\na, b = b, a\nprint(a, b)", "2 1"),
    ("x, y = [10, 20]\nprint(x + y)", "30"),
    ("xs = [1, 2, 3]\nxs[1] += 5\nd = {n: 1}\nd.n *= 4\nprint(xs, d)", "[1, 7, 3] {n: 4}"),
    # error handling
    ("try { 1 / 0 } catch e { print(\"caught\", e) }", "caught division by zero"),
    ("try { throw {code: 7} } catch e { print(e.code) }", "7"),
    ("try { error(\"boom\") } catch e { print(e) }", "boom"),
    ("fn f() { try { return 1 } catch e { return 2 } }\nprint(f())", "1"),
    # type annotations
    ("fn half(x: float) -> float => x / 2\nprint(half(3))", "1.5"),
    ("let r: float = 2\nprint(r)", "2.0"),
    # conversions and other builtins
    ("print(int(3.9), int(-3.9), int(\"42\"), float(\"1.5\"), str(12) + \"!\", bool(0), type(1..2))", "3 -3 42 1.5 12! false range"),
    ("print(round(2.5), round(-2.5), round(0.5), round(3.14159, 2), round(1234, -2), round(2.675, 2))", "3 -3 1 3.14 1200 2.68"),
    ("print(floor(-2.5), ceil(2.1), trunc(-2.7), abs(-3), sign(-0.5), clamp(15, 0, 10))", "-3 3 -2 3 -1 10"),
    ("print(gcd(12, 18), lcm(4, 6), isqrt(99), is_prime(97), primes(20), factors(84), divisors(12))",
     "6 12 9 true [2, 3, 5, 7, 11, 13, 17, 19] [2, 2, 3, 7] [1, 2, 3, 4, 6, 12]"),
    ("print(choose(5, 2), perm(5, 2), hex(255), bin(5), fmt(pi, \".2f\"), repr(\"a\"))", '10 20 0xff 0b101 3.14 "a"'),
    ("print(log(e), log(8, 2), log2(1024), log10(1000), exp(0), deg(pi), rad(180) == pi)", "1.0 3.0 10.0 3.0 1.0 180.0 true"),
    ("print(median([3, 1, 2]), mode([1, 2, 2]), stdev([2, 4, 4, 4, 5, 5, 7, 9]) > 2)", "2 2 true"),
    ('print(split("a,b", ","), join([1, 2], "+"), replace("aaa", "a", "b"), trim("  x "), chars("hi"), lines("a\\nb"))',
     '["a", "b"] 1+2 bbb x ["h", "i"] ["a", "b"]'),
    ('print(pad(5, 3, "0"), pad("x", -3) + "|", ord("a"), chr(98), is_digit("12"), is_alpha("ab1"))', "005 x  | 97 b true false"),
    ("seed(1)\na = random()\nseed(1)\nprint(a == random(), 0 <= randint(1, 6) - 1 < 6)", "true true"),
    ("print(linspace(0, 1, 5))", "[0.0, 0.25, 0.5, 0.75, 1.0]"),
    ("print(pi, π, tau == 2pi, e, phi, inf, -inf)", f"{math.pi} {math.pi} true {math.e} {(1 + 5 ** 0.5) / 2} inf -inf"),
    ("print(type(print), doc(sqrt).split(\"\\n\")[0])", "fn sqrt(x)"),
]


@pytest.mark.parametrize("code, expected", CASES, ids=[c[0][:40] for c in CASES])
def test_program_output(code, expected):
    assert run_program(code).rstrip("\n") == expected


def test_final_expression_value():
    assert evaluate("2^10 + 5!") == 1144
    assert evaluate("x = 3\nx * 2") == 6
    assert evaluate("[1, 2] + [3]") == [1, 2, 3]
    assert evaluate("print(1)\nx = 2") is None


def test_python_api():
    import jarlang
    assert jarlang.evaluate("2^10 + 5!") == 1144


def test_deep_recursion_is_reported_not_crashing():
    from jarlang.interpreter import run_with_big_stack
    # Needs the big stack the CLI uses: Python 3.10 on Windows overflows the C stack first
    err = run_with_big_stack(lambda: run_error("fn f(n) => f(n + 1)\nf(0)"))
    assert "recursion" in err.message


def test_moderately_deep_recursion_works():
    from jarlang.interpreter import run_with_big_stack
    out = run_with_big_stack(lambda: run_program("fn down(n) { if n == 0 { return 0 }; down(n - 1) }\nprint(down(2000))"))
    assert out.strip() == "0"


def test_input_reads_stdin():
    import io
    from jarlang.interpreter import Interpreter
    out = io.StringIO()
    interp = Interpreter(stdout=out, stdin=io.StringIO("Ada\n"))
    interp.run('name = input("who? ")\nprint("hi " + name)')
    assert out.getvalue() == "who? hi Ada\n"


def test_exit_builtin():
    from jarlang.builtins import ProgramExit
    with pytest.raises(ProgramExit) as info:
        run_program("print(1)\nexit(3)\nprint(2)")
    assert info.value.code == 3


def test_interpreter_keeps_globals_between_runs():
    import io
    from jarlang.interpreter import Interpreter
    interp = Interpreter(stdout=io.StringIO())
    interp.run("x = 20")
    interp.run("fn double(n) => 2n")
    assert interp.run("double(x) + 2") == 42


def test_imports(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shapes.jlang").write_text("area(r) = 3r^2\n_secret = 1\nunit = 1\n", encoding="utf-8")
    main = tmp_path / "main.jlang"
    main.write_text('import "lib/shapes"\nimport "lib/shapes.jlang" as s\nprint(area(2), s.area(1), s.unit, has(s, "_secret"))\n',
                    encoding="utf-8")
    import io
    from jarlang.interpreter import Interpreter
    from jarlang.source import Source
    out = io.StringIO()
    Interpreter(stdout=out).run(Source(main.read_text(encoding="utf-8"), str(main)))
    assert out.getvalue() == "12 3 1 false\n"


def test_import_missing_module(tmp_path):
    import io
    from jarlang.errors import JarLangError
    from jarlang.interpreter import Interpreter
    from jarlang.source import Source
    main = tmp_path / "main.jlang"
    with pytest.raises(JarLangError) as info:
        Interpreter(stdout=io.StringIO()).run(Source('import "nope" as n\nprint(1)', str(main)))
    assert "nope" in info.value.message


def test_plot_draws_braille():
    out = run_program("plot(fn(x) => x^2, -1, 1, 20, 5)")
    lines = out.splitlines()
    assert len(lines) == 5 + 2
    assert any(0x2800 <= ord(ch) <= 0x28FF and ch != "\u2800" for ch in out)
    assert "┤" in lines[0] and "└" in lines[5]


def test_help_lists_builtins():
    out = run_program("help()")
    assert "math:" in out and "sqrt" in out


# Regression tests for bugs found in review
@pytest.mark.parametrize("code, expected", [
    ("print([true] == [1], {a: true} != {a: 1}, [1, 2] == [1.0, 2.0])", "false true true"),
    ("print([1, 2] < [1, 3], [2] > [1, 9], 2.0 in 1..3)", "true true true"),
    ("print((-2)^inf, inf!, round(1.5, 30), round(10^40 + 5, -1))", "inf inf 1.5 10000000000000000000000000000000000000010"),
    ("print(unique([1, true, 1.0, false, 0]))", "[1, true, false, 0]"),
    ("d = {len: fn(x) => 99}\nprint(d.len(1))", "99"),
    ("fn f() { t = 0.0; t += 1; t *= 3; t }\nprint(f(), 4.0 / 2.0)", "3.0 2.0"),
])
def test_review_regressions(code, expected):
    assert run_program(code).rstrip("\n") == expected


@pytest.mark.parametrize("code, message", [
    ('print([1, "a"] < [1, 2])', "cannot compare str and int"),
    ('print("a" * 10^20)', "too large"),
    ("print((10^400)^-1)", "too large to convert"),
    ("print(√(10^400))", "too large to convert"),
    ('print(int("ff", "16"))', "integer base"),
    ('print(round(5, "a"))', "digit count"),
    ("seed([1])", "number or string"),
    ('print("{99999999999:c}")', "invalid format spec"),
    ("print(2 + ²)", "unexpected character"),
    ("fn f(x: foo) => x", "unknown type `foo`"),
    ("fn f(x: list[strr]) => x", "unknown type `strr`"),
])
def test_review_errors_are_jarlang_errors(code, message):
    assert message in run_error(code).message


def test_type_names_match_interpreter_checks():
    from jarlang.interpreter import _TYPE_CHECKS
    from jarlang.resolver import TYPE_NAMES
    assert set(TYPE_NAMES) == set(_TYPE_CHECKS)


def test_transitive_and_function_imports(tmp_path):
    import io
    from jarlang.interpreter import Interpreter
    from jarlang.source import Source
    (tmp_path / "c.jlang").write_text("cf() = 42\n", encoding="utf-8")
    (tmp_path / "a.jlang").write_text('import "c"\naf() = cf()\n', encoding="utf-8")
    main = tmp_path / "main.jlang"
    code = 'import "a"\nfn load() { import "c" }\nload()\nprint(af(), cf())\n'
    main.write_text(code, encoding="utf-8")
    out = io.StringIO()
    Interpreter(stdout=out).run(Source(code, str(main)))
    assert out.getvalue() == "42 42\n"


def test_ctrl_c_stops_the_program_thread():
    import _thread
    import threading
    import time

    from jarlang.interpreter import Interpreter, run_with_big_stack
    from jarlang.source import Source

    interp = Interpreter()
    compiled = interp.compile(Source("i = 0\nwhile true { i += 1 }\n", "<test>"))
    threading.Timer(0.3, _thread.interrupt_main).start()  # what Ctrl+C does
    with pytest.raises(KeyboardInterrupt):
        run_with_big_stack(compiled.run)
    count = interp.globals["i"]
    time.sleep(0.2)
    assert interp.globals["i"] == count


def test_files_saved_with_a_bom(tmp_path):
    # Notepad adds a byte order mark, which used to be read as an unexpected character
    import io

    from jarlang.interpreter import Interpreter
    from jarlang.source import Source
    (tmp_path / "lib.jlang").write_text("two = 2\n", encoding="utf-8-sig")
    (tmp_path / "data.txt").write_text("hi", encoding="utf-8-sig")
    (tmp_path / "latin1.txt").write_bytes("caf\xe9".encode("latin-1"))
    main = tmp_path / "main.jlang"
    code = f'import "lib"\nprint(two, read_file({str(tmp_path / "data.txt")!r}))\n'
    main.write_text(code, encoding="utf-8")
    out = io.StringIO()
    Interpreter(stdout=out).run(Source(code, str(main)))
    assert out.getvalue() == "2 hi\n"
    assert "not a UTF-8 text file" in run_error(f'read_file({str(tmp_path / "latin1.txt")!r})').message


def test_let_initializer_reads_the_outer_variable():
    assert run_program("x = 5\nfn f() { let x = x + 1\n x }\nprint(f(), x)") == "6 5\n"
    assert run_program("fn f(x) { let x = x + 1\n x }\nprint(f(1))") == "2\n"
    assert run_program("let pi = pi * 2\nprint(pi > 6)") == "true\n"
    assert "undefined variable `y`" in run_error("fn f() { let y = y + 1 }").message


def test_method_call_only_uses_function_fields():
    assert run_program("print({len: 3}.len(), {double: fn(x) => 2x}.double(4))") == "1 8\n"


def test_imported_constants_stay_constant(tmp_path):
    import io

    from jarlang.errors import JarLangError
    from jarlang.interpreter import Interpreter
    from jarlang.source import Source
    (tmp_path / "m.jlang").write_text("const limit = 10\n", encoding="utf-8")
    main = tmp_path / "main.jlang"
    main.write_text("", encoding="utf-8")
    with pytest.raises(JarLangError) as info:
        Interpreter(stdout=io.StringIO()).run(Source('import "m"\nlimit = 1', str(main)))
    assert "cannot assign to constant `limit`" in info.value.render(color=False)
    out = io.StringIO()
    Interpreter(stdout=out).run(Source('import "m"\nlet limit = limit + 1\nprint(limit)', str(main)))
    assert out.getvalue() == "11\n"
