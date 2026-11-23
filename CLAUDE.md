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
- **Do not hand-edit what `refresh.sh` copies** (`backend/app/`, `backend/recordings/demo/`, `public/`, `requirements.txt`): the next refresh overwrites it. If the app itself needs a change, that is a change in the main project, decided there, not here.
- Hand-edited files are only: `index.py`, `vercel.json`, `backend/.env`, `refresh.sh`, `README.md`, `.gitignore`, `.python-version`, this file.
- Keep the repo private: it holds the app code, the skill and prompt files, and recordings that contain full prompts.
- macOS: `sed -i` needs `-i.bak`. Use `rm -f` on the `.bak` files afterwards.
- Discuss first, then build; say what you will do and why before changing files, and keep changes small. The owner tries things himself and reports back.

## How the demo works (so you do not rediscover it)

- Vercel runs one FastAPI function from `index.py`. It is not a server that stays up: a new copy starts on demand and is stopped when quiet.
- `backend/.env` has `ADMIN_NAME=demo-user`, so the app seeds exactly one person, an Administrator with every role. `refresh.sh` puts `localStorage["sdlc-workbench.actor"]="demo-user"` into `public/index.html` so the browser never asks who you are (the name is only a label for the audit trail, not a login).
- On the first request to a new copy, `index.py` calls the app's own `app.demo.prepare(app.state, "human:demo-user", <last recorded phase>)`, which builds "Meeting Room Booking" from the recordings in `backend/recordings/demo` (Setup to Architecture approved, Build written and left running). **The actor there must be `human:demo-user`**, the form the app stores (the `X-Actor: demo-user` header becomes that); plain `demo-user` is refused.
- `index.py` also adds a small ASGI middleware, `LookOnly`: any request that is not GET/HEAD/OPTIONS, except `/api/plan/preview`, gets 403 with "This is a look-around demo, so changes are switched off here." The app has no login, so this middleware is the only barrier. Treat any change to it as security-relevant and re-run the offline check (below).
- The database is under `DATA_DIR=/tmp/sdlc-workbench` (Vercel's only writable place), rebuilt empty every time a copy starts. Project ids are random per copy, so a visitor moved to a new copy mid-visit can see "not found" until they go home. A fixed demo project id would fix that, but needs a change to the main app: only if it happens often, and decided in the main project.
- **A recording is replayed only when the whole request is identical.** That is why `backend/.env` must carry the eight `IBM_ICA_*_MODEL_ID` / `*_FALLBACK_MODEL_ID` names and `AGENT_MAX_OUTPUT_TOKENS=16000`, `REVIEW_MAX_OUTPUT_TOKENS=16000` (the app's code defaults for the names are blank). Without them every request misses and the demo project does not build. Keep these in step with the main project's `backend/.env.example` and its recorder (`scripts/demo_record.py`).
- Recordings go stale when the main project changes a prompt, skill, model name or pinned limit. The cure is in the main project: re-record the demo there (its `docs/DEMO.md`), then `./refresh.sh`. A stale demo shows an empty project list; `index.py` prints the reason in the Vercel function log.

## Offline check to run after any change to `index.py` or a refresh

Load `index.py` as Vercel does, with a throwaway `DATA_DIR`, and with `httpx.ASGITransport(app=index.app)` confirm: `/api/health` answers; the first `/api/projects` builds exactly one project ("Meeting Room Booking"); `/api/users` lists only `demo-user`; `/api/projects/{id}/export/zip` (with an `X-Actor` header) returns a zip; POST/PUT/DELETE on projects, settings, users, phase run, documents and `/api/demo/start` all return 403; `/api/plan/preview` is NOT blocked by the middleware; and afterwards still one user and one project. Nothing may be written inside this folder (no `__pycache__`, no `.db`): use `PYTHONDONTWRITEBYTECODE=1`.

## Status (2026-11-23)

- Built and checked offline as above. **Not yet deployed or tested on Vercel.**
- Earlier attempts to deploy from inside the main project with the Vercel command line failed (no saved login, and the project was never created); that approach was dropped for this separate repo imported through the Vercel dashboard.
- Next, in order: owner does `git init`, private GitHub repo, imports it in the Vercel dashboard, deploys; then runs the README checklist and reports what the Vercel build log and the site show. Fix only what that shows.
- Things worth confirming on the first deploy, because they are the untested parts: Vercel picks up `index.py` as the entry point; `includeFiles: "backend/**"` in `vercel.json` actually ships `backend/` (including the dot-file `backend/.env`; if Vercel drops it, the app starts with blank model names and no admin: look there first); Pillow, python-docx and pypdf install; the function stays under the 300 s limit; and Download .docx works.
- If `backend/.env` is dropped from the bundle, the simple fallback is to set the same settings as Vercel project environment variables (they hold no secrets) instead of the file.
- Later, outside this folder: the owner's GitHub Pages site gets a link to the demo and a refresh of its feature list (the profile site repo is `Personal-Projects/parag-jain`, deployed on push to its main).
