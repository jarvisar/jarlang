"""Tests for jarlang.web, which the playground calls through Pyodide."""

from __future__ import annotations

import json
import sys

import pytest

from jarlang import __version__, web


# Run

def test_run_captures_stdout_and_final_value():
    r = web.run('print("hello")\nx = 6\nx * 7')
    assert r["ok"] is True
    assert r["stdout"] == "hello\n"
    assert r["stderr"] == ""
    assert r["value"] == "42"
    assert r["value_type"] == "int"
    assert r["exit_code"] == 0
    assert r["diagnostics"] == []
    assert r["truncated"] is False
    assert isinstance(r["time_ms"], float) and r["time_ms"] >= 0


def test_run_value_is_a_repr_and_absent_for_statements():
    assert web.run('"hi"')["value"] == '"hi"'
    assert web.run("[1, 2.5, nil]")["value"] == "[1, 2.5, nil]"
    assert web.run("x = 1")["value"] is None
    assert web.run("nil")["value"] is None
    assert web.run("")["ok"] is True


def test_run_math_features():
    r = web.run('print(√16, 5!, 2^10)\nf(x) = x^2 + 1\nprint("{f(3)}")\n1..10 |> filter(is_prime) |> sum')
    assert r["ok"], r["stderr"]
    assert r["stdout"] == "4.0 120 1024\n10\n"
    assert r["value"] == "17"


def test_run_plot_output_is_braille():
    r = web.run("plot(fn(x) => sin(x), -pi, pi)")
    assert r["ok"], r["stderr"]
    assert any(0x2800 <= ord(ch) <= 0x28FF for ch in r["stdout"])


def test_run_seed_makes_random_deterministic():
    code = "[randint(1, 1000000), randint(1, 1000000), random()]"
    assert web.run(code, seed=7)["value"] == web.run(code, seed=7)["value"]
    assert web.run(code, seed=7)["value"] != web.run(code, seed=8)["value"]


def test_run_reads_stdin_lines_and_echoes_them():
    r = web.run('name = input("Name? ")\nprint("Hi {name}")', stdin_lines=["Ada", "unused"])
    assert r["ok"]
    assert r["stdout"] == "Name? Ada\nHi Ada\n"
    assert r["stdin_exhausted"] is False


def test_run_reports_exhausted_stdin():
    r = web.run('a = input("A? ")\nb = input("B? ")\nprint("[{a}] [{b}]")', stdin_lines=["x"])
    assert r["ok"]
    assert r["stdout"] == "A? x\nB? \n[x] []\n"
    assert r["stdin_exhausted"] is True


def test_run_exit_is_a_normal_result_with_exit_code():
    r = web.run('print("bye")\nexit(3)\nprint("never")')
    assert r["ok"] is True
    assert r["exit_code"] == 3
    assert r["stdout"] == "bye\n"
    assert r["stderr"] == ""


def test_run_syntax_error_is_rendered_without_color():
    r = web.run("x = (1 +\nprint(x")
    assert r["ok"] is False
    assert r["error_kind"] == "syntax error"
    assert r["stderr"].startswith("error:")
    assert "\x1b[" not in r["stderr"]
    assert "--> main.jlang:" in r["stderr"]
    d = r["diagnostics"][0]
    assert d["severity"] == "error" and d["line"] >= 1 and d["column"] >= 1


def test_run_name_error_lists_every_diagnostic_with_positions():
    r = web.run("print(lenght([1]))\nprint(nope)")
    assert r["ok"] is False
    messages = [d["message"] for d in r["diagnostics"]]
    assert messages == ["undefined variable `lenght`", "undefined variable `nope`"]
    first = r["diagnostics"][0]
    assert (first["line"], first["column"], first["end_line"], first["end_column"]) == (1, 7, 1, 13)
    assert "did you mean `len`?" in first["helps"]
    assert "did you mean `len`?" in r["stderr"]


