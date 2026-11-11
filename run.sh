#!/bin/bash
# Lancia hringr.py con il venv locale. Esempio: ./run.sh --fullscreen
exec "$(dirname "$(readlink -f "$0")")/.venv/bin/python" "$(dirname "$(readlink -f "$0")")/hringr.py" "$@"
