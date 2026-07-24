#!/usr/bin/env python3
"""Builds the playground bundle (playground/jarlang/bundle.json)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "jarlang"
EXAMPLES = ROOT / "examples"
PLAYGROUND = ROOT / "playground"
BUNDLE_DIR = "jarlang"  # relative to the site root (ignored by git)
BUNDLE_NAME = "bundle.json"

# Static files that make up the site
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico", ".webmanifest", ".txt"}
SKIP_PARTS = {"__pycache__", BUNDLE_DIR, "node_modules"}


def package_files() -> list[Path]:
    """Every file the jarlang package needs to run."""
    files = [p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]
    runtime = PACKAGE / "native" / "runtime.c"
    if runtime.exists():
        files.append(runtime)
    return sorted(files)


def example_files() -> list[Path]:
    """Example programs, including the modules they import from examples/lib."""
    if not EXAMPLES.is_dir():
        return []
    return sorted(p for p in EXAMPLES.rglob("*.jlang") if p.is_file())


def read_text(path: Path) -> str:
    # Use \n newlines so the bundle is the same on every OS
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def page_meta() -> dict:
    """Version, highlighting lists and the examples menu, so the page doesn't have to work them out."""
    sys.path.insert(0, str(ROOT))
    try:
        from jarlang import __version__, web
    finally:
        sys.path.pop(0)
    examples = [{k: e[k] for k in ("name", "title", "description")} for e in web.examples(EXAMPLES)]
    return {"version": __version__, "language": web.language(), "examples": examples}


def build_bundle() -> tuple[dict, dict[str, int]]:
    files: dict[str, str] = {}
    counts = {"package": 0, "examples": 0}
    for path in package_files():
        files[path.relative_to(ROOT).as_posix()] = read_text(path)
        counts["package"] += 1
    for path in example_files():
        files[path.relative_to(ROOT).as_posix()] = read_text(path)
        counts["examples"] += 1
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    bundle = {"format": 1, "hash": digest, **page_meta(), "files": files}
    return bundle, counts


def write_if_changed(path: Path, data: bytes) -> bool:
    """Write data to path unless it is already the same. Returns True if written."""
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def static_files() -> list[Path]:
    out = []
    for path in sorted(PLAYGROUND.rglob("*")):
        rel = path.relative_to(PLAYGROUND)
        if not path.is_file() or SKIP_PARTS.intersection(rel.parts):
            continue
        if path.suffix.lower() in STATIC_SUFFIXES:
            out.append(path)
    return out


def human(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the JarLang playground.")
    parser.add_argument("--out", metavar="DIR",
                        help="build the whole site into DIR so it can be uploaded. "
                             "Without this, only the bundle is written to playground/")
    parser.add_argument("--quiet", action="store_true", help="only print errors")
    args = parser.parse_args(argv)

    def say(text: str) -> None:
        if not args.quiet:
            print(text)

    if not (PACKAGE / "__init__.py").exists():
        print(f"error: cannot find the jarlang package at {PACKAGE}", file=sys.stderr)
        return 1

    out_dir = Path(args.out).resolve() if args.out else PLAYGROUND
    if out_dir == ROOT or out_dir in ROOT.parents:
        print(f"error: refusing to build into {out_dir}", file=sys.stderr)
        return 1

    bundle, counts = build_bundle()
    data = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    bundle_path = out_dir / BUNDLE_DIR / BUNDLE_NAME
    changed = write_if_changed(bundle_path, data)
    say(f"{'wrote' if changed else 'unchanged'} {bundle_path} "
        f"({counts['package']} package files, {counts['examples']} examples, {human(len(data))}, "
        f"JarLang {bundle['version']}, hash {bundle['hash']})")

    if out_dir != PLAYGROUND.resolve():
        copied = same = 0
        for src in static_files():
            dest = out_dir / src.relative_to(PLAYGROUND)
            if write_if_changed(dest, src.read_bytes()):
                copied += 1
                say(f"copied  {src.relative_to(ROOT).as_posix()} -> {dest}")
            else:
                same += 1
        # Tell GitHub Pages not to run Jekyll
        if write_if_changed(out_dir / ".nojekyll", b""):
            copied += 1
        say(f"site ready in {out_dir} ({copied} file(s) written, {same} unchanged)")
    else:
        say("serve it with: jarlang playground")
    return 0


if __name__ == "__main__":
    sys.exit(main())
