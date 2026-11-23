# SDLC Workbench: look-around demo

A hosted, read-only copy of SDLC Workbench for a public link. A visitor opens it already signed in as `demo-user` and finds a recorded sample project ("Meeting Room Booking"): its phases, documents, board, diagrams, trace and downloads. They cannot change anything.

It replays recorded model replies, so it needs **no model key**, makes **no model calls** and costs nothing to run.

This is a **separate project** with its own repository. It is a copy, not a link: the main project does not know it exists. Keep this repository **private**: it holds the app code, its prompt files and the recordings (which contain full prompts).

## What is here

| Path | What it is | Changed by |
|---|---|---|
| `backend/app/` | The app's code, as copied | `refresh.sh` |
| `backend/recordings/demo/` | The recorded model replies the demo plays | `refresh.sh` |
| `public/` | The built website, with the name `demo-user` set in the page | `refresh.sh` |
| `requirements.txt` | The app's Python packages (`refresh.sh` adds `python-multipart` after copying: the main project's list leaves it out, and the app will not start on a fresh install without it) | `refresh.sh` |
| `backend/seed/demo.db` | The finished sample project saved as a database file (about 1 MB). Every copy of the app on Vercel starts from it, so they all have the same project with the same ids | `refresh.sh` (runs `build_seed.py`) |
| `build_seed.py` | Builds that file offline from the recordings: no server, no model call | you, by hand |
| `backend/.env` | The demo's only settings: replay mode, `ADMIN_NAME=demo-user`, model names and prices, the database in `/tmp`. **No keys.** | you, by hand |
| `index.py` | Vercel's door. Builds the demo project on the first visit and turns away every request that would change something | you, by hand |
| `vercel.json` | The function's time limit (300 s, the Hobby maximum) and the `backend/` folder to include | you, by hand |
| `refresh.sh` | Pulls the latest app, recordings and screens in from the main project | |
| `start.sh`, `local_serve.py` | Try the demo on your own computer before (or without) Vercel: `./start.sh`, then open http://127.0.0.1:8000. Throwaway database outside this folder; Vercel does not use these | you, by hand |

## How it works

Vercel is not a server that stays on: it starts a copy of the app when a visit arrives, often several at once, and stops them after a quiet spell. Each new copy starts from the same saved database (`backend/seed/demo.db`, copied into `/tmp`), so every copy has the same sample project with the same ids and the first visit is instant. `demo-user` is the only person, an Administrator. The database in `/tmp` is thrown away with the copy, so nothing a visitor does can last, and writes are switched off anyway.

If the seed is missing or unusable, `index.py` says so in the function log and falls back to building the project from the recordings on the first visit (a few seconds; but then copies can disagree about the project's id, and a visitor may see "not found").

## First publish

1. **Refresh** (builds the main project's screens and copies everything in):
   ```bash
   ./refresh.sh /Users/paragjain/dev-works/SDLC-Workbench
   ```
2. **Make it a repository and push it** (private):
   ```bash
   git init -b main
   git add -A
   git status        # look at it: there must be no data folder and no key anywhere
   git commit -m "SDLC Workbench look-around demo"
   gh repo create sdlc-workbench-demo --private --source=. --push
   ```
3. **Import it in Vercel**: dashboard, Add New, Project, pick `sdlc-workbench-demo`. Vercel detects FastAPI. Leave every setting as it is and press Deploy. (If the dashboard cannot see a private repository, grant the Vercel GitHub app access to it under GitHub, Settings, Applications.)
4. Open the address Vercel gives. Put it on your GitHub Pages site.

## Updating the demo (from time to time)

```bash
./refresh.sh /Users/paragjain/dev-works/SDLC-Workbench      # copies the latest in, rebuilds the seed, shows what changed
git add -A && git commit -m "Refresh the demo" && git push   # Vercel redeploys on its own
```

`refresh.sh` also rebuilds `backend/seed/demo.db` from the recordings, so the seed cannot drift from them. If a prompt changed in the main project, re-record the demo there first (its `docs/DEMO.md`): with out-of-date recordings the refresh stops at the seed step and says so.

## Check after a deploy

- [ ] The site opens on the app (not a blank page) and does not ask who you are.
- [ ] `/api/health` answers `{"ok":true}`.
- [ ] The project list shows "Meeting Room Booking" (after a few seconds on the first visit).
- [ ] The Users page shows only `demo-user`.
- [ ] A document opens; "Download .docx" works.
- [ ] A save or approve button shows "This is a look-around demo, so changes are switched off here."

If the project list is empty, open the function log in Vercel: `index.py` prints the reason.

## Limits

- **The database is temporary** (`/tmp`, not shared between copies), but every copy starts from the same seed, so the project id is the same everywhere. Only if the seed is missing or unusable can copies differ (see How it works).
- **Nothing runs here**: no key, writes off. Build Verify (which runs generated code) is not available, and the recorded demo stops before it anyway.
- **The address is public** unless you turn on Vercel's Deployment Protection, if your plan offers it. The look-only wrapper in `index.py` is the only barrier, since the app has no login.
- Deployed and working on Vercel. Not yet confirmed there: Download .docx.