def test_run_runtime_error_keeps_output_printed_before_it():
    r = web.run('print("before")\nfn f(x) { x / 0 }\nf(1)')
    assert r["ok"] is False
    assert r["stdout"] == "before\n"
    assert r["error_kind"] == "runtime error"
    assert "division by zero" in r["stderr"]
    assert r["diagnostics"][0]["line"] == 2


def test_run_uncaught_throw():
    r = web.run('throw "boom"')
    assert r["ok"] is False
    assert "boom" in r["stderr"]


def test_run_returns_warnings():
    r = web.run("fn f() {\n  y = 1\n  0\n}\nf()")
    assert r["ok"]
    assert [w["message"] for w in r["warnings"]] == ["unused variable `y`"]
    assert r["warnings"][0]["severity"] == "warning"


def test_run_truncates_huge_output():
    r = web.run('for i in 1..2000 { print("line {i}") }', max_output=1000)
    assert r["ok"] is True
    assert r["truncated"] is True
    kept, _, note = r["stdout"].partition("\n… output truncated")
    assert len(kept) == 1000
    assert kept.startswith("line 1\nline 2\n")
    assert "showing the first 1,000 of" in note


def test_run_default_output_limit_is_about_a_megabyte():
    r = web.run('s = "x" * 1000\nfor i in 1..1200 { print(s) }')
    assert r["ok"]
    assert r["truncated"] is True
    assert web.MAX_OUTPUT <= len(r["stdout"]) < web.MAX_OUTPUT + 200


def test_run_stops_a_program_that_prints_forever():
    r = web.run('while true { print("spam") }', max_output=100, hard_output_limit=10_000)
    assert r["ok"] is False
    assert r["error_kind"] == "output"
    assert "stopped after printing more than 10,000 characters" in r["stderr"]
    assert r["truncated"] is True
    assert r["stdout"].startswith("spam\n")


def test_run_output_limit_cannot_be_caught_by_the_program():
    code = 'try {\n  while true { print("x") }\n} catch e {\n  print("caught")\n}'
    r = web.run(code, max_output=10, hard_output_limit=1000)
    assert r["error_kind"] == "output"
    assert "caught" not in r["stdout"]


def test_run_truncates_a_huge_final_value(monkeypatch):
    monkeypatch.setattr(web, "MAX_VALUE", 50)
    r = web.run("[1] * 100")
    assert r["value"].startswith("[1, 1, 1")
    assert "more characters" in r["value"]


def test_run_deep_recursion_is_allowed_up_to_max_depth():
    code = "fn f(n) { for i in [1] { if n > 0 { return f(n - 1) } }\n 0 }\nf({n})"
    assert web.run(code.replace("{n}", "990"))["value"] == "0"
    r = web.run(code.replace("{n}", "5000"))
    assert r["ok"] is False
    assert "maximum recursion depth exceeded" in r["stderr"]


def test_run_rejects_absurdly_nested_source_gracefully():
    r = web.run("x = " + "[" * 20000 + "]" * 20000)
    assert r["ok"] is False
    assert r["error_kind"] == "recursion"
    assert "nested too deeply" in r["stderr"]


def test_run_restores_the_recursion_limit():
    before = sys.getrecursionlimit()
    web.run("fn f(n) { if n == 0 { return 0 }\n 1 + f(n - 1) }\nf(100)")
    web.run("x = " + "(" * 5000 + "1" + ")" * 5000)
    assert sys.getrecursionlimit() == before


def test_run_streams_output_to_the_listener():
    chunks: list[str] = []
    web.set_listener(chunks.append)
    try:
        r = web.run('for i in 1..3 { print(i) }', stream=True)
        silent = web.run('print("quiet")')  # stream=False: the listener is not called
    finally:
        web.set_listener(None)
    assert "".join(chunks) == r["stdout"] == "1\n2\n3\n"
    assert silent["stdout"] == "quiet\n"
    assert "quiet" not in "".join(chunks)


