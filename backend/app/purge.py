"""Removing a deleted project's folders from disk (S37).

A project keeps files in two places beside the database: `workspaces/<id>` (generated code) and `Templates/<id>` (Word templates). Both are named by the project's ID, which is
checked against a strict pattern, so an ID can never point outside the folder it is looked up in. A symbolic link is unlinked, never followed, so nothing outside the data
folder can be deleted through one. A failure is reported, never raised: by then the database rows are already gone, and the person is told which folder to remove by hand.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def remove_project_folder(base: Path, project_id: str) -> str:
    """Remove `base/<project_id>`. Returns 'removed', 'absent', or 'failed: <reason>'."""
    if not PROJECT_ID.fullmatch(project_id or ""):
        return "failed: not a project ID that can name a folder"
    target = Path(base) / project_id
    try:
        if target.is_symlink():
            target.unlink()
            return "removed"
        if not target.exists():
            return "absent"
        if not target.is_dir():
            target.unlink()
            return "removed"
        shutil.rmtree(target)
        return "failed: still there" if target.exists() else "removed"
    except OSError as exc:
        return f"failed: {exc.strerror or type(exc).__name__}"
