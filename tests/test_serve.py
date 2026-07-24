# Tests for jarlang playground (builds the playground and serves it)

import json
import threading
import urllib.request

import pytest

from jarlang import serve


@pytest.fixture
def server():
    assert serve.available()
    assert serve.build() == 0
    srv, url = serve.make_server(port=8950)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield url
    srv.shutdown()
    srv.server_close()


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.headers.get("Content-Type"), r.headers.get("Cache-Control"), r.read()


def test_serves_the_page_bundle_and_icon(server):
    kind, cache, body = fetch(server)
    assert kind.startswith("text/html") and cache == "no-store" and b"JarLang" in body
    kind, _, _ = fetch(server + "app.js")
    assert kind.startswith("text/javascript")
    kind, _, body = fetch(server + "jarlang/bundle.json")
    assert kind.startswith("application/json")
    bundle = json.loads(body)
    assert bundle["examples"][0]["name"] == "hello"
    assert all(f"examples/{e['name']}.jlang" in bundle["files"] for e in bundle["examples"])
    assert "print" in bundle["language"]["builtins"]
    kind, _, _ = fetch(server + "favicon.ico")
    assert kind == "image/x-icon"


def test_uses_the_next_port_when_one_is_taken(server):
    srv, url = serve.make_server(port=int(server.rsplit(":", 1)[1].strip("/")))
    try:
        assert url != server
    finally:
        srv.server_close()
