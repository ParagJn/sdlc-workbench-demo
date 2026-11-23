"""The 17a checks inside the runtime's own repair loop, and the rule that the workspace equals the latest saved version (S17b-1).

A Build artefact is only worth saving if its files can be written and pass their one real check. `make_extra_check` turns that into the optional
`extra_check` argument of `AgentRuntime.run`: it is called on a reply that already passed every ordinary check, writes the files, runs the check, and
returns the problems in plain sentences. The runtime sends them back in the SAME conversation, so a compile error or a schema typo is repaired while
the model still has the whole task in view, and a version that fails is never saved.

Trying a reply means writing it, so the workspace can end up holding an attempt that was never saved. `sync_workspace` puts every component's folder
back to its latest SAVED version (or removes it if there is none). It is called at the end of every loop, whatever happened.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Sequence

from ..agents.skills import SkillLibrary
from .checks import table_names_in_design
from .files import KINDS
from .service import BuildRefused, BuildService

DESIGN = "low_level_design"
# What collecting the generated tests imports: the backend, which the test suite is guaranteed to wait for, so it is saved and nothing is writing it. NOT the database or
# the frontend: in wave 1 they are written at the same moment, have no saved version yet, and putting them 'back' would delete the folder they are being tested in.
TESTED_AGAINST = ("backend_code",)


def latest_body(store, project_id: str, skill) -> str | None:
    """The text of the newest saved version of a Build artefact, or None if it has never been saved."""
    found = store.artefacts.list(project_id, phase_key=skill.artefact.phase_key, artefact_type=skill.key)
    if not found or found[0].latest_version is None:
        return None
    return store.artefacts.get_version(project_id, found[0].id).body


def design_tables_for(store, library: SkillLibrary, project_id: str) -> tuple[str, ...]:
    """The tables the saved low-level design names, or none if it has not been written."""
    skill = library.get(DESIGN)
    if skill is None:
        return ()
    body = latest_body(store, project_id, skill)
    return tuple(table_names_in_design(body or "", skill.items))


def sync_workspace(
    build: BuildService, store, library: SkillLibrary, project_id: str, *, actor: str, keys: Sequence[str] | None = None,
) -> dict[str, str]:
    """Make each component's folder hold exactly its latest saved version. {artefact: "written" | "removed" | "none" | "refused"}.
    `keys` limits it to some components (None means all): a check that only needs the backend must not touch a component that is being written right now."""
    done: dict[str, str] = {}
    for key in KINDS:
        skill = library.get(key)
        if skill is None or (keys is not None and key not in keys):
            continue
        body = latest_body(store, project_id, skill)
        if body is None:
            done[key] = "removed" if build.remove_artefact_files(project_id, key) else "none"
            continue
        try:
            build.write_artefact(project_id, skill, body, actor=actor, audit=False)
            done[key] = "written"
        except BuildRefused:
            done[key] = "refused"                  # a saved version that no longer passes the rules: left for the report, never hidden
    return done


def make_extra_check(build: BuildService, store, library: SkillLibrary, project_id: str, skill, *, actor: str) -> Callable[[str], Awaitable[list[str]]]:
    """The `extra_check` for one Build artefact. Sandbox work is blocking, so it runs in a thread."""

    def run(body: str) -> list[str]:
        if skill.key == "test_suite":                                   # collecting the tests imports the backend: it must be the saved one
            sync_workspace(build, store, library, project_id, actor=actor, keys=TESTED_AGAINST)
        try:
            build.write_artefact(project_id, skill, body, actor=actor)
        except BuildRefused as exc:
            return [str(exc)]
        design = design_tables_for(store, library, project_id)           # only the database check uses it
        result = build.check_artefact(project_id, skill.key, actor=actor, design_tables=design)
        return list(result.problems)                  # only a failed check has problems; "skipped" (no sandbox) is not the model's fault and the loop reports it as blocked

    async def check(body: str) -> list[str]:
        return await asyncio.to_thread(run, body)

    return check
