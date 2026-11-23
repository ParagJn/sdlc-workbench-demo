"""The usage report over HTTP (S25): what one project's model calls cost, in words and figures. Read-only.

Anyone who can open the project can read it (no name or role is needed), and an archived project can be read. Nothing here writes, and no model is called.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .models import NotFoundError
from .usage.report import build_report

router = APIRouter(prefix="/api/projects/{project_id}/usage")


@router.get("")
def usage(project_id: str, request: Request) -> dict[str, Any]:
    store = request.app.state.store
    try:
        project = store.projects.get(project_id)
        rows = store.traces.usage_rows(project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return build_report({"id": project.id, "name": project.name, "created_at": project.created_at}, rows)
