#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PY=/opt/homebrew/bin/python3.12
[ -d .venv ] || { "$PY" -m venv .venv; ./.venv/bin/pip install -q -U pip; \
  ./.venv/bin/pip install -q -r requirements.txt; }
./.venv/bin/python -m app.seed
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8012 "$@"
