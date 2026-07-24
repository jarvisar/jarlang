"""Tests for the language server (jarlang/lsp.py)."""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jarlang.lsp import (
    Analysis,
    LanguageServer,
    LineIndex,
    is_identifier,
    path_to_uri,
    read_message,
    serve,
    uri_to_path,
    write_message,
)

ROOT = Path(__file__).resolve().parent.parent
URI = "file:///tmp/demo.jlang"

DEMO = """\
# Squares plus one.
f(x) = x^2 + 1

# Fibonacci numbers.
fn fib(n: int) -> int {
    if n < 2 { return n }
    fib(n - 1) + fib(n - 2)
}

fn greet(name, greeting = "Hello") {
    unused = 3
    return "{greeting}, {name}!"
    print("never")
}

const LIMIT = 0xFF
xs = [1, 2, 3]
print(lenght(xs), f(3), "fib: {fib(10):>5}")
squares = xs.map(fn(v) => v^2) |> filter(fn(v) => v > 1)
"""


# Helpers

def pos(line: int, character: int) -> dict:
    return {"line": line, "character": character}


# Position (UTF-16) of the nth match of needle, plus delta characters
def find(text: str, needle: str, nth: int = 0, delta: int = 0) -> dict:
    index = -1
    for _ in range(nth + 1):
        index = text.index(needle, index + 1)
    return LineIndex(text).position(index + delta)


# Runs a LanguageServer in the same process
class Client:
    def __init__(self, init_params: dict | None = None) -> None:
        self.server = LanguageServer()
        self.next_id = 0
        params = {"processId": None, "rootUri": None,
                  "capabilities": {"textDocument": {"rename": {"prepareSupport": True}}}}
        params.update(init_params or {})
        self.init_result = self.request("initialize", params)
        self.notify("initialized", {})

    @property
    def outbox(self) -> list[dict]:
        return self.server.outbox

    def request_raw(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        response = self.server.handle_message({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                               "params": params or {}})
        assert response is not None and response["id"] == self.next_id
        return response

    def request(self, method: str, params: dict | None = None):
        response = self.request_raw(method, params)
        assert "error" not in response, response
        return response["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.server.handle_message({"jsonrpc": "2.0", "method": method, "params": params or {}}) is None

    def open(self, text: str, uri: str = URI, version: int = 1) -> list[dict]:
        self.outbox.clear()
        self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "jarlang",
                                                              "version": version, "text": text}})
        return self.diagnostics(uri)

    def change(self, text: str, uri: str = URI, version: int = 2) -> list[dict]:
        self.outbox.clear()
        self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                               "contentChanges": [{"text": text}]})
        return self.diagnostics(uri)

    def diagnostics(self, uri: str = URI) -> list[dict]:
        published = [m for m in self.outbox if m.get("method") == "textDocument/publishDiagnostics"
                     and m["params"]["uri"] == uri]
        assert published, self.outbox
        return published[-1]["params"]["diagnostics"]

    def at(self, method: str, position: dict, uri: str = URI, **extra):
        return self.request(method, {"textDocument": {"uri": uri}, "position": position, **extra})


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def demo(client: Client) -> Client:
    client.open(DEMO)
    return client


# Positions

class TestLineIndex:
    def test_ascii(self):
        index = LineIndex("ab\ncd")
        assert index.position(0) == pos(0, 0)
        assert index.position(4) == pos(1, 1)
        assert index.offset(pos(1, 1)) == 4
        assert index.position(5) == pos(1, 2)

    def test_bmp_characters_count_once(self):
        text = "π = 3\nτ ≤ 7"
        index = LineIndex(text)
        assert index.position(text.index("=")) == pos(0, 2)
        assert index.position(text.index("≤")) == pos(1, 2)
        assert index.offset(pos(1, 2)) == text.index("≤")

    def test_astral_characters_count_twice_in_utf16(self):
        text = 'x = "😀😀" + y\nz'
        index = LineIndex(text)
        plus = text.index("+")
        assert plus == 9  # code points
        assert index.position(plus) == pos(0, 11)  # UTF-16 units
        assert index.offset(pos(0, 11)) == plus
        assert index.position(text.index("y")) == pos(0, 13)
        # Round trip every offset
        for offset in range(len(text) + 1):
            assert index.offset(index.position(offset)) == offset

    def test_position_inside_surrogate_pair_snaps_to_character_start(self):
        text = "a😀b"
        index = LineIndex(text)
        assert index.offset(pos(0, 1)) == 1
        assert index.offset(pos(0, 2)) == 1  # between the two halves of 😀
        assert index.offset(pos(0, 3)) == 2
        assert index.offset(pos(0, 4)) == 3

    def test_utf8_and_utf32_encodings(self):
        text = "é😀x"
        utf8 = LineIndex(text, "utf-8")
        assert utf8.position(1) == pos(0, 2)
        assert utf8.position(2) == pos(0, 6)
        assert utf8.offset(pos(0, 6)) == 2
        utf32 = LineIndex(text, "utf-32")
        assert utf32.position(2) == pos(0, 2)
        assert utf32.offset(pos(0, 2)) == 2
        with pytest.raises(ValueError):
            LineIndex(text, "latin-1")

    def test_line_endings(self):
        text = "a\r\nbb\rccc\nd"
        index = LineIndex(text)
        assert index.line_count == 4
        assert index.position(text.index("bb")) == pos(1, 0)
        assert index.position(text.index("ccc")) == pos(2, 0)
        assert index.position(text.index("d")) == pos(3, 0)
        assert index.offset(pos(0, 99)) == 1  # clamped to the end of line 0 (before \r\n)
        assert index.offset(pos(1, 99)) == text.index("\r", 3)

    def test_clamping(self):
        index = LineIndex("abc\ndef")
        assert index.offset(pos(-1, 0)) == 0
        assert index.offset(pos(9, 0)) == 7
        assert index.position(-5) == pos(0, 0)
        assert index.position(100) == pos(1, 3)


