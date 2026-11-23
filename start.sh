#!/bin/bash

# SDLC Workbench look-around demo - try it on this computer
# ==========================================
#   ./start.sh            start on http://127.0.0.1:8000
#   PORT=8010 ./start.sh  use another port
#
# Runs index.py as Vercel does, with the built site in public/ served at the front door (local_serve.py).
# The database is a throwaway folder OUTSIDE this repository, emptied on every start (like a fresh Vercel copy), so nothing is ever written in here.
# Stop with Ctrl+C. Nothing here calls a model: the demo only replays recordings.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/Users/paragjain/dev-works/myenv/bin/python"
PORT="${PORT:-8000}"

GREEN='\033[0;32m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

[ -x "$PYTHON" ] || fail "Python not found at $PYTHON"
[ -f "$HERE/public/index.html" ] || fail "public/index.html is missing: run ./refresh.sh first"
"$PYTHON" -c "import fastapi, uvicorn, docx, PIL, pypdf" 2>/dev/null || fail "A package is missing in that Python: $PYTHON -m pip install -r $HERE/requirements.txt"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail "Port $PORT is already in use. Try: PORT=8010 ./start.sh"; fi

# An environment value beats backend/.env, so this moves the database out of /tmp/sdlc-workbench and out of the repo.
DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/sdlc-demo-local.XXXXXX")"
export DATA_DIR
export PYTHONDONTWRITEBYTECODE=1
trap 'rm -rf "$DATA_DIR"' EXIT

info "Database (throwaway): $DATA_DIR"
echo -e "${GREEN}[✓]${NC} Open  http://127.0.0.1:$PORT   (the first page load builds the sample project: a few seconds)"
echo

cd "$DATA_DIR"      # so a stray relative path can never land inside the repository
PORT="$PORT" "$PYTHON" "$HERE/local_serve.py"
