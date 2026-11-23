"""Artefacts, the two plain gate actions, and the list of runs over HTTP (S12b).

An artefact is read, never written, here: the writes are an agent run (S09) and, in S12c, a person's edit. Cited sources are
returned with their text because the reader needs to see what a claim rests on; that text is the same the agent was shown.
The two gate actions (hand to review, approve) follow `allowed_actions` exactly: this file decides nothing about when they are
allowed, the workflow engine does, and its sentence is what the screen shows.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .actor import actor_from
from .archive import refuse_if_archived
from .architecture.diagram import names_for, render_all
from .architecture.word import body_for_word
from .models import ArtefactVersion, IllegalTransition, InvalidInput, NotFoundError
from .review.errors import NotAnApprover, WrongPhaseState
from .routes import _run_payload, _translate
from .routes_review_shape import review_payload
from .workflow.gates import GateNotMet, PrerequisiteNotMet

router = APIRouter(prefix="/api/projects/{project_id}")

KEY = r"^[a-z][a-z0-9_]{0,63}$"
ARTEFACT_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
MAX_SOURCES = 40
MAX_REASON_CHARS = 1000


def _store(request: Request):
    return request.app.state.store


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, NotAnApprover):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (PrerequisiteNotMet, GateNotMet, IllegalTransition, WrongPhaseState)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _valid(value: str, pattern: str) -> bool:
    return bool(re.fullmatch(pattern, value))


def _money(value: Any) -> float | None:
    return None if value is None else float(value)


def generation_summary(generation: Any) -> dict[str, Any] | None:
    """What a person needs to know about how a version was made. The prompt and the raw replies are not stored here, so cannot leak."""
    if not isinstance(generation, dict):
        return None
    calls = generation.get("calls") or []
    return {
        "human_edit": bool(generation.get("human_edit")),
        "skill": generation.get("skill"),
        "run_id": generation.get("run_id"),
        "attempts": generation.get("attempts"),
        "repairs": generation.get("repairs"),
        "tokens_in": generation.get("tokens_in"),
        "tokens_out": generation.get("tokens_out"),
        "output_limit": generation.get("output_limit") if isinstance(generation.get("output_limit"), int) else None,
        "near_limit": generation.get("near_limit") if isinstance(generation.get("near_limit"), int) else None,
        "cost_usd": _money(generation.get("cost_usd")),
        "models": sorted({c.get("model") for c in calls if c.get("model")}),
        "fell_back": any(c.get("fallback_used") for c in calls),
        "items": list(generation.get("items") or []),
        "built_on": dict(generation.get("built_on") or {}),
        "guardrails": [
            {"rule": g.get("rule"), "class": g.get("class"), "line": g.get("line"), "heading": g.get("heading"), "length": g.get("length")}
            for g in (generation.get("guardrails") or []) if isinstance(g, dict)
        ],
        "repair_reasons": [
            {"attempt": r.get("attempt"), "problems": [str(p) for p in (r.get("problems") or [])]}
            for r in (generation.get("repair_reasons") or []) if isinstance(r, dict)
        ],
    }


def artefact_card(artefact, version: ArtefactVersion | None) -> dict[str, Any]:
    summary = generation_summary(version.generation) if version is not None else None
    return {
        "id": artefact.id, "type": artefact.type, "phase_key": artefact.phase_key, "title": artefact.title,
        "latest_version": artefact.latest_version, "author": None if version is None else version.author,
        "created_at": None if version is None else version.created_at,
        "models": [] if summary is None else summary["models"],
        "cost_usd": None if summary is None else summary["cost_usd"],
        "attempts": None if summary is None else summary["attempts"],
        "near_limit": None if summary is None else summary["near_limit"],
        "item_count": 0 if summary is None else len(summary["items"]),
        "flags": 0 if summary is None else len(summary["guardrails"]),
    }


def _sources(store, project_id: str, version: ArtefactVersion) -> list[dict[str, Any]]:
    """Each cited source with its text, or a plain note that it is no longer available. Never another project's text."""
    out = []
    for source_id in version.source_chunks[:MAX_SOURCES]:
        hit = store.context.resolve(project_id, source_id)
        if hit is None:
            out.append({"source_id": source_id, "available": False, "title": None, "heading_path": None, "text": None, "page": None})
        else:
            out.append({"source_id": source_id, "available": True, "title": hit.title, "heading_path": hit.heading_path, "text": hit.text, "page": hit.page})
    return out


