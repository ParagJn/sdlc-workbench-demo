"""The logs of a project over HTTP (S32): what happened on each run of each stage, and why anything failed. Read-only.

Anyone who can open the project can read it (no name or role is needed), and an archived project can be read. Nothing here writes, and no model is called. What it returns is built by
`logs/report.py` from rows the app already keeps; there is no prompt and no model answer in any of them.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from .logs import report as R
from .models import NotFoundError

router = APIRouter(prefix="/api/projects/{project_id}/logs")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MD_TYPE = "text/markdown; charset=utf-8"
MAX_EVENTS_PER_RUN = 2000


def _names(request: Request) -> dict[str, str]:
    """Artefact key -> its plain name, and phase key -> its name, from the catalogue."""
    out: dict[str, str] = {}
    for phase in request.app.state.agents.catalogue.phases:
        for artefact in phase.artefacts:
            out[artefact.key] = artefact.name
    return out


def _requesters(store, project_id: str) -> dict[str, str]:
    """run id -> the name that asked for it, from the audit trail."""
    return {a.target_id: a.actor for a in store.audit.list(project_id, action="run.requested")}


def _row(run) -> dict[str, Any]:
    return {"id": run.id, "phase_key": run.phase_key, "status": run.status, "stop_reason": run.stop_reason, "started_at": run.started_at, "finished_at": run.finished_at, "totals": run.totals}


def _event_row(e) -> dict[str, Any]:
    return {"id": e.id, "type": e.type, "at": e.at, "data": e.data}


def _trace_row(t) -> dict[str, Any]:
    return {
        "run_id": t.run_id, "agent": t.agent, "phase_key": t.phase_key, "served_model": t.served_model, "requested_model": t.requested_model, "fallback_used": t.fallback_used,
        "status": t.status, "attempt": t.attempt, "call_id": t.call_id, "tokens_in": t.tokens_in, "tokens_out": t.tokens_out, "latency_ms": t.latency_ms, "cost_usd": t.cost_usd,
        "created_at": t.created_at, "error": t.error,
    }


@router.get("")
def logs(project_id: str, request: Request) -> dict[str, Any]:
    """Every stage with its runs (newest first), how many failed, and the decisions people made on it."""
    store = request.app.state.store
    try:
        project = store.projects.get(project_id)
        phases = store.phases.list(project_id)
        runs = store.runs.list(project_id)
        events = store.run_events.list(project_id, limit=100000)
        audit = store.audit.list(project_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    order = {p.key: (p.order, p.name) for p in request.app.state.agents.catalogue.phases}
    have = {p.phase_key: p.status for p in phases}
    stage_rows = [{"key": key, "name": name, "status": have.get(key, "not_started")} for key, (_, name) in sorted(order.items(), key=lambda kv: kv[1][0])]
    by_run: dict[str, list[dict]] = {}
    for e in events:
        by_run.setdefault(e.run_id, []).append(_event_row(e))
    body = R.stage_list(
        stage_rows, [_row(r) for r in runs], R.decision_rows([{"action": a.action, "target_id": a.target_id, "actor": a.actor, "at": a.at, "detail": a.detail} for a in audit]),
        by_run, _names(request), _requesters(store, project_id), request.app.state.settings.secret_values(),
    )
    return {"project": {"id": project.id, "name": project.name}, **body}


def _one(project_id: str, run_id: str, request: Request) -> tuple[dict[str, Any], str]:
    if not RUN_ID.match(run_id):
        raise HTTPException(status_code=422, detail="run id has unexpected characters")
    store = request.app.state.store
    try:
        project = store.projects.get(project_id)
        run = store.runs.get(project_id, run_id)
        events = store.run_events.list(project_id, run_id=run_id, limit=MAX_EVENTS_PER_RUN)
        traces = [t for t in store.traces.list(project_id) if t.run_id == run_id]
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    detail = R.run_detail(
        _row(run), [_event_row(e) for e in events], [_trace_row(t) for t in traces], _names(request), _requesters(store, project_id).get(run_id),
        request.app.state.settings.secret_values(),
    )
    return detail, project.name


@router.get("/{run_id}")
def one_run(project_id: str, run_id: str, request: Request) -> dict[str, Any]:
    detail, _ = _one(project_id, run_id, request)
    return detail


@router.get("/{run_id}/download")
def download(project_id: str, run_id: str, request: Request) -> Response:
    """The run's log as a Markdown file, to send to whoever will fix it."""
    detail, name = _one(project_id, run_id, request)
    return Response(R.markdown(detail, name), media_type=MD_TYPE, headers={"Content-Disposition": f'attachment; filename="run-{run_id}-log.md"'})