def test_run_survives_a_broken_listener():
    def broken(chunk: str) -> None:
        raise RuntimeError("listener exploded")

    web.set_listener(broken)
    try:
        r = web.run('print("still fine")', stream=True)
    finally:
        web.set_listener(None)
    assert r["ok"] and r["stdout"] == "still fine\n"


def test_run_reports_internal_errors_instead_of_raising(monkeypatch):
    def explode(self, source, name="<input>"):
        raise ZeroDivisionError("simulated bug")

    monkeypatch.setattr(web._PlaygroundInterpreter, "compile", explode)
    r = web.run("1 + 1")
    assert r["ok"] is False
    assert r["error_kind"] == "internal"
    assert "simulated bug" in r["stderr"]


def test_capture_counts_and_streams():
    seen: list[str] = []
    cap = web._Capture(limit=5, hard_limit=100, listener=seen.append, interval=0)
    cap.write("abc")
    cap.write("defgh")
    cap.flush()
    assert cap.getvalue() == "abcde"
    assert cap.truncated and cap.total == 8
    assert "".join(seen) == "abcde"
    with pytest.raises(web.OutputLimitExceeded):
        cap.write("x" * 200)


# Tokens and AST

def test_tokens():
    r = web.tokens("x = 2x + √16  # comment")
    assert r["ok"]
    kinds = [t["kind"] for t in r["tokens"]]
    assert kinds[:5] == ["IDENT", "ASSIGN", "INT", "IDENT", "PLUS"]
    assert "SQRT" in kinds and kinds[-1] == "EOF"
    first = r["tokens"][0]
    assert (first["text"], first["line"], first["column"], first["end_column"]) == ("x", 1, 1, 2)
    assert r["comments"] == [{"text": " comment", "line": 1, "column": 15}]
    assert r["text"].splitlines()[0].split() == ["1:1", "IDENT", "'x'"]


def test_tokens_lex_error_returns_partial_tokens():
    r = web.tokens("x = 1\ny = $")
    assert r["ok"] is False
    assert "unexpected character '$'" in r["stderr"]
    assert [t["kind"] for t in r["tokens"][:3]] == ["IDENT", "ASSIGN", "INT"]
    assert r["diagnostics"][0]["line"] == 2


def test_ast_tree():
    r = web.ast_tree("f(x) = x^2")
    assert r["ok"]
    lines = r["tree"].splitlines()
    assert lines[0] == "Program"
    assert "FnDecl fn f" in r["tree"] and "Binary ^" in r["tree"]
    assert "\x1b[" not in r["tree"]


def test_ast_tree_syntax_error():
    r = web.ast_tree("fn (")
    assert r["ok"] is False
    assert r["tree"] == ""
    assert r["stderr"].startswith("error:")


def test_ast_json_is_strict_json():
    r = web.ast_json("x = 1e999\ny = [1, 2]")
    assert r["ok"]
    tree = r["ast"]
    assert tree["type"] == "Program" and tree["line"] == 1
    assert tree["stmts"][0]["type"] == "Assign"
    json.dumps(r, allow_nan=False)  # infinity should be made JSON-safe
    assert "inf" in json.dumps(tree)


def test_ast_json_error():
    r = web.ast_json("x = ")
    assert r["ok"] is False and r["ast"] is None


def test_analysis_rejects_absurd_nesting_gracefully():
    deep = "x = " + "[" * 20000 + "]" * 20000
    for fn in (web.ast_tree, web.ast_json, web.check, web.explain):
        r = fn(deep)
        assert r["ok"] is False, fn.__name__
        assert "nested too deeply" in r["stderr"]


# Native compiler

FIB = "fn fib(n) {\n  if n < 2 { return n }\n  fib(n - 1) + fib(n - 2)\n}\nprint(fib(25))\n"