def _links(store, project_id: str, artefact_id: str, items: list[str]) -> dict[str, list[dict[str, str]]]:
    """Links of this artefact and of the items it defines: what it was derived from, and what is built on it."""
    ids = {artefact_id, *items}
    derived_from, used_by = [], []
    for link in store.links.list(project_id):
        if link.from_id in ids and link.to_id not in ids:
            derived_from.append({"from": link.from_id, "to": link.to_id, "kind": link.kind})
        elif link.to_id in ids and link.from_id not in ids:
            used_by.append({"from": link.from_id, "to": link.to_id, "kind": link.kind})
    return {"derived_from": derived_from, "used_by": used_by}


@router.get("/artefacts")
def list_artefacts(project_id: str, request: Request, phase: str | None = Query(default=None, max_length=64)) -> dict:
    if phase is not None and not _valid(phase, KEY):
        raise HTTPException(status_code=422, detail="phase must be lower-case letters, digits and underscores")
    store = _store(request)
    try:
        store.projects.get(project_id)
        artefacts = store.artefacts.list(project_id, phase_key=phase)
        cards = [artefact_card(a, store.artefacts.get_version(project_id, a.id) if a.latest_version else None) for a in artefacts]
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"artefacts": cards}


@router.get("/build/categories")
async def build_categories(project_id: str, request: Request) -> dict:
    """The five categories of the Build instruction set as it stands (S44): each file saved, missing or older, and whether the category can be built now. Reads only."""
    from .agents.errors import AgentError
    store = _store(request)
    try:
        store.projects.get(project_id)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    try:
        rows = await request.app.state.build.categories(project_id)
    except AgentError as exc:                  # a design that cannot be read yet (an artefact it is built from is missing) is a plain answer for the card, not an error
        return {"categories": [], "unavailable": str(exc)[:300]}
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"categories": [{
        "key": c.key, "label": c.label, "blocked": c.blocked, "saved": c.saved, "missing": c.missing, "older": c.older, "complete": c.complete,
        "files": [{"name": f.name, "kind": f.kind, "title": f.title, "state": f.state, "count": f.count, "why": f.why} for f in c.files],
    } for c in rows], "unavailable": None}


@router.get("/build/map")
async def build_map(project_id: str, request: Request) -> dict:
    """The screens and, for each, the APIs the design already links to it (S45), so the person can confirm or change the screen-to-API map BEFORE it is saved. Reads only; no model."""
    from .agents.errors import AgentError
    store = _store(request)
    try:
        store.projects.get(project_id)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    try:
        return {**await asyncio.to_thread(request.app.state.build.composer.map_choice, project_id), "unavailable": None}
    except AgentError as exc:
        return {"screens": [], "apis": [], "unavailable": str(exc)[:300]}


@router.get("/build/verified")
def build_verified(project_id: str, request: Request) -> dict:
    """Is the Build verified (S45)? The newest Build Verify run judged against the design as it is now, with the reasons when it is not. Reads only: it never runs anyone's code."""
    store = _store(request)
    try:
        store.projects.get(project_id)
        return request.app.state.human.build_verdict(project_id).to_dict()
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc


@router.get("/artefacts/{artefact_id}")
def get_artefact(project_id: str, request: Request, artefact_id: str = Path(max_length=64), version: int | None = Query(default=None, ge=1, le=100000)) -> dict:
    if not _valid(artefact_id, ARTEFACT_ID):
        raise HTTPException(status_code=422, detail="artefact id has unexpected characters")
    store = _store(request)
    try:
        artefact = store.artefacts.get(project_id, artefact_id)
        if artefact.latest_version is None:
            raise NotFoundError(f"artefact {artefact_id!r} has not been written yet")
        shown = store.artefacts.get_version(project_id, artefact_id, version)
        versions = store.artefacts.versions(project_id, artefact_id)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    summary = generation_summary(shown.generation)
    items = [] if summary is None else summary["items"]
    return {
        **artefact_card(artefact, shown),
        "version": shown.number, "body": shown.body, "is_latest": shown.number == artefact.latest_version,
        "generation": summary, "sources": _sources(store, project_id, shown), "links": _links(store, project_id, artefact_id, items),
        "review": review_payload(shown.review_findings),
        "renderings": _renderings(request, store, project_id, artefact.type, shown.body),
        "readable": _readable(artefact.type, shown.body),
        "versions": [{"number": v.number, "author": v.author, "created_at": v.created_at, "human_edit": bool(isinstance(v.generation, dict) and v.generation.get("human_edit"))} for v in versions],
    }


