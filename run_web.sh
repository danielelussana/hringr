#!/bin/bash
# Copyright (C) 2026 Daniele "DLx" Lussana
# SPDX-License-Identifier: AGPL-3.0-or-later
# Lancia la versione web. Esempio: ./run_web.sh --port 8080
exec "$(dirname "$(readlink -f "$0")")/.venv/bin/python" "$(dirname "$(readlink -f "$0")")/hringr_web.py" "$@"
