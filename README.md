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
| `requirements.txt` | The app's Python packages | `refresh.sh` |
| `backend/.env` | The demo's only settings: replay mode, `ADMIN_NAME=demo-user`, model names and prices, the database in `/tmp`. **No keys.** | you, by hand |
| `index.py` | Vercel's door. Builds the demo project on the first visit and turns away every request that would change something | you, by hand |
| `vercel.json` | The function's time limit (300 s, the Hobby maximum) and the `backend/` folder to include | you, by hand |
| `refresh.sh` | Pulls the latest app, recordings and screens in from the main project | |

## How it works

Vercel is not a server that stays on: it starts a copy of the app when a visit arrives and stops it after a quiet spell. On the first visit to each new copy, `index.py` lets the app start (an empty database in `/tmp`, with `demo-user` as the only person, an Administrator), then builds the sample project from the recordings, using the app's own prepared-demo steps. The first visit takes a few seconds; the rest are normal. The database is rebuilt every time, so nothing a visitor does can last, and writes are switched off anyway.

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
./refresh.sh /Users/paragjain/dev-works/SDLC-Workbench      # shows what changed
git add -A && git commit -m "Refresh the demo" && git push   # Vercel redeploys on its own
```

If a prompt changed in the main project, re-record the demo there first (its `docs/DEMO.md`). With out-of-date recordings the first visit shows an empty project list and the reason is in the function log.

## Check after a deploy

- [ ] The site opens on the app (not a blank page) and does not ask who you are.
- [ ] `/api/health` answers `{"ok":true}`.
- [ ] The project list shows "Meeting Room Booking" (after a few seconds on the first visit).
- [ ] The Users page shows only `demo-user`.
- [ ] A document opens; "Download .docx" works.
- [ ] A save or approve button shows "This is a look-around demo, so changes are switched off here."

If the project list is empty, open the function log in Vercel: `index.py` prints the reason.

## Limits

- **The database is temporary** (`/tmp`, not shared): a new copy of the app means a new database and a new project id. A visitor who is moved to a new copy mid-visit may see "not found" and should go back to the home page.
- **Nothing runs here**: no key, writes off. Build Verify (which runs generated code) is not available, and the recorded demo stops before it anyway.
- **The address is public** unless you turn on Vercel's Deployment Protection, if your plan offers it. The look-only wrapper in `index.py` is the only barrier, since the app has no login.
- Not tested on Vercel until you deploy it.