def _readable(artefact_type: str, body: str) -> str | None:
    """The same text laid out for a person (2026-11-22): each table of the Low-level design as a small table and each endpoint as labelled lines. None when nothing in it changes, and for every
    other document. The stored text is never touched; the screen shows this with a switch back to the words as written. It is the text the Word file is made from."""
    laid = body_for_word(artefact_type, body)
    return laid if laid != body else None


def _renderings(request: Request, store, project_id: str, artefact_type: str, body: str) -> dict[str, str] | None:
    """The pictures for a diagram artefact (S14b), one per look, drawn now from the text shown; None for every other artefact or when there is nothing
    to draw. Never stored, so they cannot be out of step with the text."""
    return render_all(artefact_type, body, names_for(request.app.state.skills.view(store.projects.get(project_id).kind), store, project_id, artefact_type))


class ReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=MAX_REASON_CHARS)


@router.post("/phases/{phase_key}/reopen")
def reopen_phase(project_id: str, phase_key: str, body: ReopenBody, request: Request) -> dict:
    """Take an approved phase back to running. Needs the person's name and a written reason, both recorded. Nothing after this phase is changed:
    a later phase that is under way or approved shows that it stands on a reopened one (the server already reports it as `blocked_by`)."""
    actor = actor_from(request)
    if not _valid(phase_key, KEY):
        raise HTTPException(status_code=422, detail="phase key must be lower-case letters, digits and underscores")
    refuse_if_archived(request, project_id)
    reason = " ".join(body.reason.split())
    if not reason:
        raise HTTPException(status_code=422, detail="say why you are reopening this phase; the reason is recorded with your name")
    try:
        run = request.app.state.engine.reopen(project_id, phase_key, actor=actor, reason=reason)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"phase": run.phase_key, "status": run.status, "decision": run.decision, "decided_by": run.decided_by}


@router.post("/phases/{phase_key}/finish")
def finish_phase(project_id: str, phase_key: str, request: Request) -> dict:
    return _gate(project_id, phase_key, request, "finish")


@router.post("/phases/{phase_key}/back-to-writing")
def back_to_writing(project_id: str, phase_key: str, request: Request) -> dict:
    """Cancel a hand-over: a phase under review goes back to running, so documents can be written, rewritten or edited again. Nothing is deleted or approved."""
    return _gate(project_id, phase_key, request, "back_to_writing")


@router.post("/phases/{phase_key}/approve")
def approve_phase(project_id: str, phase_key: str, request: Request) -> dict:
    return _gate(project_id, phase_key, request, "approve")


def _gate(project_id: str, phase_key: str, request: Request, action: str) -> dict:
    actor = actor_from(request)
    if not _valid(phase_key, KEY):
        raise HTTPException(status_code=422, detail="phase key must be lower-case letters, digits and underscores")
    refuse_if_archived(request, project_id)
    engine, human = request.app.state.engine, request.app.state.human
    try:
        if request.app.state.runner.is_active(project_id, phase_key):
            raise HTTPException(status_code=409, detail=f"{phase_key} is being written right now; wait for the run to end, or cancel it")
        if action == "finish":
            run = engine.finish(project_id, phase_key, actor=actor, require_all=True)       # a person's button: every required document must be written
        elif action == "back_to_writing":
            run = engine.back_to_writing(project_id, phase_key, actor=actor)
        else:
            run = human.approve(project_id, phase_key, actor=actor)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"phase": run.phase_key, "status": run.status, "decision": run.decision, "decided_by": run.decided_by}


@router.get("/runs")
def list_runs(project_id: str, request: Request, phase: str | None = Query(default=None, max_length=64)) -> dict:
    if phase is not None and not _valid(phase, KEY):
        raise HTTPException(status_code=422, detail="phase must be lower-case letters, digits and underscores")
    try:
        _store(request).projects.get(project_id)
        runs = _store(request).runs.list(project_id, phase_key=phase)
    except (NotFoundError, InvalidInput) as exc:
        raise _translate(exc) from exc
    return {"runs": [_run_payload(r) for r in reversed(runs)][:20]}