def test_uri_path_round_trip(tmp_path: Path):
    target = tmp_path / "some dir" / "file π.jlang"
    uri = path_to_uri(str(target))
    assert uri.startswith("file://")
    assert os.path.normcase(uri_to_path(uri)) == os.path.normcase(str(target))
    assert uri_to_path("untitled:Untitled-1") is None
    if os.name == "nt":
        assert uri_to_path("file:///c%3A/Users/me/x.jlang").lower() == r"c:\users\me\x.jlang"


def test_is_identifier():
    assert is_identifier("x")
    assert is_identifier("_tmp2")
    assert is_identifier("π")
    assert not is_identifier("2x")
    assert not is_identifier("for")
    assert not is_identifier("a-b")
    assert not is_identifier("")


# Lifecycle

def test_initialize_advertises_capabilities(client: Client):
    caps = client.init_result["capabilities"]
    assert caps["textDocumentSync"]["openClose"] is True
    assert caps["textDocumentSync"]["change"] == 2  # incremental
    assert "save" in caps["textDocumentSync"]
    for name in ("hoverProvider", "definitionProvider", "referencesProvider", "documentSymbolProvider",
                 "documentFormattingProvider", "foldingRangeProvider"):
        assert caps[name]
    assert caps["renameProvider"] == {"prepareProvider": True}
    assert set(caps["signatureHelpProvider"]["triggerCharacters"]) == {"(", ","}
    assert "." in caps["completionProvider"]["triggerCharacters"]
    assert caps["positionEncoding"] == "utf-16"
    assert client.init_result["serverInfo"]["name"] == "jarlang-lsp"


def test_position_encoding_negotiation():
    c = Client({"capabilities": {"general": {"positionEncodings": ["utf-32", "utf-16"]}}})
    assert c.init_result["capabilities"]["positionEncoding"] == "utf-32"
    diags = c.open('s = "😀"; print(lenght(s))')
    assert diags[0]["range"]["start"] == pos(0, 15)  # code points (UTF-16 would say 16)


def test_request_before_initialize_is_rejected():
    server = LanguageServer()
    response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover", "params": {}})
    assert response["error"]["code"] == -32002


def test_unknown_request_and_notification(client: Client):
    response = client.request_raw("jarlang/doesNotExist")
    assert response["error"]["code"] == -32601
    client.outbox.clear()
    client.notify("$/unknownNotification", {"x": 1})
    client.notify("workspace/didChangeConfiguration", {"settings": {}})
    assert client.outbox == []


def test_handler_exception_is_logged_and_answered(client: Client):
    def boom(params):
        raise RuntimeError("kaboom")

    client.server.requests["textDocument/hover"] = boom
    client.outbox.clear()
    response = client.request_raw("textDocument/hover", {})
    assert response["error"]["code"] == -32603
    logs = [m for m in client.outbox if m.get("method") == "window/logMessage"]
    assert logs and "kaboom" in logs[0]["params"]["message"] and logs[0]["params"]["type"] == 1


def test_shutdown_then_exit(client: Client):
    assert client.request("shutdown") is None
    assert client.request_raw("textDocument/hover", {})["error"]["code"] == -32600
    client.notify("exit")
    assert client.server.exited and client.server.exit_code == 0


def test_exit_without_shutdown(client: Client):
    client.notify("exit")
    assert client.server.exited and client.server.exit_code == 1


