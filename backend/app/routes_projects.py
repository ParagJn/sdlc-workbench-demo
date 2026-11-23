"""Projects over HTTP (S12a): preview what a selection means, create a project, list projects, read one.

The screen holds no business rules. The server says what a selection means (`/api/plan/preview`), what a person may do with each
phase (`allowed_actions`) and what went wrong, in sentences meant to be shown as they are. Changing routes need `X-Actor`
(a name for the audit trail, not a sign-in); reading needs nothing.

Payloads are written out field by field here, not taken from internal objects, so adding a field inside the application can
never change what the screen receives by accident. The shapes are pinned by the contract fixtures (scripts/export_contract.py).
"""
from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .access.roles import ADMIN
from .actor import actor_from
from .llm.recordings import reviews_can_run
from .models import ConflictError, InvalidInput, NotFoundError, Project, MODES
from .purge import remove_project_folder
from .agents.plan_check import check_inputs
from .workflow.plan import Plan, PlanError, build_plan

router = APIRouter(prefix="/api")

MAX_NAME_CHARS = 120
MAX_BRIEF_CHARS = 8000
MAX_PROJECTS_LISTED = 200
CONNECTORS = ("source_control", "issue_tracker", "ci_cd", "cloud_account")
KEY = r"^[a-z][a-z0-9_]{0,63}$"


class Selection(BaseModel):
    """phase key -> the artefact keys switched ON; a phase left out gets everything."""

    model_config = ConfigDict(extra="forbid")

    phases: dict[str, list[str] | None] = Field(default_factory=dict, max_length=20)

    @field_validator("phases")
    @classmethod
    def _keys(cls, value: dict[str, list[str] | None]) -> dict[str, list[str] | None]:
        import re

        for phase, keys in value.items():
            if not re.fullmatch(KEY, phase):
                raise ValueError(f"phase key {phase!r} must be lower-case letters, digits and underscores")
            for key in keys or ():
                if not re.fullmatch(KEY, key):
                    raise ValueError(f"artefact key {key!r} must be lower-case letters, digits and underscores")
            if keys is not None and len(keys) > 60:
                raise ValueError("too many artefacts listed for one phase")
        return value


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: dict[str, list[str] | None] = Field(default_factory=dict, max_length=20)
    kind: Literal["software", "automation"] = "software"           # S40: what is required depends on the kind of project


class CreateProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    brief: str = Field(default="", max_length=MAX_BRIEF_CHARS)
    kind: Literal["software", "automation"] = "software"      # S40: chosen once, here, and never changed
    selection: dict[str, list[str] | None] = Field(default_factory=dict, max_length=20)
    connectors: list[str] = Field(default_factory=list, max_length=len(CONNECTORS))

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("the project needs a name")
        return value

    @field_validator("connectors")
    @classmethod
    def _connectors(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(CONNECTORS))
        if unknown:
            raise ValueError(f"unknown connector(s) {unknown}; the connectors are {list(CONNECTORS)}")
        return list(dict.fromkeys(value))


class DeleteItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=MAX_NAME_CHARS)


class DeleteProjects(BaseModel):
    """Delete archived projects for good (S37). `agree` is the person's answer to "You agree to delete, this is unrecoverable"; each item carries the project's exact name."""

    model_config = ConfigDict(extra="forbid")

    items: list[DeleteItem] = Field(min_length=1, max_length=MAX_PROJECTS_LISTED)
    agree: StrictBool = False


def _store(request: Request):
    return request.app.state.store


def _engine(request: Request):
    return request.app.state.engine


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


# ---- payloads, written out field by field ---------------------------------------------------------------------------------

def plan_payload(plan: Plan) -> dict[str, Any]:
    return {
        "phases": [
            {
                "key": p.key, "order": p.phase.order, "name": p.phase.name, "summary": p.phase.summary, "approver": p.phase.approver,
                "artefacts": [
                    {"key": a.key, "name": a.name, "required": a.required, "description": a.description, "waits_for": list(a.after),
                     "wave": next(n for n, wave in enumerate(p.waves, start=1) if any(x.key == a.key for x in wave)),
                     "added_because_needed": a.key in p.pulled_in}
                    for a in p.artefacts
                ],
                "waves": [[a.key for a in wave] for wave in p.waves],
                "not_enabled": [{"key": a.key, "name": a.name, "reason": a.disabled_reason} for a in p.not_enabled],
                "by_hand": [{"key": a.key, "name": a.name, "description": a.description} for a in p.by_hand],
            }
            for p in plan.phases
        ],
        "notes": list(plan.notes),
    }


def catalogue_payload(request: Request) -> dict[str, Any]:
    """Every phase and artefact that exists, with nothing switched off: what the wizard offers to tick."""
    catalogue = request.app.state.engine.catalogue
    return {
        "phases": [
            {"key": p.key, "order": p.order, "name": p.name, "summary": p.summary, "approver": p.approver,
             "artefacts": [{"key": a.key, "name": a.name, "required": a.required, "description": a.description, "waits_for": list(a.after),
                            "enabled": a.enabled, "disabled_reason": a.disabled_reason, "by_hand": a.by_hand, "optional_for": list(a.optional_for)}
                           for a in p.artefacts]}
            for p in catalogue.phases
        ]
    }


