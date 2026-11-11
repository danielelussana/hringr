#!/bin/bash
# Lancia la versione web. Esempio: ./run_web.sh --port 8080
exec "$(dirname "$(readlink -f "$0")")/.venv/bin/python" "$(dirname "$(readlink -f "$0")")/hringr_web.py" "$@"
