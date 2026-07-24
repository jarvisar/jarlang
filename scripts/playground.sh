#!/usr/bin/env bash
# Build the playground and open it in a browser (Ctrl+C to stop)
exec bash "$(dirname "$0")/jarlang.sh" playground "$@"