def project_summary(project: Project, snapshot) -> dict[str, Any]:
    done = sum(1 for p in snapshot.phases if p.status == "approved")
    active = next((p for p in snapshot.phases if p.status in ("running", "awaiting_review", "rejected")), None)
    return {
        "id": project.id, "name": project.name, "mode": project.mode, "kind": project.kind, "created_at": project.created_at, "archived_at": project.archived_at,
        "phases_total": sum(1 for p in snapshot.phases if p.artefacts), "phases_approved": done,
        "active_phase": None if active is None else {"key": active.key, "name": active.name, "status": active.status},
        "next_action": snapshot.next_action,
        # 2026-11-22: each phase at a glance, for the strip on a project's card in the list: where it stands and how many of its documents are written. A phase with nothing in the plan is left out.
        "phases": [
            {"key": p.key, "name": p.name, "status": p.status, "written": sum(1 for a in p.artefacts if a.has_content), "total": len(p.artefacts), "locked": bool(p.blocked_by) and p.status == "not_started"}
            for p in snapshot.phases if p.artefacts
        ],
    }


def connectors_of(store, project_id: str) -> list[str]:
    """The simulated connectors ticked in the wizard (the latest note), or none."""
    notes = store.audit.list(project_id, action="project.connectors_noted")
    return list(notes[-1].detail.get("connectors", [])) if notes else []


def reads_of(request: Request, project: Project) -> dict[str, list[str]]:
    """What each artefact's skill needs as REQUIRED input, by artefact key: the real list, which the run's own group check uses. The plan's `waits_for` is only an ordering and misses
    some of it (the epics read the personas, though the plan orders them after the requirements). Sent so the screen's quick warning can say the same as the server."""
    skills = request.app.state.skills
    out: dict[str, list[str]] = {}
    for key in {a.key for phase in _engine(request).catalogue.phases for a in phase.artefacts}:
        skill = skills.get(key, project.kind)
        out[key] = [u for u in (skill.upstream if skill else ()) if u not in skill.optional_upstream]
    return out


def project_detail(project: Project, snapshot, connectors: list[str], reads: dict[str, list[str]] | None = None) -> dict[str, Any]:
    return {
        "id": project.id, "name": project.name, "brief": project.brief, "mode": project.mode, "kind": project.kind, "created_at": project.created_at,
        "updated_at": project.updated_at, "archived_at": project.archived_at, "connectors": connectors, "connectors_are_simulated": True,
        "next_action": snapshot.next_action, "notes": list(snapshot.notes),
        "phases": [
            {
                "key": p.key, "order": p.order, "name": p.name, "approver": p.approver, "status": p.status, "decision": p.decision,
                "decided_by": p.decided_by, "feedback": p.feedback, "needs_review": p.needs_review, "blocked_by": p.blocked_by,
                "allowed_actions": list(p.allowed_actions),
                "artefacts": [{"key": a.key, "name": a.name, "required": a.required, "has_content": a.has_content, "wave": a.wave,
                               "waits_for": list(a.waits_for), "reads": list((reads or {}).get(a.key, ()))} for a in p.artefacts],
                "not_enabled": [{"key": a.key, "name": a.name, "reason": a.reason} for a in p.not_enabled],
                "by_hand": [{"key": a.key, "name": a.name, "description": a.description} for a in p.by_hand],
            }
            for p in snapshot.phases
        ],
    }


# ---- routes ----------------------------------------------------------------------------------------------------------------------

@router.get("/catalogue")
def get_catalogue(request: Request) -> dict:
    return catalogue_payload(request)


@router.get("/modes")
def get_mode(request: Request) -> dict:
    """Mode is a fact about this server, not a choice (S12 decision 6)."""
    mode = request.app.state.settings.app_mode
    return {
        "mode": mode,
        "launch": os.environ.get("SDLC_LAUNCH_ID", ""),          # set by start.sh, one per run of the script; empty when the backend is started any other way (the screen then never asks again)
        "reviews_available": reviews_can_run(request.app.state.settings, getattr(request.app.state, "recordings", None)),
        "meaning": ("This server answers from recordings: free, offline and repeatable." if mode == "replay"
                    else "This server calls real models through the gateway: it costs money and takes time."),
    }


@router.post("/plan/preview")
def preview_plan(body: PreviewRequest, request: Request) -> dict:
    try:
        plan = build_plan(_engine(request).catalogue, body.selection, body.kind)
        check_inputs(plan, request.app.state.skills, body.kind)
        return plan_payload(plan)
    except PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/projects", status_code=201)
