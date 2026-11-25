#!/bin/sh
set -eu
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
