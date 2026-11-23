"""Run the demo on this computer, the way Vercel runs it (for trying it out before a deploy; Vercel does not use this file).

`index.py` is imported exactly as it is, so the look-only wrapper and the first-visit build are the real ones. The one thing Vercel does that a plain server does not is serve
`public/` (the built site) at the front door: that is all this adds. Start it with ./start.sh, which also gives it a throwaway database.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uvicorn  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import index  # noqa: E402  (the same file Vercel loads)

app = index.app
app.mount("/", StaticFiles(directory=HERE / "public", html=True), name="site")   # added last, so every /api route is matched first

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), log_level="info")
