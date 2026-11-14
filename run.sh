#!/bin/bash
# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lancia hringr.py con il venv locale. Esempio: ./run.sh --fullscreen
exec "$(dirname "$(readlink -f "$0")")/.venv/bin/python" "$(dirname "$(readlink -f "$0")")/hringr.py" "$@"
