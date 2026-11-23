"""The code inside a Build artefact (S17a): reading the `File:` blocks, and the rules every one of them must meet before anything is saved.

A Build artefact is still a Markdown body (so it is versioned, reviewed, traced and replayed like every other artefact). Its `Code` section
holds one block per file:

    File: backend/main.py
    ```python
    ...
    ```

and its `Files` section lists the same files as numbered items. This module only reads and judges text. It writes nothing and runs nothing.
The rules are plain sentences that go back to the model, the same way a missing section does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..agents.items import parse_items
from ..agents.reply import section_bodies
from ..agents.skills import ItemSpec
from ..sandbox.errors import PathRefused
from ..sandbox.workspace import check_relative

MAX_FILES = 12
CODE_SECTION = "Code"

_FILE_LINE = re.compile(r"^\s{0,3}(?:#{1,6}[ \t]+)?(?:[-*][ \t]+)?(?:\*\*|__)?File:[ \t]*(?:\*\*|__)?[ \t]*`?([^`\s*]+)`?(?:\*\*|__)?[ \t]*$")
_OPEN_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})[ \t]*([\w+.#-]*)[ \t]*$")


@dataclass(frozen=True)
class Kind:
    """What one Build artefact may write: a single folder, some file types, and the files it cannot do without."""
    folder: str
    extensions: tuple[str, ...]
    required: tuple[str, ...] = ()
    only: tuple[str, ...] = ()       # when set, these are the only files allowed


KINDS: dict[str, Kind] = {
    "database_schema": Kind("db", (".sql", ".py"), required=("db/schema.sql", "db/init_db.py"), only=("db/schema.sql", "db/init_db.py")),
    "backend_code": Kind("backend", (".py",), required=("backend/main.py",)),
    "frontend_code": Kind("frontend", (".html", ".js", ".css"), required=("frontend/index.html",)),
    "test_suite": Kind("tests", (".py",)),
}


@dataclass(frozen=True)
class CodeFile:
    path: str
    text: str
    line: int          # the line of its `File:` line in the body, 1-based


def is_build_artefact(key: str) -> bool:
    return key in KINDS


def parse_code(body: str) -> tuple[list[CodeFile], list[str]]:
    """(the files in every `Code` section, structural problems). Never raises. A body with no Code section gives ([], []): the missing
    section is already reported by the section check, and saying it twice only crowds out other problems."""
    files: list[CodeFile] = []
    problems: list[str] = []
    for section in section_bodies(body, CODE_SECTION):
        _read_section(section, files, problems)
    return files, problems


def _read_section(section: str, files: list[CodeFile], problems: list[str]) -> None:
    lines = section.split("\n")
    pending: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        named = _FILE_LINE.match(line)
        opened = _OPEN_FENCE.match(line)
        if named is not None:
            if pending is not None:
                problems.append(f"'File: {pending[0]}' is not followed by a code block; put the opening ``` line straight after the File line")
            pending = (named.group(1), index + 1)
            index += 1
            continue
        if opened is not None:
            marker = opened.group(1)
            closing = re.compile(rf"^\s{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            end = index + 1
            while end < len(lines) and not closing.match(lines[end]):
                end += 1
            if pending is None:
                problems.append(f"the code block at line {index + 1} of the Code section has no 'File: <path>' line before it; give every code block a File line")
            elif end >= len(lines):
                problems.append(f"the code block for {pending[0]} is never closed; end it with a line holding only {marker}")
            else:
                files.append(CodeFile(pending[0], "\n".join(lines[index + 1:end]) + "\n", pending[1]))
            pending = None
            index = end + 1
            continue
        if pending is not None and line.strip():
            problems.append(f"'File: {pending[0]}' is not followed by a code block; put the opening ``` line straight after the File line")
            pending = None
        index += 1
    if pending is not None:
        problems.append(f"'File: {pending[0]}' is not followed by a code block; put the opening ``` line straight after the File line")


def listed_paths(body: str, specs: Sequence[ItemSpec]) -> tuple[list[str], list[str]]:
    """(the path each `Files` entry starts with, entries that do not start with one)."""
    paths: list[str] = []
    bad: list[str] = []
    for item in parse_items(body, specs).items:
        words = item.text.replace("`", " ").replace("*", " ").split()
        token = words[0].rstrip(":,;.") if words else ""
        if "/" in token and not token.startswith(("(", "[")):
            paths.append(token)
        else:
            bad.append(item.id)
    return paths, bad


def path_problem(path: str, kind: Kind) -> str | None:
    """Why this path may not be written by this artefact, or None."""
    try:
        check_relative(path)
    except PathRefused as exc:
        return f"the file path '{path}' is not allowed: {exc}"
    if not path.startswith(kind.folder + "/"):
        return f"'{path}' is outside {kind.folder}/; this artefact may only write files under {kind.folder}/"
    if kind.only and path not in kind.only:
        return f"'{path}' is not one of the files this artefact may write: {', '.join(kind.only)}"
    if not path.endswith(kind.extensions):
        return f"'{path}' is a type this artefact cannot write; allowed types: {', '.join(kind.extensions)}"
    return None


def file_problems(skill_key: str, body: str, specs: Sequence[ItemSpec]) -> list[str]:
    """Every problem with the files in a Build artefact's body, as sentences for the model. [] means the files can be written.
    Other artefacts have no files and always give []."""
    kind = KINDS.get(skill_key)
    if kind is None:
        return []
    files, problems = parse_code(body)
    seen: set[str] = set()
    for file in files:
        if file.path in seen:
            problems.append(f"{file.path} is given more than once in Code; write each file once")
            continue
        seen.add(file.path)
        why = path_problem(file.path, kind)
        if why is not None:
            problems.append(why)
        elif not file.text.strip():
            problems.append(f"{file.path} is empty; a file needs content")
        elif "\x00" in file.text:
            problems.append(f"{file.path} contains a NUL byte; a file must be text")
    if len(seen) > MAX_FILES:
        problems.append(f"the artefact has {len(seen)} files; the limit is {MAX_FILES}")
    for needed in kind.required:
        if files and needed not in seen:
            problems.append(f"the required file {needed} is missing from Code")
    listed, unlisted = listed_paths(body, specs)
    for item_id in unlisted:
        problems.append(f"{item_id} does not start with a file path; write it as '{item_id} {kind.folder}/<file>: what it does'")
    if files or listed:
        for path in listed:
            if path not in seen and not any(f.path == path for f in files):
                problems.append(f"Files lists {path}, but Code has no 'File: {path}' block")
        for path in sorted(seen - set(listed)):
            problems.append(f"Code has a 'File: {path}' block, but Files does not list it")
    return problems
