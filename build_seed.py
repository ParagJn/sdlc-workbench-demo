"""Builds backend/seed/demo.db: the finished demo project, saved as a database file (run by refresh.sh; or by hand: python build_seed.py).

Why: Vercel starts several copies of the app at once, and each one used to build its OWN "Meeting Room Booking" with its own random ids, so the list from one copy named a project
the next copy did not know ("project ... not found"). With a seed every copy starts from the SAME file, so the ids are the same everywhere (and the first visit is instant).

It is the same build index.py used to do on the first visit: the app's own `prepare()`, as the Administrator, replaying the recordings (no model call, no network, no server), into a
throwaway data folder outside this repository. Only the finished database file is copied in. Nothing else is written here.
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "backend" / "seed" / "demo.db"

sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE / "backend"))


async def build(work: str) -> Path:
    os.environ["DATA_DIR"] = work                       # an environment value beats backend/.env, so nothing is built in /tmp/sdlc-workbench or in here
    from app.demo import prepare, recorded_phases       # noqa: E402  (the paths above have to be set first)
    from app.main import create_app                     # noqa: E402

    app = create_app()
    settings = app.state.settings
    if not settings.admin_name:
        raise SystemExit("backend/.env must say ADMIN_NAME=demo-user: the demo is built as that person.")
    phases = recorded_phases(settings)
    if not phases:
        raise SystemExit("No recorded demo found (backend/recordings/demo/recorded_stages.txt): nothing to build.")
    await prepare(app.state, f"human:{settings.admin_name}", phases[-1])
    app.state.store.db.checkpoint()                     # fold the write-ahead log into the one file, so the one file is the whole database
    return settings.db_path


def check(path: Path) -> str:
    """Open the finished file on its own and say what is in it; stop if it is not exactly one project."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("The finished database failed SQLite's integrity check; nothing was saved.")
        projects = [r[0] for r in conn.execute("SELECT name FROM projects")]
        artefacts = conn.execute("SELECT COUNT(*) FROM artefacts").fetchone()[0]
        users = [r[0] for r in conn.execute("SELECT name FROM users")]
    finally:
        conn.close()
    if len(projects) != 1:
        raise SystemExit(f"Expected exactly one project in the seed, found {len(projects)}: {projects}")
    return f"project {projects[0]!r}, {artefacts} artefacts, people {users}"


def main() -> None:
    work = tempfile.mkdtemp(prefix="sdlc-demo-seed.")
    try:
        db = asyncio.run(build(work))
        summary = check(db)
        SEED.parent.mkdir(parents=True, exist_ok=True)
        part = SEED.with_name(SEED.name + ".part")
        shutil.copyfile(db, part)
        os.replace(part, SEED)                          # whole file or nothing
        print(f"seed saved: {SEED.relative_to(HERE)} ({SEED.stat().st_size / 1e6:.2f} MB): {summary}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