def test_unknown_document_is_an_error(client: Client):
    response = client.request_raw("textDocument/hover", {"textDocument": {"uri": "file:///nope.jlang"},
                                                         "position": pos(0, 0)})
    assert response["error"]["code"] == -32602


# Diagnostics

def test_diagnostics_errors_and_warnings(client: Client):
    diags = client.open(DEMO)
    by_message = {d["message"].split("\n")[0]: d for d in diags}
    undefined = by_message["undefined variable `lenght`"]
    assert undefined["severity"] == 1
    assert undefined["source"] == "jarlang"
    assert "\nhelp: did you mean `len`?" in undefined["message"]
    assert undefined["range"] == {"start": find(DEMO, "lenght"), "end": find(DEMO, "lenght", delta=6)}
    assert undefined["data"] == {"suggestion": "len"}

    unused = by_message["unused variable `unused`"]
    assert unused["severity"] == 2 and unused["tags"] == [1]
    unreachable = by_message["unreachable code"]
    assert unreachable["severity"] == 2 and unreachable["tags"] == [1]
    assert "tags" not in undefined
    published = client.outbox[-1]["params"]
    assert published["uri"] == URI and published["version"] == 1


def test_diagnostics_are_republished_on_change_and_cleared_on_close(client: Client):
    assert client.open("x = 1\nprint(x)") == []
    diags = client.change("x = 1\nprint(y)")
    assert [d["message"].split("\n")[0] for d in diags] == ["undefined variable `y`"]
    assert client.outbox[-1]["params"]["version"] == 2
    client.outbox.clear()
    client.notify("textDocument/didClose", {"textDocument": {"uri": URI}})
    assert client.diagnostics() == []


def test_incremental_changes_are_applied(client: Client):
    client.open("x = 1\nprint(x)")
    client.outbox.clear()
    client.notify("textDocument/didChange", {
        "textDocument": {"uri": URI, "version": 2},
        "contentChanges": [{"range": {"start": pos(1, 6), "end": pos(1, 7)}, "text": "zz"}],
    })
    assert client.server.documents[URI].text == "x = 1\nprint(zz)"
    assert "`zz`" in client.diagnostics()[0]["message"]


def test_incremental_changes_count_utf16_units_and_crlf(client: Client):
    client.open('s = "😀 a"\r\nprint(s)\r\n')
    client.notify("textDocument/didChange", {
        "textDocument": {"uri": URI, "version": 2},
        "contentChanges": [
            {"range": {"start": pos(0, 8), "end": pos(0, 9)}, "text": "b"},  # the emoji is 2 units
            {"range": {"start": pos(1, 0), "end": pos(1, 0)}, "text": "t = 1\r\n"},
        ],
    })
    assert client.server.documents[URI].text == 's = "😀 b"\r\nt = 1\r\nprint(s)\r\n'


def test_syntax_error_diagnostics_have_related_information(client: Client):
    text = "total = (1 +\n  2\nprint(total)\n}"
    diags = client.open(text)
    assert diags and all(d["severity"] == 1 for d in diags)
    related = [d for d in diags if d.get("relatedInformation")]
    assert related
    info = related[0]["relatedInformation"][0]
    assert info["location"]["uri"] == URI
    assert info["location"]["range"]["start"] == find(text, "(")
    assert "unclosed" in info["message"]


def test_zero_width_and_end_of_input_spans_get_one_character(client: Client):
    diags = client.open("fn f(x) {\n  x + 1\n")
    assert diags
    rng = diags[0]["range"]
    assert rng["start"] != rng["end"]
    for d in client.open("print(1 +\n"):
        assert d["range"]["start"] != d["range"]["end"]


def test_diagnostic_ranges_use_utf16(client: Client):
    text = 's = "😀😀"; print(lenght(s))'
    diags = client.open(text)
    assert diags[0]["range"]["start"] == pos(0, text.index("lenght") + 2)


def test_save_rechecks(client: Client):
    client.open("print(1)")
    client.outbox.clear()
    client.notify("textDocument/didSave", {"textDocument": {"uri": URI}})
    assert client.diagnostics() == []


# Hover

def hover_text(client: Client, position: dict) -> str | None:
    result = client.at("textDocument/hover", position)
    if result is None:
        return None
    assert result["contents"]["kind"] == "markdown"
    return result["contents"]["value"]


def test_hover_builtin(demo: Client):
    value = hover_text(demo, find(DEMO, "print(lenght"))
    assert "```jarlang\nprint(...values)\n```" in value
    assert "Print values" in value
    value = hover_text(demo, find(DEMO, "map(", delta=1))
    assert "map(xs, f)" in value


