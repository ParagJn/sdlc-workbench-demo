"""Documents, search and the sample project over HTTP (S12b).

Same rules as the project routes: payloads are written out field by field, errors are plain sentences, a request that changes
something needs `X-Actor` (a name for the audit trail, not a sign-in), reading needs nothing. Document text leaves the server in
exactly two places: search hits and cited sources. Both are the same text the agents are shown.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Path, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from .actor import actor_from
from .archive import refuse_if_archived
from .context.types import DocumentInfo, check_category
from .questions.check import wrong_stage_problem
from .models import InvalidInput, NotFoundError
from .demo import DemoUnavailable, prepare as prepare_demo, recorded_phases
from .review.errors import NotAnApprover
from .sample import BRIEF, FILENAME, NAME, POLICY

router = APIRouter(prefix="/api")

MAX_QUERY_CHARS = 200
MAX_HIT_CHARS = 900
MAX_SEARCH_LIMIT = 20


def _store(request: Request):
    return request.app.state.store


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def document_payload(doc: DocumentInfo, *, with_flags: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "number": doc.number, "source_id": doc.source_prefix, "filename": doc.filename, "kind": doc.kind, "size_bytes": doc.size_bytes,
        "chars": doc.chars, "chunk_count": doc.chunk_count, "added_by": doc.added_by, "created_at": doc.created_at,
        "category": doc.category, "stage": doc.stage,
    }
    if with_flags:   # only the result of an upload carries these: they are computed then and deliberately not stored
        out["already_added"] = doc.already_added
        out["flags"] = [f.as_dict() for f in doc.findings]
    return out


def hit_payload(hit) -> dict[str, Any]:
    text = hit.text if len(hit.text) <= MAX_HIT_CHARS else hit.text[:MAX_HIT_CHARS].rstrip() + "…"
    return {
        "source_id": hit.source_id, "kind": hit.kind, "title": hit.title, "heading_path": hit.heading_path, "text": text,
        "page": hit.page, "citation": hit.citation, "phase_key": hit.phase_key, "artefact_id": hit.artefact_id,
        "relevance": None if hit.relevance is None else round(hit.relevance, 3),
        "coverage": None if hit.coverage is None else round(hit.coverage, 3),
    }


@router.get("/sample")
def get_sample() -> dict:
    """The exact name, brief and policy note the recordings were made with. The screen fills the wizard from this."""
    return {"name": NAME, "brief": BRIEF, "document": {"filename": FILENAME, "text": POLICY.decode("utf-8")}}


class DemoStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    through: str = Field(min_length=1, max_length=40)


def _demo_phases(request: Request) -> list[str]:
    return recorded_phases(request.app.state.settings)


@router.get("/demo")
def get_demo(request: Request) -> dict:
    """Can this server start the prepared demo, and through which phases is it recorded? Reading needs no name."""
    phases = _demo_phases(request)
    names = {p.key: p.name for p in request.app.state.engine.catalogue.phases} if phases else {}
    return {"available": bool(phases), "phases": [{"key": k, "name": names.get(k, k)} for k in phases]}


@router.post("/demo/start", status_code=201)
async def start_demo(body: DemoStart, request: Request) -> dict:
    """Create the sample project and play the recorded script up to `through`, as the person who asked, and leave that phase handed to review for them to approve.
    The same steps, taken through the same parts the screen's buttons use, so the audit trail is true. Needs a name (X-Actor)."""
    actor = actor_from(request)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", body.through):
        raise HTTPException(status_code=422, detail="the phase key has unexpected characters")
    try:
        project_id = await prepare_demo(request.app.state, actor, body.through)
    except DemoUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotAnApprover as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"project_id": project_id, "phase": body.through}


@router.get("/projects/{project_id}/documents")
def list_documents(project_id: str, request: Request) -> dict:
    try:
        return {"documents": [document_payload(d) for d in _store(request).context.list_documents(project_id)]}
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc


@router.post("/projects/{project_id}/documents", status_code=201)
async def add_document(project_id: str, request: Request, file: UploadFile = File(...), category: str = Form(default="reference", max_length=20), stage: str | None = Form(default=None, max_length=64)) -> dict:
    """Add a document. `category` says what it is for (S28): a reference document (the default, exactly as before), responses to a stage's open questions, or confirmations of the
    charter's constraints and assumptions; `stage` is the phase the last two are for."""
    actor = actor_from(request)
    store = _store(request)
    try:
        store.projects.get(project_id)
        refuse_if_archived(request, project_id)
    except NotFoundError as exc:
        raise _fail(exc) from exc
    name = (file.filename or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="the file has no name; a name is needed to tell what kind of file it is")
    limit = store.context.limits.max_file_bytes
    data = await file.read(limit + 1)    # one byte over the limit is enough to refuse it, without reading all of a huge upload
    if len(data) > limit:
        raise HTTPException(status_code=422, detail=f"this file is larger than the {limit // (1024 * 1024)} MB limit")
    try:
        category, stage = check_category(category, stage)
        if stage is not None and not request.app.state.engine.catalogue.has_phase(stage):
            raise InvalidInput(f"there is no stage called {stage!r}")
        problem = wrong_stage_problem(request, project_id, data, name, category, stage)
        if problem:
            raise InvalidInput(problem)
        doc = await asyncio.to_thread(store.context.add_document, project_id, name, data, actor=actor, category=category, stage=stage)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return document_payload(doc, with_flags=True)


@router.delete("/projects/{project_id}/documents/{number}")
def remove_document(project_id: str, request: Request, number: int = Path(ge=1, le=1_000_000)) -> dict:
    actor = actor_from(request)
    refuse_if_archived(request, project_id)
    try:
        _store(request).context.remove_document(project_id, number, actor=actor)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"removed": number}


@router.get("/projects/{project_id}/search")
def search(
    project_id: str, request: Request, q: str = Query(default="", max_length=MAX_QUERY_CHARS * 2),
    limit: int = Query(default=8, ge=1, le=MAX_SEARCH_LIMIT),
) -> dict:
    query = " ".join(q.split())
    if not query:
        raise HTTPException(status_code=422, detail="type a word or a question to search for")
    if len(query) > MAX_QUERY_CHARS:
        raise HTTPException(status_code=422, detail=f"keep the search to {MAX_QUERY_CHARS} characters or fewer")
    try:
        hits = _store(request).context.search(project_id, query, limit=limit)
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc
    return {"query": query, "hits": [hit_payload(h) for h in hits]}
