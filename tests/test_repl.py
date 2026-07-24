"""Tests for the REPL that don't need a terminal."""

import pytest

from jarlang.repl import Repl, prompt_toolkit_parts


def test_incomplete_detection():
    assert Repl._incomplete("fn f(x) {")
    assert Repl._incomplete("xs = [1, 2,")
    assert Repl._incomplete("total = 1 +")
    assert not Repl._incomplete("x = 1")
    assert not Repl._incomplete("x = )")  # a real error, not an incomplete line
    # Strings can span lines, with either quote
    assert Repl._incomplete('s = "one')
    assert Repl._incomplete("s = 'one")
    assert Repl._incomplete('s = "say \\"hi\\" and')
    assert not Repl._incomplete('s = "one\ntwo"')


def test_native_source_wraps_final_expression():
    repl = Repl(color=False)
    assert repl._native_source("2 + 3") == "print(2 + 3)"
    assert repl._native_source("x = 2\nx * 3") == "x = 2\nprint(x * 3)"
    assert repl._native_source('print("hi")') == 'print("hi")'


def test_evaluate_sets_ans(capsys):
    repl = Repl(color=False)
    repl.evaluate("6 * 7")
    assert repl.interp.globals["ans"] == 42
    assert capsys.readouterr().out.strip() == "42"
    repl.evaluate("ans + 1")
    assert capsys.readouterr().out.strip() == "43"
    # A nil result, like print's, keeps the last value
    repl.evaluate('print("hi")')
    assert repl.interp.globals["ans"] == 43


def test_commands(capsys):
    repl = Repl(color=False)
    repl.evaluate("x = 5")
    repl.command(":vars")
    repl.command(":type x / 2")
    repl.command(":doc sqrt")
    repl.command(":ast 1 + 2")
    repl.command(":tokens x!")
    repl.command(":bogus")
    out = capsys.readouterr().out
    assert "x" in out and "float" in out and "sqrt(x)" in out and "Binary" in out and "BANG" in out
    assert "unknown command" in out


def test_commands_without_an_argument_show_usage(capsys):
    repl = Repl(color=False)
    for name in ("type", "doc", "load", "asm", "native", "time"):
        repl.command(":" + name)
    out = capsys.readouterr().out.splitlines()
    assert out == ["usage: :type <code>", "usage: :doc <name>", "usage: :load <file>", "usage: :asm <code>",
                   "usage: :native <code>", "usage: :time <code>"]


prompt_toolkit = pytest.importorskip("prompt_toolkit")


def test_live_highlighting_fragments():
    from prompt_toolkit.document import Document
    lexer, _, _ = prompt_toolkit_parts(lambda: [])
    get_line = lexer.lex_document(Document('fn f(x) {\n  print("hi {x}")  # note\n}'))
    first = get_line(0)
    assert first[0] == ("class:keyword", "fn")
    second = "".join(text for _, text in get_line(1))
    assert second == '  print("hi {x}")  # note'
    classes = {cls for cls, _ in get_line(1)}
    assert {"class:builtin", "class:string", "class:comment"} <= classes


def test_completion_includes_builtins_keywords_and_user_names():
    from prompt_toolkit.document import Document
    _, completer, _ = prompt_toolkit_parts(lambda: ["primary_color"])
    words = [c.text for c in completer.get_completions(Document("pri"), None)]
    assert "print" in words and "primes" in words and "primary_color" in words
    assert "while" in [c.text for c in completer.get_completions(Document("whi"), None)]
