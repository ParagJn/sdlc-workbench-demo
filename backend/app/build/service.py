"""Putting a Build artefact's files into the project's workspace and checking them (S17a).

The artefact body is the source of truth. The workspace is WRITTEN FROM it and is never edited by hand, so writing is repeatable: the same
body gives the same folder. An artefact owns exactly one folder (`db/`, `backend/`, `frontend/` or `tests/`); writing a new version replaces
that folder and nothing else, so one component can never overwrite another's files.

Every file goes through the S16 file tools (path rules, secret scan, size limits). Every command goes through `SandboxService.run_command`,
which audits it. This module adds one audit row for a write and one for a check, holding counts and names, never file contents or output.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from ..sandbox.errors import SandboxError
from ..sandbox.service import SandboxService
from . import checks
from .checks import CheckResult
from .files import KINDS, CodeFile, file_problems, parse_code

STAGE = ".stage"
STALE_STAGE_SECONDS = 3600          # a staging folder this old belongs to a write that crashed; nothing legitimate takes an hour


class BuildRefused(ValueError):
    """The files cannot be written. The message is a plain sentence, and for a body that fails the file rules it lists them."""


@dataclass(frozen=True)
class WriteResult:
    artefact: str
    folder: str
    files: tuple[str, ...]
    replaced: bool                      # an earlier version of this folder existed and was replaced


class BuildService:
    def __init__(self, sandbox: SandboxService, clock: Callable[[], float] = time.time):
        self.sandbox = sandbox
        self._clock = clock

    # ---- writing ---------------------------------------------------------------------------------------------------------------

    def write_artefact(self, project_id: str, skill, body: str, *, actor: str, audit: bool = True) -> WriteResult:
        """Write the files in `body` into the workspace, replacing this artefact's folder. All or nothing: a refusal leaves the old folder whole."""
        kind = KINDS.get(skill.key)
        if kind is None:
            raise BuildRefused(f"'{skill.key}' is not an artefact that holds code")
        problems = file_problems(skill.key, body, skill.items)
        if problems:
            raise BuildRefused("the files cannot be written: " + "; ".join(problems[:5]))
        files, _ = parse_code(body)
        if not files:
            raise BuildRefused("the artefact has no files to write")
        ws = self.sandbox.workspace(project_id)
        self._sweep_stale_staging(ws)
        stage = f"{STAGE}/{skill.key}-{secrets.token_hex(5)}"             # this write's OWN staging folder: writers running at the same moment share nothing
        staged = f"{stage}/{kind.folder}"
        try:
            for file in files:
                ws.write_file(f"{stage}/{file.path}", file.text)
            existed = bool(_files_under(ws, kind.folder))
            ws.replace_folder(staged, kind.folder)
        except SandboxError as exc:
            reason = str(exc).replace(f"{stage}/", "")                    # the staging folder is ours; the model only knows its own paths
            if audit:
                self._audit(project_id, actor, "build.files_refused", skill.key, {"reason": reason[:200]})
            raise BuildRefused(f"the files cannot be written: {reason}") from None
        finally:
            _quietly(ws.remove_folder, stage)      # only OUR folder: the shared `.stage` parent stays, because removing it races with another writer making a folder in it
        paths = tuple(f.path for f in files)
        if audit:
            self._audit(project_id, actor, "build.files_written", skill.key, {"folder": kind.folder, "files": len(paths), "replaced": existed})
        return WriteResult(skill.key, kind.folder, paths, existed)

    def _sweep_stale_staging(self, ws) -> None:
        """Remove staging folders left behind by a write that crashed. Only folders older than an hour: a write in progress is never touched."""
        try:
            root = ws.resolve(STAGE)
            entries = list(os.scandir(root)) if root.is_dir() else []
        except (SandboxError, OSError):
            return
        cutoff = self._clock() - STALE_STAGE_SECONDS
        for entry in entries:
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    _quietly(ws.remove_folder, f"{STAGE}/{entry.name}")
            except OSError:
                continue                                                   # it vanished while we looked: someone else finished with it

    def remove_artefact_files(self, project_id: str, skill_key: str) -> bool:
        """Remove an artefact's folder (an artefact with no saved version has no files). False if there was none."""
        kind = KINDS.get(skill_key)
        if kind is None:
            raise BuildRefused(f"'{skill_key}' is not an artefact that holds code")
        return self.sandbox.workspace(project_id).remove_folder(kind.folder)

    # ---- checking --------------------------------------------------------------------------------------------------------------

    def check_artefact(self, project_id: str, skill_key: str, *, actor: str, design_tables: tuple[str, ...] = ()) -> CheckResult:
        """Run the one real check for this artefact on what is in the workspace now."""
        if skill_key not in KINDS:
            raise BuildRefused(f"'{skill_key}' is not an artefact that holds code")
        ws = self.sandbox.workspace(project_id)
        run: Callable = lambda program, args: self.sandbox.run_command(project_id, program, args, actor=actor)
        if skill_key == "database_schema":
            result = checks.check_database(run, ws, design_tables)
        elif skill_key == "backend_code":
            result = checks.check_backend(run, ws)
        elif skill_key == "frontend_code":
            result = checks.check_frontend(run, ws)
        else:
            result = checks.check_tests(run, ws)
        self._audit(project_id, actor, "build.checked", skill_key, {"status": result.status, "facts": result.facts})
        return result

    def _audit(self, project_id: str, actor: str, action: str, target: str, detail: dict) -> None:
        self.sandbox.store.audit.append(actor=actor, action=action, target_type="build", target_id=target, project_id=project_id, detail=detail)


def _files_under(ws, folder: str) -> list[str]:
    try:
        return ws.list_files(folder)
    except SandboxError:
        return []


def _quietly(call, *args) -> None:
    try:
        call(*args)
    except SandboxError:
        pass
