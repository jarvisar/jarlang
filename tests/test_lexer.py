import pytest

from jarlang.errors import LexError
from jarlang.lexer import Interp, Lexer, tokenize
from jarlang.source import Source
from jarlang.tokens import T


def kinds(text: str) -> list[T]:
    return [t.kind for t in tokenize(text) if t.kind not in (T.NEWLINE, T.EOF)]


def values(text: str) -> list:
    return [t.value for t in tokenize(text) if t.kind not in (T.NEWLINE, T.EOF)]


@pytest.mark.parametrize("text, expected", [
    ("42", 42),
    ("1_000_000", 1_000_000),
    ("0xFF", 255),
    ("0b1010", 10),
    ("0o17", 15),
    ("3.25", 3.25),
    ("6.02e23", 6.02e23),
    ("2.5e-3", 0.0025),
    ("1e3", 1000.0),
])
def test_numbers(text, expected):
    (tok,) = [t for t in tokenize(text) if t.kind in (T.INT, T.FLOAT)]
    assert tok.value == expected
    assert type(tok.value) is type(expected)


def test_range_is_not_a_float():
    assert kinds("1..5") == [T.INT, T.DOTDOT, T.INT]
    assert kinds("0..<n") == [T.INT, T.DOTDOTLT, T.IDENT]


def test_operators_longest_match():
    assert kinds("a // b ** c != d <= e |> f => g -> h") == [
        T.IDENT, T.DSLASH, T.IDENT, T.CARET, T.IDENT, T.NE, T.IDENT, T.LE, T.IDENT,
        T.PIPE_GT, T.IDENT, T.FAT_ARROW, T.IDENT, T.ARROW, T.IDENT]
    assert kinds("x += 1; y //= 2; z ^= 3") == [
        T.IDENT, T.PLUS_ASSIGN, T.INT, T.SEMI, T.IDENT, T.DSLASH_ASSIGN, T.INT, T.SEMI,
        T.IDENT, T.CARET_ASSIGN, T.INT]


def test_unicode_operators():
    assert kinds("a × b ÷ c ≤ d ≥ e ≠ f") == [
        T.IDENT, T.STAR, T.IDENT, T.SLASH, T.IDENT, T.LE, T.IDENT, T.GE, T.IDENT, T.NE, T.IDENT]
    assert kinds("√x") == [T.SQRT, T.IDENT]
    assert values("π ∞") == ["π", "inf"]


def test_keywords_vs_identifiers():
    assert kinds("fn let letter iffy if") == [T.FN, T.LET, T.IDENT, T.IDENT, T.IF]


def test_spacing_flag_for_implicit_multiplication():
    toks = tokenize("2x 2 x")
    assert toks[1].kind is T.IDENT and toks[1].spaced is False
    assert toks[3].kind is T.IDENT and toks[3].spaced is True


def test_newlines_are_significant_but_collapse():
    toks = tokenize("a\n\n\nb")
    assert [t.kind for t in toks] == [T.IDENT, T.NEWLINE, T.IDENT, T.NEWLINE, T.EOF]


def test_newlines_ignored_inside_parens_and_brackets():
    assert T.NEWLINE not in kinds("f(1,\n 2)\n")
    assert T.NEWLINE not in kinds("[1,\n2,\n3]")


def test_newline_kept_inside_braces():
    assert kinds("{\na\nb\n}").count(T.NEWLINE) == 0  # kinds() strips them, so check the raw tokens
    raw = [t.kind for t in tokenize("{\na\nb\n}")]
    assert raw.count(T.NEWLINE) >= 2


def test_line_continuation_after_operator_and_before_pipe():
    assert [t.kind for t in tokenize("x = 1 +\n 2")].count(T.NEWLINE) == 1
    assert [t.kind for t in tokenize("xs\n  |> f\n  |> g")].count(T.NEWLINE) == 1
    assert [t.kind for t in tokenize("xs\n  .map(f)\n  .sum()")].count(T.NEWLINE) == 1


def test_comments_are_collected():
    lexer = Lexer(Source("x = 1  # the answer\n# alone\n"))
    lexer.tokenize()
    assert [c.text for c in lexer.comments] == [" the answer", " alone"]


def test_string_interpolation_parts():
    (tok,) = [t for t in tokenize('"a {x + 1} b {y:.2f}"') if t.kind is T.STRING]
    parts = tok.value
    assert parts[0] == "a "
    assert isinstance(parts[1], Interp) and parts[1].spec is None
    assert parts[2] == " b "
    assert isinstance(parts[3], Interp) and parts[3].spec == ".2f"


def test_single_quoted_strings_do_not_interpolate():
    (tok,) = [t for t in tokenize("'a {x}'") if t.kind is T.STRING]
    assert tok.value == ["a {x}"]


def test_escapes():
    (tok,) = [t for t in tokenize(r'"tab\there \{brace\} \u{1F600} é"') if t.kind is T.STRING]
    assert tok.value == ["tab\there {brace} \U0001F600 é"]


def test_nested_string_in_interpolation():
    (tok,) = [t for t in tokenize('"x {f("y")} z"') if t.kind is T.STRING]
    assert len(tok.value) == 3


@pytest.mark.parametrize("text, message", [
    ('"never closed', "unterminated string"),
    ("x = $5", "unexpected character"),
    ('"{}"', "empty interpolation"),
    (r'"\q"', "unknown escape"),
    ("0x", "expected digits"),
])
def test_lex_errors(text, message):
    with pytest.raises(LexError) as info:
        tokenize(text)
    assert message in info.value.message


def test_helpful_hint_for_dollar():
    with pytest.raises(LexError) as info:
        tokenize('"a" + $x')
    assert any("{expr}" in h for h in info.value.diagnostic.helps)


def test_backslash_crlf_continues_a_string():
    toks = tokenize('x = "a' + chr(92) + '\r\nb"')
    assert toks[2].value == ["ab"]


def test_old_mac_line_endings():
    from support import run_program
    assert run_program("x = 1\ry = 2\rprint(x + y)\r") == "3\n"
    assert run_program("x = 1\r\ny = 2\r\nprint(x + y)\r\n") == "3\n"


@pytest.mark.parametrize("code, message", [
    ('print("{(1}")', "expected `)`"),
    ('print("{[1, 2}")', "expected `]`"),
    ('print("{f(1}")', "expected `)`"),
])
def test_unclosed_bracket_in_interpolation(code, message):
    from support import run_error
    assert message in run_error(code).message


def test_brackets_in_interpolation():
    from support import run_program
    assert run_program('print("{[1, (2)][0]} {({a: 1}).a} {"x":>3}")') == "1 1   x\n"