def test_hover_user_function_shows_signature_and_doc_comment(demo: Client):
    value = hover_text(demo, find(DEMO, "fib(n - 1)"))
    assert "fn fib(n: int) -> int" in value
    assert "Fibonacci numbers." in value
    assert "line 5" in value
    value = hover_text(demo, find(DEMO, "f(3)"))
    assert "fn f(x)" in value and "Squares plus one." in value
    value = hover_text(demo, find(DEMO, "greet"))
    assert 'fn greet(name, greeting = "Hello")' in value


def test_hover_variables_and_parameters(demo: Client):
    value = hover_text(demo, find(DEMO, "xs.map"))
    assert "(global variable) xs = [1, 2, 3]" in value
    value = hover_text(demo, find(DEMO, "n < 2"))
    assert "(parameter) n: int" in value and "`fib`" in value
    value = hover_text(demo, find(DEMO, "LIMIT"))
    assert "const LIMIT = 0xFF" in value
    value = hover_text(demo, find(DEMO, "unused"))
    assert "(local variable) unused = 3" in value


def test_hover_inside_string_interpolation(demo: Client):
    value = hover_text(demo, find(DEMO, "{fib(10)", delta=2))
    assert "fn fib(n: int) -> int" in value
    value = hover_text(demo, find(DEMO, ":>5", delta=2))
    assert "Format spec `>5`" in value and "right-aligned" in value
    value = hover_text(demo, find(DEMO, "{greeting}", delta=3))
    assert "(parameter) greeting" in value


def test_hover_keywords_numbers_constants(demo: Client):
    assert "Define a function" in hover_text(demo, find(DEMO, "fn fib"))
    value = hover_text(demo, find(DEMO, "0xFF", delta=1))
    assert "**255**" in value
    client = Client()
    client.open("area = π * 2^8")
    value = hover_text(client, find("area = π * 2^8", "π"))
    assert "const π = 3.14159" in value and "`pi`" in value
    assert "Power" in hover_text(client, find("area = π * 2^8", "^"))


def test_hover_on_nothing(demo: Client):
    assert hover_text(demo, pos(0, 3)) is None  # inside a comment
    assert hover_text(demo, pos(2, 0)) is None  # blank line


def test_hover_with_emoji_before_the_name(client: Client):
    text = 'label = "😀"; print(label)'
    client.open(text)
    value = hover_text(client, pos(0, text.index("label)") + 1 + 1))  # +1 for the emoji's 2nd unit
    assert value is not None and "(global variable) label" in value


# Definition, references and rename

def test_goto_definition(demo: Client):
    result = demo.at("textDocument/definition", find(DEMO, "fib(n - 1)"))
    assert result == {"uri": URI, "range": {"start": find(DEMO, "fib("), "end": find(DEMO, "fib(", delta=3)}}
    result = demo.at("textDocument/definition", find(DEMO, "n < 2"))
    assert result["range"]["start"] == find(DEMO, "n: int")
    assert demo.at("textDocument/definition", find(DEMO, "print(lenght")) is None


def test_goto_definition_of_import(tmp_path: Path):
    lib = tmp_path / "lib.jlang"
    lib.write_text("# library\nfn helper(x) => x + 1\n", encoding="utf-8")
    main = tmp_path / "main.jlang"
    text = 'import "lib.jlang"\nimport "lib.jlang" as lib\nprint(helper(1), lib)\n'
    main.write_text(text, encoding="utf-8")
    client = Client()
    uri = path_to_uri(str(main))
    assert client.open(text, uri=uri) == []
    result = client.at("textDocument/definition", find(text, "helper"), uri=uri)
    assert os.path.normcase(uri_to_path(result["uri"])) == os.path.normcase(str(lib))
    assert result["range"]["start"] == pos(1, 3)
    result = client.at("textDocument/definition", find(text, '"lib.jlang"', delta=2), uri=uri)
    assert os.path.normcase(uri_to_path(result["uri"])) == os.path.normcase(str(lib))
    result = client.at("textDocument/definition", find(text, "lib)"), uri=uri)
    assert result["uri"] == uri and result["range"]["start"] == find(text, "lib\n")


def test_references(demo: Client):
    refs = demo.at("textDocument/references", find(DEMO, "fib(10)"), context={"includeDeclaration": True})
    starts = [r["range"]["start"] for r in refs]
    assert starts == [find(DEMO, "fib("), find(DEMO, "fib(n - 1)"), find(DEMO, "fib(n - 2)"), find(DEMO, "fib(10)")]
    assert all(r["uri"] == URI for r in refs)
    refs = demo.at("textDocument/references", find(DEMO, "fib(10)"), context={"includeDeclaration": False})
    assert len(refs) == 3
    assert demo.at("textDocument/references", find(DEMO, "print(lenght"), context={}) == []


def test_document_highlight(demo: Client):
    highlights = demo.at("textDocument/documentHighlight", find(DEMO, "xs.map"))
    assert len(highlights) == 3  # xs = ..., lenght(xs), xs.map


