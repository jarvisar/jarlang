#!/usr/bin/env bash
# Run JarLang from .venv, e.g. scripts/jarlang.sh examples/hello.jlang
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -x "$root/.venv/bin/python" ] && [ ! -x "$root/.venv/Scripts/python.exe" ]; then
  bash "$root/scripts/setup.sh"
fi
if [ -x "$root/.venv/bin/python" ]; then PY="$root/.venv/bin/python"; else PY="$root/.venv/Scripts/python.exe"; fi
exec "$PY" -m jarlang "$@"
