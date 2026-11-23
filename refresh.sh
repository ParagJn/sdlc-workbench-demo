#!/bin/bash

# SDLC Workbench demo - refresh from the main project
# ==========================================
#   ./refresh.sh /path/to/SDLC-Workbench            copy the latest app, recordings and screens in
#   ./refresh.sh                                     same, using ../SDLC-Workbench
#
# This only READS the main project. It builds the main project's frontend, copies three things into this folder (the app code, the demo recordings and the built site),
# and shows what changed. It never changes the main project, never reads its backend/.env or backend/data, and never commits or pushes: you do that.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-$HERE/../SDLC-Workbench}"
SRC="$(cd "$SRC" 2>/dev/null && pwd)" || { echo "[x] Cannot find the main project at: ${1:-$HERE/../SDLC-Workbench}"; exit 1; }

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
fail()    { echo -e "${RED}[✗]${NC} $1"; exit 1; }

command -v npm   >/dev/null 2>&1 || fail "npm is not installed"
command -v rsync >/dev/null 2>&1 || fail "rsync is not installed"
[ -d "$SRC/backend/app" ] && [ -d "$SRC/frontend" ] || fail "$SRC does not look like the SDLC Workbench project"
[ -d "$SRC/backend/recordings/demo" ] || fail "$SRC/backend/recordings/demo is missing: the demo has nothing to replay"
[ "$SRC" != "$HERE" ] || fail "Give the path of the MAIN project, not this folder"

info "Main project: $SRC"

# 1. Build the main project's frontend (its own dist/ folder; nothing else in it changes) -----------------------------------
info "Building the frontend"
cd "$SRC/frontend"
[ -d node_modules ] || npm install
npm run build
[ -f dist/index.html ] || fail "The frontend build did not produce dist/index.html"
success "Frontend built"

# 2. Copy in: the app, the recordings, the built site, the requirements -----------------------------------------------------
# --delete keeps each copied folder identical to its source. backend/.env, index.py, vercel.json, README and the scripts here are never touched.
info "Copying into $HERE"
mkdir -p "$HERE/backend/recordings" "$HERE/public"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$SRC/backend/app/"              "$HERE/backend/app/"
rsync -a --delete                                           "$SRC/backend/recordings/demo/" "$HERE/backend/recordings/demo/"
rsync -a --delete                                           "$SRC/frontend/dist/"           "$HERE/public/"
cp "$SRC/backend/requirements.txt" "$HERE/requirements.txt"
# The main project's list leaves out python-multipart, which FastAPI needs to load the document-upload routes. It is installed on the owner's own computer, so the main project
# works, but a fresh install (Vercel) crashes on start-up without it. Add it back after every copy; harmless once the main project lists it too.
grep -qi "^python-multipart" "$HERE/requirements.txt" || printf 'python-multipart>=0.0.20   # added by refresh.sh: FastAPI needs it for the upload routes\n' >> "$HERE/requirements.txt"

# 3. Everyone is demo-user: the app keeps "who you are" in the browser, so put the name there before the app starts. This edits the COPY here only.
sed -i.bak 's#<div id="root"></div>#<script>try{localStorage.setItem("sdlc-workbench.actor","demo-user")}catch(e){}</script><div id="root"></div>#' "$HERE/public/index.html"
rm -f "$HERE/public/index.html.bak"
grep -q "sdlc-workbench.actor" "$HERE/public/index.html" || fail "Could not set the demo-user name in public/index.html (the page's root element changed?)"

# 3b. Save the finished demo project as a database file (backend/seed/demo.db), so every copy of the app on Vercel starts from the SAME project, with the same ids.
# It replays the recordings just copied in (no model call, no server, nothing written outside a throwaway folder). If the recordings are stale it stops here.
PYTHON="${PYTHON:-/Users/paragjain/dev-works/myenv/bin/python}"
[ -x "$PYTHON" ] || fail "Python not found at $PYTHON (set PYTHON=/path/to/python to use another)"
info "Building the demo project seed"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$HERE/build_seed.py" || fail "The seed could not be built: the recordings may be out of date (see README.md)"
success "Seed built"

# 4. Belt and braces: nothing private may be in this folder, since it is about to be pushed ---------------------------------
[ ! -e "$HERE/backend/data" ] || fail "backend/data is in this folder; stopping"
if grep -rqE "^[A-Z_]*(KEY|SECRET|TOKEN|PASSWORD)=.+" "$HERE/backend/.env"; then fail "backend/.env holds something that looks like a key; stopping"; fi
grep -q "^ADMIN_NAME=demo-user" "$HERE/backend/.env" || fail "backend/.env must say ADMIN_NAME=demo-user (index.py builds the demo as that person)"
success "Copied"

# 5. Show what changed, for you to look at before you commit ------------------------------------------------------------------
echo
if [ -d "$HERE/.git" ]; then
  git -C "$HERE" add -N . >/dev/null 2>&1 || true
  git -C "$HERE" --no-pager diff --stat | tail -25
  echo
  warn "Look at the list above. When happy:  git add -A && git commit -m \"Refresh the demo\" && git push   (Vercel then redeploys)"
else
  warn "This folder is not a git repository yet. See README.md."
fi