def test_prepare_rename(demo: Client):
    result = demo.at("textDocument/prepareRename", find(DEMO, "fib(10)", delta=1))
    assert result == {"range": {"start": find(DEMO, "fib(10)"), "end": find(DEMO, "fib(10)", delta=3)},
                      "placeholder": "fib"}
    for needle in ("print(", "if n"):
        response = demo.request_raw("textDocument/prepareRename",
                                    {"textDocument": {"uri": URI}, "position": find(DEMO, needle)})
        assert "error" in response


def test_rename(demo: Client):
    edit = demo.at("textDocument/rename", find(DEMO, "fib(n - 2)"), newName="fibonacci")
    edits = edit["changes"][URI]
    assert len(edits) == 4 and all(e["newText"] == "fibonacci" for e in edits)
    assert {"start": find(DEMO, "fib(10)"), "end": find(DEMO, "fib(10)", delta=3)} in [e["range"] for e in edits]
    # Parameters, including uses inside string interpolation
    edit = demo.at("textDocument/rename", find(DEMO, "name,"), newName="who")
    ranges = [e["range"]["start"] for e in edit["changes"][URI]]
    assert ranges == [find(DEMO, "name,"), find(DEMO, "{name}", delta=1)]


def test_rename_rejects_bad_names(demo: Client):
    params = {"textDocument": {"uri": URI}, "position": find(DEMO, "fib(10)")}
    for bad in ("for", "2fast", "a-b", ""):
        response = demo.request_raw("textDocument/rename", {**params, "newName": bad})
        assert "error" in response, bad
    response = demo.request_raw("textDocument/rename", {"textDocument": {"uri": URI},
                                                        "position": find(DEMO, "print("), "newName": "show"})
    assert "built-in" in response["error"]["message"]
    response = demo.request_raw("textDocument/rename", {**params, "newName": "greet"})
    assert "already defined" in response["error"]["message"]


# Completion

def labels(result) -> dict[str, dict]:
    items = result["items"] if isinstance(result, dict) else result
    out: dict[str, dict] = {}
    for item in items:
        out.setdefault(item["label"], item)  # keywords come before same-named snippets
    return out


def test_completion_general(demo: Client):
    inside_greet = find(DEMO, "unused = 3")
    items = labels(demo.at("textDocument/completion", inside_greet))
    assert items["while"]["kind"] == 14
    assert items["map"]["kind"] == 3 and items["map"]["detail"] == "map(xs, f)"
    assert items["map"]["documentation"]["kind"] == "markdown"
    assert items["pi"]["kind"] == 21 and items["π"]["kind"] == 21
    assert items["fib"]["kind"] == 3 and items["fib"]["detail"] == "fn fib(n: int) -> int"
    assert items["LIMIT"]["kind"] == 21
    assert items["xs"]["kind"] == 6
    assert "name" in items and "greeting" in items  # parameters of the enclosing function
    assert "n" not in items  # fib's parameter is not visible here
    snippets = [i for i in demo.at("textDocument/completion", inside_greet)["items"] if i["kind"] == 15]
    assert {s["label"].split()[0] for s in snippets} >= {"fn", "if", "for", "while"}
    assert all(s["insertTextFormat"] == 2 for s in snippets)


def test_completion_snippets_can_be_disabled():
    client = Client({"initializationOptions": {"snippets": False}})
    client.open("x = 1\n")
    items = client.at("textDocument/completion", pos(1, 0))["items"]
    assert not [i for i in items if i["kind"] == 15]


def test_completion_after_dot_offers_methods(demo: Client):
    text = DEMO + "xs.\n"
    demo.change(text)
    items = labels(demo.at("textDocument/completion", find(text, "xs.\n", delta=3)))
    assert items["map"]["kind"] == 2 and items["filter"]["kind"] == 2 and items["len"]["kind"] == 2
    assert "while" not in items and "random" not in items  # keywords and builtins with no arguments
    assert "fib" in items  # user functions work as methods too


def test_completion_after_syntax_error(client: Client):
    good = "fn area(radius) {\n    π * radius^2\n}\ntotal = 0\n"
    client.open(good)
    broken = good + "fn helper(alpha, beta) {\n    y = (alpha +\n    al\n}\n"
    diags = client.change(broken)
    assert any(d["severity"] == 1 for d in diags)
    items = labels(client.at("textDocument/completion", find(broken, "    al\n", delta=6)))
    for name in ("area", "total", "alpha", "beta", "len", "while"):
        assert name in items, name
    assert items["area"]["kind"] == 3


