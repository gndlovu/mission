#!/bin/sh
# Operator OS launcher
exec python3 "$(dirname "$0")/server.py" --port "${PORT:-8081}" "$@"
