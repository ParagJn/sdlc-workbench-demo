"""Vercel's door into SDLC Workbench: a look-around demo (nothing here changes the app, which is called exactly as it is).

Each time Vercel starts a copy of the app, the first visit does three things, all outside the app's own code:
  1. the app starts as it always does: an empty database in /tmp, with ONE person on the Users list, `demo-user`, an Administrator (ADMIN_NAME in backend/.env);
  2. the recorded demo project is built as `demo-user`, through the app's own prepared-demo steps (no model call: it replays recordings);
  3. from then on every request that would CHANGE something is turned away with a sentence, so visitors can look around but not break it. Reading, downloads
     and the live-progress feed work as normal.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from app.config import get_settings  # noqa: E402  (the path above has to be set first)
from app.demo import prepare, recorded_phases  # noqa: E402
from app.main import create_app  # noqa: E402

USER = "human:demo-user"                    # how the app writes a person in its audit trail (X-Actor "demo-user" becomes this); keep in step with ADMIN_NAME
LOOK_ONLY = "This is a look-around demo, so changes are switched off here. You can open every document, board, diagram and download."
ALLOWED_POSTS = {"/api/plan/preview"}      # asks the server a question (what would this plan mean?) and changes nothing
SEED = Path(__file__).resolve().parent / "backend" / "seed" / "demo.db"     # the finished demo project, built by build_seed.py (refresh.sh runs it)


def _seed_is_sound() -> bool:
    """Look inside the seed before using it (read-only, the file is never touched): a whole database with a project in it."""
    try:
        conn = sqlite3.connect(f"file:{SEED}?mode=ro&immutable=1", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok" and conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] >= 1
        finally:
            conn.close()
    except Exception:
        traceback.print_exc()
        return False


def _plant_seed() -> bool:
    """Start this copy of the app from the saved demo project, so EVERY copy has the same project with the same ids. (Vercel runs several copies at once; when each built its
    own, the list from one named a project another did not know.) False, and nothing is done, when there is no seed, it is not sound, or a database is already there: the app
    then starts empty and builds the project itself on the first visit (the old way)."""
    db = get_settings().db_path
    if not SEED.is_file() or db.exists():
        return False
    if not _seed_is_sound():
        print("demo: the seed is not usable; starting empty and building the project instead", flush=True)
        return False
    db.parent.mkdir(parents=True, exist_ok=True)
    part = db.with_name(db.name + ".part")
    shutil.copyfile(SEED, part)
    os.replace(part, db)
    return True


_seeded = _plant_seed()

app = create_app()      # keep this a plain `app = create_app()` line: Vercel's build looks for exactly this to find the app

_state = {"ready": _seeded, "lock": None}      # ready: nothing left to build


async def _build_demo_project() -> None:
    """Once per copy of the app, before the first real request is answered. A failure is logged and the site still opens (empty), never retried in a loop."""
    if _state["lock"] is None:
        _state["lock"] = asyncio.Lock()
    async with _state["lock"]:
        if _state["ready"]:
            return
        try:
            phases = recorded_phases(app.state.settings)
            if phases:
                await prepare(app.state, USER, phases[-1])
            else:
                print("demo: no recorded demo found; the site will open with no project", flush=True)
        except Exception:
            traceback.print_exc()
        _state["ready"] = True


class LookOnly:
    """Plain ASGI middleware (not the 'http' decorator kind, so live-progress streaming is untouched)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["method"] not in ("GET", "HEAD", "OPTIONS") and scope["path"] not in ALLOWED_POSTS:
                body = json.dumps({"detail": LOOK_ONLY}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            if not _state["ready"] and scope["path"] != "/api/health":
                await _build_demo_project()
        await self.inner(scope, receive, send)


app.add_middleware(LookOnly)
