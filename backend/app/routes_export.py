"""Download the artefacts of a project (the user's request, 2026-11-17): a directory listing, one file, or everything as a zip. For an active Administrator only, checked on every call.

Read-only: nothing is changed and no model is called. The Word files are made from the saved text on the server (`app/export.py`)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from . import export as X
from .access.roles import ADMIN
from .actor import actor_from
from .models import InvalidInput, NotFoundError

router = APIRouter(prefix="/api/projects/{project_id}/export")
ZIP_TYPE = "application/zip"
TYPES = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "md": "text/markdown; charset=utf-8"}


def _admin(request: Request) -> None:
    """Only an active Administrator may download the artefacts. The name comes from the same header every other action uses."""
    actor = actor_from(request)
    me = request.app.state.store.users.find(actor.removeprefix("human:"))
    if me is None or not me.active or ADMIN not in me.roles:
        raise HTTPException(status_code=403, detail=f"only an active {ADMIN} can download the project's artefacts; ask one of them")


def _items(request: Request, project_id: str) -> tuple[str, list[X.Item]]:
    store = request.app.state.store
    try:
        project = store.projects.get(project_id)
        items = []
        for a in store.artefacts.list(project_id):
            if a.latest_version is None or a.phase_key not in X.PHASES:
                continue
            v = store.artefacts.get_version(project_id, a.id)
            items.append(X.Item(a.phase_key, a.type, a.id, a.title, v.number, v.body, v.created_at))
    except (NotFoundError, InvalidInput) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, NotFoundError) else 422, detail=str(exc)) from exc
    return project.name, items


@router.get("")
def listing(project_id: str, request: Request) -> dict:
    """The folders and files a download would hold, with their kind and size. Nothing is sent but names and numbers."""
    _admin(request)
    name, items = _items(request, project_id)
    entries = X.entries_for(items, name)
    return {"zip_name": X.zip_name(name), "folders": X.listing(entries), "files": len(entries), "bytes": sum(len(e.data) for e in entries), "not_yet": list(X.NOT_YET)}


@router.get("/zip")
def download_zip(project_id: str, request: Request) -> Response:
    _admin(request)
    name, items = _items(request, project_id)
    entries = X.entries_for(items, name)
    if not entries:
        raise HTTPException(status_code=404, detail="nothing has been written yet, so there is nothing to download")
    return Response(X.zip_bytes(entries), media_type=ZIP_TYPE, headers={"Content-Disposition": f'attachment; filename="{X.zip_name(name)}"'})


@router.get("/file")
def download_file(project_id: str, request: Request, path: str = Query(min_length=1, max_length=300)) -> Response:
    """One file of the listing, by its path in it. A path that is not in the listing is not served, whatever it says."""
    _admin(request)
    name, items = _items(request, project_id)
    found = next((e for e in X.entries_for(items, name) if e.path == path), None)
    if found is None:
        raise HTTPException(status_code=404, detail="that file is not in this project's artefacts")
    base = found.path.rsplit("/", 1)[1]
    return Response(found.data, media_type=TYPES[found.kind], headers={"Content-Disposition": f'attachment; filename="{base}"'})