def test_completion_is_empty_in_strings_and_comments(client: Client):
    text = 'x = "hello wor"  # a comment\ny = "{x.'
    client.open(text)
    assert client.at("textDocument/completion", find(text, "wor", delta=2))["items"] == []
    assert client.at("textDocument/completion", find(text, "comment", delta=3))["items"] == []
    # Code inside an interpolation still completes
    items = labels(client.at("textDocument/completion", pos(1, len('y = "{x.'))))
    assert "map" in items


def test_completion_of_type_names(client: Client):
    text = "fn f(x: in) -> \n"
    client.open(text)
    items = labels(client.at("textDocument/completion", find(text, "in)", delta=2)))
    assert set(items) >= {"int", "float", "str", "list"}
    items = labels(client.at("textDocument/completion", find(text, "-> ", delta=3)))
    assert "bool" in items


# Signature help

def test_signature_help_builtin(client: Client):
    text = "print(clamp(5, 1, "
    client.open(text)
    result = client.at("textDocument/signatureHelp", pos(0, len(text)))
    sig = result["signatures"][0]
    assert sig["label"] == "clamp(x, lo, hi)"
    assert result["activeParameter"] == 2
    start, end = sig["parameters"][2]["label"]
    assert sig["label"][start:end] == "hi"
    assert "documentation" in sig


def test_signature_help_user_function_and_nesting(demo: Client):
    text = DEMO + 'greet("Ada", fib(3), '
    demo.change(text)
    result = demo.at("textDocument/signatureHelp", pos(len(text.splitlines()) - 1, len('greet("Ada", fib(3), ')))
    sig = result["signatures"][0]
    assert sig["label"] == 'greet(name, greeting = "Hello")'
    assert result["activeParameter"] == 2  # past the last parameter
    result = demo.at("textDocument/signatureHelp", pos(len(text.splitlines()) - 1, len('greet("Ada", fib(')))
    assert result["signatures"][0]["label"] == "fib(n: int) -> int"
    assert result["activeParameter"] == 0


def test_signature_help_method_and_pipe_forms(client: Client):
    text = "xs = [3, 1]\nys = xs.map(\nzs = xs |> reduce(fn(a, b) => a + b, "
    client.open(text)
    result = client.at("textDocument/signatureHelp", pos(1, len("ys = xs.map(")))
    assert result["signatures"][0]["label"] == "map(xs, f)" and result["activeParameter"] == 1
    result = client.at("textDocument/signatureHelp", pos(2, len("zs = xs |> reduce(fn(a, b) => a + b, ")))
    assert result["signatures"][0]["label"].startswith("reduce(") and result["activeParameter"] == 2


def test_signature_help_variadic_and_outside_calls(client: Client):
    text = 'print(1, 2, 3, "x, y", 4)\nx = 1'
    client.open(text)
    result = client.at("textDocument/signatureHelp", pos(0, len('print(1, 2, 3, "x, y", 4')))
    assert result["activeParameter"] == 0  # clamped to the variadic parameter
    assert client.at("textDocument/signatureHelp", pos(1, 5)) is None
    assert client.at("textDocument/signatureHelp", pos(0, 2)) is None


# Document symbols, folding and formatting

def test_document_symbols(client: Client):
    text = ("const G = 9.81\nxs = [1, 2]\nxs = [3]\nf(x) = x + 1\n"
            "fn outer(a) {\n  fn inner(b) => b * 2\n  helper = fn(c) => c\n  inner(a)\n}\n"
            "sq = fn(v) => v * v\n")
    client.open(text)
    symbols = {s["name"]: s for s in client.request("textDocument/documentSymbol",
                                                    {"textDocument": {"uri": URI}})}
    assert list(symbols) == ["G", "xs", "f", "outer", "sq"]
    assert symbols["G"]["kind"] == 14
    assert symbols["xs"]["kind"] == 13
    assert symbols["f"]["kind"] == 12 and symbols["f"]["detail"] == "fn f(x)"
    outer = symbols["outer"]
    assert outer["kind"] == 12 and outer["range"]["start"] == pos(4, 0) and outer["range"]["end"] == pos(8, 1)
    assert outer["selectionRange"] == {"start": pos(4, 3), "end": pos(4, 8)}
    assert [c["name"] for c in outer["children"]] == ["inner", "helper"]
    assert symbols["sq"]["kind"] == 12


def test_folding_ranges(client: Client):
    text = "# one\n# two\n# three\nfn f(x) {\n  y = x\n  y\n}\nxs = [\n  1,\n  2,\n]\n"
    client.open(text)
    ranges = client.request("textDocument/foldingRange", {"textDocument": {"uri": URI}})
    assert {"startLine": 0, "endLine": 2, "kind": "comment"} in ranges
    assert {"startLine": 3, "endLine": 5} in ranges
    assert {"startLine": 7, "endLine": 9} in ranges


