#!/usr/bin/env bash
# Run the tests
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] && [ ! -x .venv/Scripts/python.exe ]; then bash scripts/setup.sh; fi
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python.exe; fi
exec "$PY" -m pytest "$@"