@pytest.mark.parametrize("target, marker", [
    ("linux", "System V"),
    ("windows", "Microsoft x64"),
    ("macos", "_jl_main"),
])
def test_asm_targets(target, marker):
    r = web.asm(FIB, target=target)
    assert r["ok"], r["stderr"]
    assert r["target"] == target
    assert marker in r["asm"]
    assert "# 5: print(fib(25))" in r["asm"]  # source lines are annotated
    assert r["lines"] > 20


def test_asm_defaults_to_linux_and_can_skip_the_optimizer():
    assert "System V" in web.asm(FIB)["asm"]
    assert web.asm(FIB, opt=False)["ok"]


def test_asm_unsupported_program_gets_a_friendly_diagnostic():
    r = web.asm('d = {name: "Ada"}\nprint(d.name)')
    assert r["ok"] is False
    assert r["asm"] == ""
    assert "not supported by the native compiler" in r["stderr"]
    assert "native compiler" in r["note"]
    assert all(d["line"] for d in r["diagnostics"])


def test_asm_syntax_error_has_no_native_note():
    r = web.asm("x = (")
    assert r["ok"] is False
    assert "note" not in r


def test_asm_unknown_target():
    r = web.asm(FIB, target="riscv")
    assert r["ok"] is False
    assert r["error_kind"] == "usage"
    assert "unknown target 'riscv'" in r["stderr"]


def test_explain():
    r = web.explain("sq(x) = x * x\nprint(sq(3))\nprint(sq(2.5))")
    assert r["ok"], r["stderr"]
    assert r["text"].startswith("native type inference for main.jlang")
    assert "fn sq(x: int) -> int" in r["text"]
    assert "fn sq(x: float) -> float" in r["text"]


def test_explain_unsupported_program():
    r = web.explain("xs = [1, 2].map(fn(x) => x)")
    assert r["ok"] is False
    assert r["text"] == ""
    assert "note" in r


# Check and format

def test_check_clean_code():
    r = web.check("x = 1\nprint(x)")
    assert r == {"ok": True, "diagnostics": [], "errors": 0, "warnings": 0, "time_ms": r["time_ms"]}


def test_check_errors_have_positions_for_squiggles():
    r = web.check("x = 1\nprint(lenght(x))\n")
    assert r["ok"] is False
    assert r["errors"] == 1
    d = r["diagnostics"][0]
    assert d["severity"] == "error"
    assert d["message"] == "undefined variable `lenght`"
    assert (d["line"], d["column"], d["end_line"], d["end_column"]) == (2, 7, 2, 13)
    assert d["label"] == "not found in this scope"
    assert d["rendered"].startswith("error: undefined variable")


def test_check_warnings_do_not_fail():
    r = web.check("fn f() {\n  return 1\n  print(2)\n}\nf()")
    assert r["ok"] is True
    assert r["warnings"] == 1
    assert r["diagnostics"][0]["message"] == "unreachable code"


def test_check_syntax_error():
    r = web.check("if x {")
    assert r["ok"] is False
    assert r["diagnostics"] and r["diagnostics"][0]["severity"] == "error"


def test_check_native_mode_reports_unsupported_features():
    code = 'd = {a: 1}\nprint(d)'
    assert web.check(code)["ok"] is True
    assert web.check(code, native=True)["ok"] is False


def test_diagnostic_dict_widens_zero_width_spans():
    from jarlang.errors import Diagnostic
    from jarlang.source import Source

    src = Source("abc", "t.jlang")
    d = web.diagnostic_dict(Diagnostic("error", "boom", src.span(3, 3)))
    assert (d["line"], d["column"], d["end_line"], d["end_column"]) == (1, 4, 1, 5)
    assert web.diagnostic_dict(Diagnostic("note", "no span"))["line"] is None