def test_formatting(client: Client):
    from jarlang.formatter import format_code

    def format_request():
        return client.request("textDocument/formatting", {"textDocument": {"uri": URI},
                                                          "options": {"tabSize": 4, "insertSpaces": True}})

    text = "fn   f( x ){\nx+1\n}\nprint( f(2) )\n"
    client.open(text)
    expected = format_code(text)
    assert format_request() == [{"range": {"start": pos(0, 0), "end": LineIndex(text).position(len(text))},
                                 "newText": expected}]
    client.change(expected)
    assert format_request() == []
    client.change("fn f( {\n")
    assert format_request() is None  # syntax errors are shown as diagnostics instead


# Code actions

def test_code_action_did_you_mean(demo: Client):
    diags = demo.diagnostics() if demo.outbox else demo.open(DEMO)
    target = next(d for d in diags if "lenght" in d["message"])
    actions = demo.request("textDocument/codeAction", {
        "textDocument": {"uri": URI}, "range": target["range"],
        "context": {"diagnostics": [target]},
    })
    fix = actions[0]
    assert fix["kind"] == "quickfix" and fix["isPreferred"] is True
    assert fix["edit"]["changes"][URI] == [{"range": target["range"], "newText": "len"}]


def test_code_action_from_message_only_and_underscore_fix(demo: Client):
    text = "fn f() {\n  temp = 1\n  prnt(2)\n}\n"
    diags = demo.change(text)
    typo = next(d for d in diags if "prnt" in d["message"])
    typo.pop("data", None)  # clients may drop `data`, but the help text is enough
    actions = demo.request("textDocument/codeAction", {"textDocument": {"uri": URI}, "range": typo["range"],
                                                       "context": {"diagnostics": [typo]}})
    assert actions[0]["edit"]["changes"][URI][0]["newText"] == "print"
    unused = next(d for d in diags if "unused" in d["message"])
    actions = demo.request("textDocument/codeAction", {"textDocument": {"uri": URI}, "range": unused["range"],
                                                       "context": {"diagnostics": [unused]}})
    assert actions[0]["title"].startswith("Prefix `temp`")
    assert [e["newText"] for e in actions[0]["edit"]["changes"][URI]] == ["_temp"]
    # Filtered out when the client only wants refactorings
    assert demo.request("textDocument/codeAction", {"textDocument": {"uri": URI}, "range": unused["range"],
                                                    "context": {"diagnostics": [unused], "only": ["refactor"]}}) == []


# Semantic tokens

def decode_semantic_tokens(text: str, data: list[int], legend: dict) -> list[tuple[str, str, list[str]]]:
    lines = text.split("\n")
    out = []
    line = char = 0
    for i in range(0, len(data), 5):
        d_line, d_char, length, kind, modifiers = data[i:i + 5]
        line += d_line
        char = d_char if d_line else char + d_char
        utf16 = lines[line].encode("utf-16-le")
        word = utf16[char * 2:(char + length) * 2].decode("utf-16-le")
        mods = [m for j, m in enumerate(legend["tokenModifiers"]) if modifiers >> j & 1]
        out.append((word, legend["tokenTypes"][kind], mods))
    return out


def test_semantic_tokens(client: Client):
    legend = client.init_result["capabilities"]["semanticTokensProvider"]["legend"]
    text = 'const N = 3\nfn sq(x) => x * x\nprint(xs.map(sq), "😀 {sq(N)} {pi}", p.name)\nxs = [1]\n'
    client.open(text)
    data = client.request("textDocument/semanticTokens/full", {"textDocument": {"uri": URI}})["data"]
    tokens = decode_semantic_tokens(text, data, legend)
    assert ("N", "variable", ["declaration", "readonly"]) in tokens
    assert ("sq", "function", ["declaration"]) in tokens
    assert ("x", "parameter", ["declaration"]) in tokens
    assert ("x", "parameter", []) in tokens
    assert ("print", "function", ["defaultLibrary"]) in tokens
    assert ("map", "function", ["defaultLibrary"]) in tokens
    assert tokens.count(("sq", "function", [])) == 2  # passed as a value, and called inside "{...}"
    assert ("pi", "variable", ["readonly", "defaultLibrary"]) in tokens  # after an emoji: UTF-16 columns
    assert ("name", "property", []) in tokens
    assert ("xs", "variable", ["declaration"]) in tokens


# Broken code and checker errors