def create_project(body: CreateProject, request: Request) -> dict:
    actor = actor_from(request)
    engine, store = _engine(request), _store(request)
    try:
        check_inputs(build_plan(engine.catalogue, body.selection, body.kind), request.app.state.skills, body.kind)   # refuse a bad selection before anything is created, including a writer whose input is not ticked
        project = store.projects.create(
            name=body.name, brief=body.brief, mode=request.app.state.settings.app_mode, selection=dict(body.selection), actor=actor, kind=body.kind,
        )
        engine.open_project(project.id)
        if body.connectors:
            # Connectors are SIMULATED: ticking one is recorded in the audit trail (structured, append-only) and changes no behaviour.
            store.audit.append(
                actor=actor, action="project.connectors_noted", target_type="project", target_id=project.id, project_id=project.id,
                detail={"connectors": body.connectors, "simulated": True},
            )
        return project_detail(project, engine.snapshot(project.id), connectors_of(store, project.id), reads_of(request, project))
    except (PlanError, InvalidInput) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects")
def list_projects(request: Request, archived: bool = False) -> dict:
    store, engine = _store(request), _engine(request)
    projects = list(reversed(store.projects.list(archived=archived)))[:MAX_PROJECTS_LISTED]
    return {"projects": [project_summary(p, engine.snapshot(p.id)) for p in projects]}


@router.get("/projects/{project_id}")
def get_project(project_id: str, request: Request) -> dict:
    engine = _engine(request)
    try:
        project = _store(request).projects.get(project_id)
        return project_detail(project, engine.snapshot(project_id), connectors_of(_store(request), project_id), reads_of(request, project))
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc


@router.post("/projects/{project_id}/archive")
def archive_project(project_id: str, request: Request) -> dict:
    """Hide a project and make it read-only. Refused while a run is going on it, because that run could still be spending money."""
    actor = actor_from(request)
    runner = request.app.state.runner
    try:
        store = _store(request)
        store.projects.get(project_id)
        for phase_key in _engine(request).catalogue.phase_keys:
            if runner.is_active(project_id, phase_key):
                raise HTTPException(status_code=409, detail=f"{phase_key} is being written right now; wait for the run to end, or cancel it, before archiving")
        project = store.projects.archive(project_id, actor=actor)
        return project_detail(project, _engine(request).snapshot(project_id), connectors_of(store, project_id), reads_of(request, project))
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc


@router.post("/projects/{project_id}/restore")
def restore_project(project_id: str, request: Request) -> dict:
    actor = actor_from(request)
    try:
        store = _store(request)
        project = store.projects.restore(project_id, actor=actor)
        return project_detail(project, _engine(request).snapshot(project_id), connectors_of(store, project_id), reads_of(request, project))
    except (NotFoundError, InvalidInput) as exc:
        raise _fail(exc) from exc


@router.post("/projects/delete")
def delete_projects(body: DeleteProjects, request: Request) -> dict:
    """Delete archived projects and ALL their data, for good (S37). Only an active Administrator (even when the Users list is empty), only with `agree: true`.

    Each project is its own all-or-nothing step, so one that cannot be deleted (not archived, wrong name, a run going) is reported and the others still go. The reply says,
    per project, what happened in a sentence meant to be shown as it is. A project's folders are removed after its rows."""
    actor = actor_from(request)
    store, runner, cfg = _store(request), request.app.state.runner, request.app.state.settings
    me = store.users.find(actor.removeprefix("human:"))
    if me is None or not me.active or ADMIN not in me.roles:
        raise HTTPException(status_code=403, detail=f"only an active {ADMIN} can delete projects; ask one of them")
    if body.agree is not True:
        raise HTTPException(status_code=422, detail="deleting is unrecoverable, so it needs agree: true")
    results: list[dict[str, Any]] = []
    for item in list(dict.fromkeys((i.id, i.name) for i in body.items)):
        project_id, name = item
        try:
            project = store.projects.get(project_id)
            if project.archived_at is None:
                raise ConflictError("this project is not archived; archive it first")
            busy = next((k for k in _engine(request).catalogue.phase_keys if runner.is_active(project_id, k)), None)
            if busy is not None:
                raise ConflictError(f"{busy} is being written right now; wait for the run to end before deleting")
            counts = store.projects.purge(project_id, name=name, actor=actor)
        except (NotFoundError, InvalidInput, ConflictError) as exc:
            results.append({"id": project_id, "name": name, "deleted": False, "reason": "this project no longer exists" if isinstance(exc, NotFoundError) else str(exc), "rows": 0, "folder_problems": []})
            continue
        problems = [f"{label}: {outcome}" for label, root in (("workspaces", cfg.workspaces_path), ("Templates", cfg.templates_path))
                    if (outcome := remove_project_folder(root, project_id)).startswith("failed")]
        results.append({"id": project_id, "name": name, "deleted": True, "reason": "", "rows": sum(counts.values()), "folder_problems": problems})
    return {"results": results, "deleted": sum(1 for r in results if r["deleted"]), "failed": sum(1 for r in results if not r["deleted"])}