def test_format():
    r = web.format("x=1+2\nprint( x )\n")
    assert r["ok"] and r["changed"] is True
    assert r["code"] != "x=1+2\nprint( x )\n"
    again = web.format(r["code"])
    assert again["ok"] and again["changed"] is False and again["code"] == r["code"]


def test_format_syntax_error():
    r = web.format("x = (")
    assert r["ok"] is False
    assert r["code"] == "x = ("
    assert r["stderr"].startswith("error:")


# Examples and info

def test_examples_from_a_directory(tmp_path):
    (tmp_path / "zeta_demo.jlang").write_text("# Last one.\nprint(1)\n", encoding="utf-8")
    (tmp_path / "hello.jlang").write_text("\n# Say hello.\nprint(\"hi\")\n", encoding="utf-8")
    (tmp_path / "alpha.jlang").write_text("print(2)\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not an example", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helper.jlang").write_text("x = 1\n", encoding="utf-8")
    items = web.examples(str(tmp_path))
    assert [e["name"] for e in items] == ["hello", "alpha", "zeta_demo"]
    hello, alpha, zeta = items
    assert hello["title"] == "Hello" and hello["description"] == "Say hello."
    assert alpha["description"] == ""
    assert zeta["title"] == "Zeta demo"
    assert zeta["code"] == "# Last one.\nprint(1)\n"


def test_examples_missing_directory(tmp_path):
    assert web.examples(str(tmp_path / "nope")) == []


def test_bundled_examples_run():
    for ex in web.examples():
        assert set(ex) == {"name", "title", "description", "code"}
        if "input(" in ex["code"] or "import " in ex["code"] or "benchmark" in ex["name"]:
            continue
        r = web.run(ex["code"], stdin_lines=[])
        assert r["ok"], f"{ex['name']}: {r['stderr']}"


def test_info():
    info = web.info()
    assert info["version"] == __version__
    assert info["in_browser"] is (sys.platform == "emscripten")
    assert info["targets"] == ["linux", "windows", "macos"]
    assert info["max_depth"] == web.MAX_DEPTH
    json.dumps(info)


def test_language():
    lang = web.language()
    assert "print" in lang["builtins"] and "plot" in lang["builtins"]
    assert "pi" not in lang["builtins"]
    assert "fn" in lang["keywords"] and "pi" in lang["constants"]


# JSON dispatch

def test_dispatch_round_trip():
    reply = json.loads(web.dispatch(json.dumps({"fn": "run", "args": {"code": "print(1)\n2", "seed": 1}})))
    assert reply["ok"] and reply["stdout"] == "1\n" and reply["value"] == "2"


@pytest.mark.parametrize("fn", sorted(web.COMMANDS))
def test_dispatch_every_command(fn):
    args = {} if fn == "info" else {"code": "x = 1\nprint(x)"}
    reply = json.loads(web.dispatch(json.dumps({"fn": fn, "args": args})))
    assert reply is not None


def test_dispatch_errors():
    for request in ["not json", "[1, 2]", json.dumps({"fn": "nope"}), json.dumps({"fn": "run", "args": [1]}),
                    json.dumps({"fn": "run", "args": {"code": 5}}), json.dumps({"fn": "run", "args": {"bad": 1}})]:
        reply = json.loads(web.dispatch(request))
        assert reply["ok"] is False
        assert reply["error_kind"] == "internal"
        assert reply["stderr"].startswith("internal error:")


def test_dispatch_output_is_strict_json():
    reply = web.dispatch(json.dumps({"fn": "ast_json", "args": {"code": "x = 1e999\ny = 2^80"}}))
    json.loads(reply, parse_constant=lambda c: pytest.fail(f"non-standard JSON constant {c}"))


def test_jsonable():
    assert web._jsonable({"a": float("nan"), "b": float("-inf"), "c": 2**60, "d": (1, 2), 3: {1}}) == {
        "a": "nan", "b": "-inf", "c": str(2**60), "d": [1, 2], "3": "{1}"}
