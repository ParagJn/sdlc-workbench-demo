"""Open questions as a file (S27): what is open in a phase, and the Markdown file to answer them in. Read-only.

Anyone who can open the project can read it (no name or role is needed), and an archived project can be read. The answers come back through the ordinary document upload;
nothing here writes. Only what the writer already put under the headings `Open questions` (and, for Setup, `Constraints and assumptions`) is ever in a file.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .actor import actor_from
from .archive import refuse_if_archived
from .models import InvalidInput, NotFoundError
from .questions import collect as C
from .questions import service as S

router = APIRouter(prefix="/api/projects/{project_id}/phases/{phase_key}/questions")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MD_TYPE = "text/markdown; charset=utf-8"


def _state(request: Request, project_id: str, phase_key: str) -> tuple[Any, Any, S.State]:
    if not KEY.fullmatch(phase_key):
        raise HTTPException(status_code=422, detail="phase key must be lower-case letters, digits and underscores")
    store, engine = request.app.state.store, request.app.state.engine
    try:
        project = store.projects.get(project_id)
        phase = engine.catalogue.phase(phase_key) if engine.catalogue.has_phase(phase_key) else None
        if phase is None:
            raise NotFoundError(f"unknown phase {phase_key!r}")
        return project, phase, S.state(store, project_id, phase_key, [a.key for a in phase.artefacts])
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _cut_note(request: Request, project_id: str, phase_key: str, phase: Any) -> str | None:
    """A sentence when the free-form responses are longer than a rewrite will be shown (S28), so the person is told before they press the button, not after; else None."""
    order = [a.key for a in phase.artefacts]
    return S.responses_for_prompt(request.app.state.store, project_id, phase_key, order)[1]


def _rewrite_view(request: Request, project_id: str, phase_key: str, st: S.State) -> dict[str, Any]:
    """What the Rewrite button would do right now, worked out here so the screen only shows it (S28)."""
    store = request.app.state.store
    try:
        status = store.phases.get(project_id, phase_key).status
    except NotFoundError:
        status = "not_started"
    action, why, targets = S.rewrite_plan(status, st)
    return {"action": action, "why": why, "documents": targets}


def _items(items: list[C.Item]) -> list[dict[str, str]]:
    return [{"id": i.id, "artefact_id": i.artefact_id, "title": i.title, "text": i.text} for i in items]


@router.get("")
def questions(project_id: str, phase_key: str, request: Request) -> dict[str, Any]:
    project, phase, st = _state(request, project_id, phase_key)
    has_constraints = phase_key in C.CONSTRAINT_PHASES
    return {
        "phase": phase_key, "stage": phase.name,
        "questions": {"total": len(st.collected.questions), "open": len(st.open_questions()), "items": _items(st.open_questions()), "resolved": _items(st.resolved_items(C.KIND_QUESTIONS)), "file": C.file_name(phase_key, C.KIND_QUESTIONS)},
        "constraints": {"applies": has_constraints, "total": len(st.collected.constraints), "open": len(st.open_constraints()), "items": _items(st.open_constraints()), "resolved": _items(st.resolved_items(C.KIND_CONSTRAINTS)),
                        "file": C.file_name(phase_key, C.KIND_CONSTRAINTS) if has_constraints else None},
        "answers": [{"document": f.source_id, "filename": f.filename, "kind": f.kind, "answered": len(f.answered)} for f in st.files],
        "has_answers_file": st.has_answers_file(),
        "numbering_warnings": st.numbering_warnings(),
        "responses_usable": st.has_usable_responses(),
        "responses_cut": _cut_note(request, project_id, phase_key, phase),
        "rewrite": _rewrite_view(request, project_id, phase_key, st),
        "note": "Answers come back as a document: upload the file with your answers filled in, choosing 'Responses to open questions' for this stage, then press Rewrite.",
    }


@router.get("/download")
def download(project_id: str, phase_key: str, request: Request, kind: str = Query(default=C.KIND_QUESTIONS)) -> Response:
    if kind not in (C.KIND_QUESTIONS, C.KIND_CONSTRAINTS):
        raise HTTPException(status_code=422, detail="kind must be 'questions' or 'constraints'")
    if kind == C.KIND_CONSTRAINTS and phase_key not in C.CONSTRAINT_PHASES:
        raise HTTPException(status_code=404, detail="only the project charter, in Setup, states constraints and assumptions")
    project, phase, st = _state(request, project_id, phase_key)
    marked = st.resolved_questions if kind == C.KIND_QUESTIONS else st.resolved_constraints
    items = st.collected.questions if kind == C.KIND_QUESTIONS else st.collected.constraints
    skip = sorted(st.answered_ids(kind) | {i.id for i in items if C.text_key(i.text) in marked})      # answered, or marked resolved by a person: neither is still open
    text = C.render(st.collected, kind, stage_name=phase.name, project_name=project.name, short_id=project.id, answered=skip)
    name = C.file_name(phase_key, kind)
    return Response(text, media_type=MD_TYPE, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- resolved marks (2026-11-19) ---------------------------------------------------------------------------------------------------------------------
# A person marks an open question resolved (it stops counting and leaves the download) or open again. Tied to the question's words; the decision and the person are recorded.
# Nothing in any document changes. Only a named person can do it, and an archived project cannot be changed.

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default=C.KIND_QUESTIONS, max_length=20)
    question: str = Field(min_length=1, max_length=1000)           # the question as it reads now: the mark is tied to these words
    state: str = Field(default="resolved", max_length=20)


@router.post("/mark")
def mark_question(project_id: str, phase_key: str, body: MarkBody, request: Request) -> dict[str, Any]:
    actor = actor_from(request)
    if not KEY.fullmatch(phase_key):
        raise HTTPException(status_code=422, detail="phase key must be lower-case letters, digits and underscores")
    refuse_if_archived(request, project_id)
    if body.kind not in (C.KIND_QUESTIONS, C.KIND_CONSTRAINTS) or body.state not in ("resolved", "open"):
        raise HTTPException(status_code=422, detail="kind is 'questions' or 'constraints'; state is 'resolved' or 'open'")
    if body.kind == C.KIND_CONSTRAINTS and phase_key not in C.CONSTRAINT_PHASES:
        raise HTTPException(status_code=404, detail="only the project charter, in Setup, states constraints and assumptions")
    _, _, st = _state(request, project_id, phase_key)
    key = C.text_key(body.question)
    known = {C.text_key(i.text): i for i in (st.collected.questions if body.kind == C.KIND_QUESTIONS else st.collected.constraints)}
    if key not in known:
        raise HTTPException(status_code=404, detail="that question is not in this stage's documents now; reload the page")
    try:
        request.app.state.store.question_marks.mark(project_id, phase_key, body.kind, key, known[key].text, body.state, actor=actor)
    except InvalidInput as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _, _, after = _state(request, project_id, phase_key)
    return {"phase": phase_key, "kind": body.kind, "state": body.state, "open": len(after.open_questions() if body.kind == C.KIND_QUESTIONS else after.open_constraints())}


# ---- the one-button rewrite (S28) --------------------------------------------------------------------------------------------------------------------
# The only WRITE route of this module. It does nothing new of its own: it calls the existing "send a phase back" (when the phase is awaiting review) and then starts the existing run
# with exactly the documents the responses bear on. So the approval rule, the audit, the money caps, the events and the cancel all apply as they do for any rewrite.

from .review.errors import ReviewError  # noqa: E402
from .run.errors import RunAlreadyActive  # noqa: E402


@router.post("/rewrite", status_code=202)
async def rewrite_with_responses(project_id: str, phase_key: str, request: Request) -> dict[str, Any]:
    actor = actor_from(request)
    if not KEY.fullmatch(phase_key):
        raise HTTPException(status_code=422, detail="phase key must be lower-case letters, digits and underscores")
    refuse_if_archived(request, project_id)
    app = request.app
    try:
        project, phase, st = _state(request, project_id, phase_key)
        status = app.state.store.phases.get(project_id, phase_key).status if _has_phase_row(app, project_id, phase_key) else "not_started"
        action, why, targets = S.rewrite_plan(status, st)
        if action == S.REWRITE_REFUSED:
            raise HTTPException(status_code=409, detail=why)
        if app.state.runner.is_active(project_id, phase_key):
            raise HTTPException(status_code=409, detail=f"{phase_key} is being written right now; wait for the run to end, or cancel it")
        if action == S.REWRITE_AFTER_SEND_BACK:
            note = S.send_back_note(st)
            app.state.human.request_changes(project_id, phase_key, actor=actor, notes={artefact_id: note for artefact_id in targets})
        types = {a.id: a.type for a in app.state.store.artefacts.list(project_id, phase_key=phase_key)}
        order = [a.key for a in phase.artefacts]
        wanted = {types[i] for i in targets if i in types}
        only = [k for k in order if k in wanted]
        if not only:
            raise HTTPException(status_code=409, detail="none of the documents to rewrite could be found")
        run_id = app.state.runner.start_phase(project_id, phase_key, only=only)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunAlreadyActive as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ReviewError, InvalidInput) as exc:
        code = 403 if type(exc).__name__ == "NotAnApprover" else 409 if type(exc).__name__ in ("WrongPhaseState", "PrerequisiteNotMet") else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    app.state.store.audit.append(
        actor=actor, action="run.requested", target_type="run", target_id=run_id, project_id=project_id,
        detail={"phase": phase_key, "only": only, "rewrite_all": False, "review": False, "rewrite_with_responses": True, "sent_back": action == S.REWRITE_AFTER_SEND_BACK},
    )
    return {"run_id": run_id, "status": "running", "sent_back": action == S.REWRITE_AFTER_SEND_BACK, "only": only,
            "events": f"/api/projects/{project_id}/events", "state": f"/api/projects/{project_id}/runs/{run_id}"}


def _has_phase_row(app: Any, project_id: str, phase_key: str) -> bool:
    try:
        app.state.store.phases.get(project_id, phase_key)
        return True
    except NotFoundError:
        return False
