#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
V="${SAZED_CHECK_VENV:-$HOME/.cache/sazed-check-venv}"
[ -x "$V/bin/python" ] || uv venv -q -p 3.12 "$V"
VIRTUAL_ENV="$V" uvx -q --from poetry==2.1.4 poetry install -q --no-interaction --no-root
uvx -q ruff@0.17.1 check --select E9,F63,F7,F82 .
uvx -q ruff@0.17.1 check app/notes $(ls app/routers/notes.py tests/test_notes_*.py 2>/dev/null)
"$V/bin/python" -c "import app.main"
"$V/bin/python" -m pytest -q
