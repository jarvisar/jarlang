#!/usr/bin/env bash
# Build the VS Code extension and install it (needs Node.js)
set -e
cd "$(dirname "$0")/../editors/vscode"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm was not found. Install Node.js from https://nodejs.org/"
  exit 1
fi
npm install
npm run package

if command -v code >/dev/null 2>&1; then
  code --install-extension jarlang.vsix --force
  echo "Installed. Reload VS Code to use it."
else
  echo "Built editors/vscode/jarlang.vsix. Install it from VS Code: Extensions, then \"Install from VSIX\"."
fi