def test_analysis_survives_a_crashing_checker(client: Client, monkeypatch):
    import jarlang.lsp as lsp

    def broken_analyze(source, tokens=None):
        raise IndexError("simulated checker bug")

    monkeypatch.setattr(lsp, "analyze", broken_analyze)
    client.outbox.clear()
    diags = client.open("x = 1\nprint(x +)\ny = x\n")
    assert [d["message"].split("\n")[0] for d in diags] == ["expected an expression, found `)`"]
    logs = [m for m in client.outbox if m.get("method") == "window/logMessage"]
    assert logs and "simulated checker bug" in logs[0]["params"]["message"]
    # Features keep working from the recovered analysis
    assert "(global variable) y" in hover_text(client, pos(2, 0))


def test_statements_the_parser_chokes_on_are_skipped():
    # `a, (b` used to crash the parser with an IndexError
    # The server should report an error and check the rest of the file
    analysis = Analysis("x = 1\na, (b\nprint(x)\n", "x.jlang")
    assert any(d.severity == "error" for d in analysis.diagnostics)
    assert analysis.symbol_at(0) is not None


def test_analysis_survives_lexer_errors():
    text = 'fn ok(a) => a\nbad = "unterminated\nlater = ok(1)\n'
    analysis = Analysis(text, "x.jlang")
    assert not analysis.complete
    assert analysis.diagnostics
    names = {t.value for t in analysis.tokens if t.kind.name == "IDENT"}
    assert "later" in names  # lexing resumed on the next line


# Transport

def frame(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def test_message_framing_round_trip():
    stream = io.BytesIO()
    write_message(stream, {"jsonrpc": "2.0", "method": "x", "params": {"text": "π 😀"}})
    stream.seek(0)
    assert json.loads(read_message(stream)) == {"jsonrpc": "2.0", "method": "x", "params": {"text": "π 😀"}}
    assert read_message(stream) is None


def test_serve_over_byte_streams():
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
            "uri": URI, "languageId": "jarlang", "version": 1, "text": "print(lenght([1]))"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
         "params": {"textDocument": {"uri": URI}, "position": pos(0, 1)}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown"},
        {"jsonrpc": "2.0", "method": "exit"},
    ]
    stdin = io.BytesIO(b"".join(frame(m) for m in messages[:3]) + b"Content-Length: 5\r\n\r\n{bad}"
                       + b"".join(frame(m) for m in messages[3:]))
    stdout = io.BytesIO()
    assert serve(stdin, stdout) == 0
    stdout.seek(0)
    out = []
    while (body := read_message(stdout)) is not None:
        out.append(json.loads(body))
    responses = {m["id"]: m for m in out if "id" in m}
    assert "capabilities" in responses[1]["result"]
    assert responses[None]["error"]["code"] == -32700
    assert "print" in responses[2]["result"]["contents"]["value"]
    assert responses[3]["result"] is None
    assert any(m.get("method") == "textDocument/publishDiagnostics" for m in out)


def test_serve_returns_1_without_shutdown():
    stdin = io.BytesIO(frame({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    assert serve(stdin, io.BytesIO()) == 1  # input ended without shutdown/exit


# Start jarlang lsp as a real process and talk to it over pipes
def test_end_to_end_subprocess():
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "jarlang", "lsp"], cwd=str(ROOT), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    inbox: queue.Queue = queue.Queue()

    def reader() -> None:
        while True:
            try:
                body = read_message(proc.stdout)
            except Exception:
                body = None
            if body is None:
                inbox.put(None)
                return
            inbox.put(json.loads(body))

    threading.Thread(target=reader, daemon=True).start()

    def send(message: dict) -> None:
        proc.stdin.write(frame(message))
        proc.stdin.flush()

    def wait_for(predicate, timeout: float = 20.0) -> dict:
        while True:
            message = inbox.get(timeout=timeout)
            assert message is not None, "server closed its output: " + proc.stderr.read().decode(errors="replace")
            if predicate(message):
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"processId": os.getpid(), "rootUri": None, "capabilities": {}}})
        init = wait_for(lambda m: m.get("id") == 1)
        assert init["result"]["capabilities"]["hoverProvider"] is True
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        text = 'label = "π 😀"\nprint(lenght(label))\n'
        send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
            "uri": URI, "languageId": "jarlang", "version": 1, "text": text}}})
        diags = wait_for(lambda m: m.get("method") == "textDocument/publishDiagnostics")
        assert diags["params"]["uri"] == URI
        assert "did you mean `len`" in diags["params"]["diagnostics"][0]["message"]
        send({"jsonrpc": "2.0", "id": 2, "method": "textDocument/hover",
              "params": {"textDocument": {"uri": URI}, "position": pos(1, 1)}})
        hover = wait_for(lambda m: m.get("id") == 2)
        assert "print(...values)" in hover["result"]["contents"]["value"]
        send({"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
        assert wait_for(lambda m: m.get("id") == 3)["result"] is None
        send({"jsonrpc": "2.0", "method": "exit"})
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
