#!/usr/bin/env bash
# Create .venv and install JarLang with the development tools
set -e
cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
else
  echo "Creating .venv"
  if command -v python3 >/dev/null 2>&1; then python3 -m venv .venv; else python -m venv .venv; fi
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python.exe; fi
fi

echo "Installing JarLang"
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -e ".[dev]"

echo
echo "Setup done."
echo "  scripts/jarlang.sh       start JarLang"
echo "  scripts/playground.sh    open the playground"
echo "  scripts/test.sh          run the tests"
