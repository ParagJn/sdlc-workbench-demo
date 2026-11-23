# SDLC Workbench look-around demo: notes for a Claude session working in THIS folder

This folder is a **separate project with its own git repository** (private, `sdlc-workbench-demo`). It is a hosted, read-only copy of SDLC Workbench for a public link
(the owner's GitHub Pages site will point at it). It is deployed on Vercel from this repo. README.md has the layout, the first-publish steps and the after-deploy checklist: read it first.

The real application lives in `/Users/paragjain/dev-works/SDLC-Workbench` (private repo `ParagJn/sdlc-workbench`) and has its own sessions and its own CLAUDE.md. **Do not edit anything there from this session.**
This folder only ever READS from it, and only when the owner runs `./refresh.sh /Users/paragjain/dev-works/SDLC-Workbench`.

## Standing rules

- **Never run the app, servers or UI to self-test** (the owner's global rule: he tests himself). Offline in-process checks are fine: build the app with `httpx.ASGITransport`, a throwaway `DATA_DIR`, no server, no network. Run Python with the venv `/Users/paragjain/dev-works/myenv/bin/python`.
- **No model calls, ever.** The demo has no key and runs in replay mode only. Nothing here may add a key, a token or a password to any file.
- **Never read or print the main project's `backend/.env`**, and never copy it. This folder's own `backend/.env` is committed on purpose and holds names and prices only.
- **The owner commits and pushes**, and does the Vercel dashboard steps. Never commit, push, or run `vercel` / `gh repo create` for him.
- **Do not hand-edit what `refresh.sh` copies or builds** (`backend/app/`, `backend/recordings/demo/`, `public/`, `requirements.txt`, `backend/seed/demo.db`): the next refresh overwrites it. If the app itself needs a change, that is a change in the main project, decided there, not here. One exception is kept alive on purpose: `refresh.sh` appends `python-multipart` to `requirements.txt` after copying (the main project's list leaves it out; without it the app crashes on start-up on a fresh install such as Vercel).
- Hand-edited files are only: `index.py`, `build_seed.py`, `vercel.json`, `backend/.env`, `refresh.sh`, `start.sh`, `local_serve.py`, `README.md`, `.gitignore`, `.python-version`, this file.
- **Keep `app = create_app()` in `index.py` a plain top-level assignment.** Vercel's build finds the app by that shape; `app, x = make()` made the build fail with FASTAPI_ENTRYPOINT_NOT_FOUND (2026-11-23).
- `start.sh` + `local_serve.py` are for the owner to try the demo on his own computer (`./start.sh`, http://127.0.0.1:8000). Vercel does not use them. `local_serve.py` imports `index.app` unchanged and only adds `public/` at `/`; the database is a throwaway folder outside the repo. Claude never starts it.
- Keep the repo private: it holds the app code, the skill and prompt files, and recordings that contain full prompts.
- macOS: `sed -i` needs `-i.bak`. Use `rm -f` on the `.bak` files afterwards.
- Discuss first, then build; say what you will do and why before changing files, and keep changes small. The owner tries things himself and reports back.

## How the demo works (so you do not rediscover it)

- Vercel runs one FastAPI function from `index.py`. It is not a server that stays up: a new copy starts on demand and is stopped when quiet.
- `backend/.env` has `ADMIN_NAME=demo-user`, so the app seeds exactly one person, an Administrator with every role. `refresh.sh` puts `localStorage["sdlc-workbench.actor"]="demo-user"` into `public/index.html` so the browser never asks who you are (the name is only a label for the audit trail, not a login).
- **The demo project comes from a saved database, `backend/seed/demo.db`** (about 1 MB, built by `build_seed.py`, which `refresh.sh` runs after copying). When a copy starts, `index.py` checks the seed read-only (SQLite quick check, at least one project) and copies it into `DATA_DIR` before the app opens, so every copy has the SAME "Meeting Room Booking" with the same ids. This fixed "project '...' not found": Vercel starts several copies at once, and when each built its own project from the recordings the ids (random `uuid4`) differed between copies (seen on the live site: 12 parallel requests gave 4 different ids).
- **Fallback, the old way:** with no seed, or an unusable one, `index.py` logs it and the first request to that copy builds the project with the app's own `app.demo.prepare(app.state, "human:demo-user", <last recorded phase>)` from the recordings in `backend/recordings/demo` (Setup to Architecture approved, Build written and left running). `build_seed.py` runs the same `prepare()` offline, in-process, in a throwaway folder. **The actor must be `human:demo-user`**, the form the app stores (the `X-Actor: demo-user` header becomes that); plain `demo-user` is refused.
- The seed holds the dates of the day it was built; that is expected. A stale seed is not possible after a refresh (it is rebuilt every time); if recordings are stale, `refresh.sh` stops at the seed step.
- `index.py` also adds a small ASGI middleware, `LookOnly`: any request that is not GET/HEAD/OPTIONS, except `/api/plan/preview`, gets 403 with "This is a look-around demo, so changes are switched off here." The app has no login, so this middleware is the only barrier. Treat any change to it as security-relevant and re-run the offline check (below).
- The database is under `DATA_DIR=/tmp/sdlc-workbench` (Vercel's only writable place), rebuilt empty every time a copy starts. Project ids are random per copy, so a visitor moved to a new copy mid-visit can see "not found" until they go home. A fixed demo project id would fix that, but needs a change to the main app: only if it happens often, and decided in the main project.
- **A recording is replayed only when the whole request is identical.** That is why `backend/.env` must carry the eight `IBM_ICA_*_MODEL_ID` / `*_FALLBACK_MODEL_ID` names and `AGENT_MAX_OUTPUT_TOKENS=16000`, `REVIEW_MAX_OUTPUT_TOKENS=16000` (the app's code defaults for the names are blank). Without them every request misses and the demo project does not build. Keep these in step with the main project's `backend/.env.example` and its recorder (`scripts/demo_record.py`).
- Recordings go stale when the main project changes a prompt, skill, model name or pinned limit. The cure is in the main project: re-record the demo there (its `docs/DEMO.md`), then `./refresh.sh`. A stale demo shows an empty project list; `index.py` prints the reason in the Vercel function log.

## Offline check to run after any change to `index.py` or a refresh

Load `index.py` as Vercel does, with a throwaway `DATA_DIR`, and with `httpx.ASGITransport(app=index.app)` confirm: `/api/health` answers; the first `/api/projects` shows exactly one project ("Meeting Room Booking"), from the seed (`index._seeded` is True); a SECOND separate process with its own empty `DATA_DIR` shows the same project id, and 12 parallel requests all name that one id; with the seed removed, and with a corrupt seed, the project is still built on the first visit; `/api/users` lists only `demo-user`; `/api/projects/{id}/export/zip` (with an `X-Actor` header) returns a zip; POST/PUT/DELETE on projects, settings, users, phase run, documents and `/api/demo/start` all return 403; `/api/plan/preview` is NOT blocked by the middleware; and afterwards still one user and one project. Nothing may be written inside this folder (no `__pycache__`, no `.db`): use `PYTHONDONTWRITEBYTECODE=1`.

## Status (2026-11-23)

- **Deployed on Vercel and working** (https://sdlc-workbench-demo.vercel.app/), from the private GitHub repo `ParagJn/sdlc-workbench-demo` imported through the Vercel dashboard (the earlier attempt to deploy with the command line from the main project was dropped). The owner is going through the README checklist himself.
- What went wrong on the way, and what fixed it (so none of it is rediscovered):
  1. Every `/api/*` call returned 500 (`FUNCTION_INVOCATION_FAILED`). The runtime log showed FastAPI refusing the upload route: **`python-multipart` is missing from the main project's `requirements.txt`** (it is installed on the owner's machine, so the main app never noticed). Fixed by adding it to `requirements.txt` here and making `refresh.sh` append it after each copy. The main project's list should get it too (the owner's decision, in that project).
  2. "project '...' not found" on the first click: several copies of the app ran at once, each built its own project with random ids. Fixed by the saved database `backend/seed/demo.db` (see above).
  3. The build failed with FASTAPI_ENTRYPOINT_NOT_FOUND after `app = create_app()` was changed into a tuple assignment. Restored; see the standing rule above.
- Confirmed on Vercel: Python 3.12 and the requirements install (Pillow, python-docx and pypdf included), `index.py` is found as the entry point, the site opens, the project list loads, the project opens without "not found".
- **Not confirmed, look here first if something is off:** whether `backend/.env` itself ships in the bundle (the same settings were also added as Vercel project environment variables, which hold no secrets, so the app works either way; if they are ever removed and the project list goes empty, check this); whether Download .docx works; the 300 s limit (no longer a concern: the seed makes the first visit instant).
- `start.sh` and `local_serve.py` are in the repo too (pushed by accident, 2026-11-23): harmless, Vercel ignores them.
- Decided against at the owner's choice: greying out the buttons that cannot work here. The 403 message on press is enough (it would need a `look_only` flag in the main project's screens).
- Later, outside this folder: the owner's GitHub Pages site gets a link to the demo and a refresh of its feature list (the profile site repo is `Personal-Projects/parag-jain`, deployed on push to its main).
