"""Builds the playground and serves it on localhost (used by `jarlang playground`)."""

from __future__ import annotations

import functools
import importlib.util
import os
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAYGROUND = ROOT / "playground"
BUILD_SCRIPT = ROOT / "tools" / "build_playground.py"

# Set the types directly, since Windows can map .js to text/plain
MIME_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, **MIME_TYPES}

    def end_headers(self) -> None:
        # Always load the latest files after a rebuild
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        pass


class Server(ThreadingHTTPServer):
    # On Windows, reusing the address lets two servers share a port without an error
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def available() -> bool:
    return (PLAYGROUND / "index.html").exists() and BUILD_SCRIPT.exists()


def build() -> int:
    spec = importlib.util.spec_from_file_location("build_playground", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(["--quiet"])


def make_server(port: int = 8000, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, str]:
    handler = functools.partial(Handler, directory=str(PLAYGROUND))
    # Try the next ports if this one is taken
    for p in range(port, port + 20):
        try:
            server = Server((host, p), handler)
        except OSError:
            continue
        name = "localhost" if host in ("127.0.0.1", "localhost") else host
        return server, f"http://{name}:{server.server_address[1]}/"
    raise OSError(f"ports {port} to {port + 19} are all in use")


def serve(port: int = 8000, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    if not available():
        print("error: the playground files were not found. Run this from a copy of the JarLang repository.")
        return 1
    if build() != 0:
        return 1
    try:
        server, url = make_server(port, host)
    except OSError as exc:
        print(f"error: {exc}")
        return 1
    print(f"Playground running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0
