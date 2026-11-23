"""One real check per Build artefact (S17a). Each is a fact from running something in the sandbox, or from reading the files that were written.

  database  run db/init_db.py (twice), then list the tables the file really holds
  backend   compile every .py file
  frontend  node --check on every .js file; every local reference must point at a file that was written; nothing may reach another machine
  tests     collect the tests (not run: running them is 17b)

A check never changes what the model wrote. A failure is a list of plain sentences that go back to the model, the same way a missing section does.
Compiling is not testing, and the summary says "compiles", not "works".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Sequence

from ..agents.items import parse_items
from ..agents.skills import ItemSpec
from ..sandbox.errors import SandboxError
from ..sandbox.runner import Result
from ..sandbox.workspace import Workspace

TAIL_CHARS = 700
MAX_LISTED = 8
PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"

_EXTERNAL = re.compile(r"(?:^|[\s\"'(=])(?:https?:)?//[A-Za-z0-9]", re.IGNORECASE)
_URL_IN_CODE = re.compile(r"https?://", re.IGNORECASE)
_LOCAL_REFERENCE_ATTRS = {("script", "src"), ("link", "href"), ("img", "src"), ("source", "src"), ("audio", "src"), ("video", "src"), ("iframe", "src")}
_ANY_REFERENCE_ATTRS = {"src", "href", "action", "poster", "data"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str                                   # passed | failed | skipped
    summary: str                                  # one line for a person
    problems: tuple[str, ...] = ()                # sentences for the model, when failed
    facts: dict = field(default_factory=dict)     # counts and names, never contents or output

    @property
    def ok(self) -> bool:
        return self.status != FAILED


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= TAIL_CHARS else "..." + text[-TAIL_CHARS:]


def _could_not_run(name: str, result: Result) -> CheckResult | None:
    """A command the sandbox refused, or could not start, is not a verdict on the code: it is reported as such and never as a pass."""
    if result.status == "unavailable":
        return CheckResult(name, SKIPPED, f"not checked: {result.reason}", facts={"status": "unavailable"})
    if result.status == "refused":
        return CheckResult(name, SKIPPED, f"not checked: the command was refused ({result.reason})", facts={"status": "refused"})
    if result.status == "timed_out":
        return CheckResult(name, FAILED, f"timed out: {result.reason}", (f"{name} did not finish in time ({result.reason}); make it do less work",), {"status": "timed_out"})
    return None


def table_names_in_design(body: str, specs: Sequence[ItemSpec]) -> list[str]:
    """The table each `TBL` item names: its first word, if that is written as a table name (lower-case with underscores, or in backticks).
    An item that starts with an ordinary word ("The invoices table") is left out rather than guessed at: a wrong name would send the model off
    to build a table nobody asked for."""
    names: list[str] = []
    for item in parse_items(body, specs).items:
        if item.prefix != "TBL":
            continue
        text = item.text.lstrip(" :.-")
        ticked = re.match(r"`([A-Za-z_][A-Za-z0-9_]*)`", text)
        plain = re.match(r"([a-z_][a-z0-9_]*)(?=$|[\s:,;.(])", text)
        word = (ticked or plain).group(1) if (ticked or plain) else ""
        if word and word.lower() not in (n.lower() for n in names):
            names.append(word)
    return names


# ---- database --------------------------------------------------------------------------------------------------------------

def check_database(run, ws: Workspace, design_tables: Sequence[str] = ()) -> CheckResult:
    """`run(program, args)` runs a command in the workspace and returns a sandbox Result (the caller audits it)."""
    name = "database"
    first = run("python", ["db/init_db.py"])
    early = _could_not_run(name, first)
    if early:
        return early
    if not first.ok:
        return CheckResult(name, FAILED, "db/init_db.py failed", (f"running db/init_db.py failed (exit {first.exit_code}): {_tail(first.output)}",), {"exit_code": first.exit_code})
    second = run("python", ["db/init_db.py"])
    if not second.ok:
        return CheckResult(name, FAILED, "db/init_db.py fails when run a second time", (f"running db/init_db.py a second time failed (exit {second.exit_code}): {_tail(second.output)}; it must be safe to run again (use CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE)",), {"exit_code": second.exit_code})
    if "db/app.db" not in _files_under(ws, "db"):                  # looking first: opening a missing file with sqlite3 would CREATE it, and the check would then find what it made
        return CheckResult(name, FAILED, "db/init_db.py made no database file", ("db/init_db.py ran but did not leave a readable db/app.db beside it; create app.db in the same folder as the script",), {})
    listing = run("python", ["-m", "sqlite3", "db/app.db", "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"])
    if not listing.ok:
        return CheckResult(name, FAILED, "the database file could not be read", ("db/init_db.py ran but did not leave a readable db/app.db beside it; create app.db in the same folder as the script",), {"exit_code": listing.exit_code})
    created = re.findall(r"\('([^']+)',\)", listing.output)
    problems: list[str] = []
    if not created:
        problems.append("db/init_db.py ran, but db/app.db holds no tables; run the statements in db/schema.sql with executescript")
    lower = {t.lower() for t in created}
    missing = [t for t in design_tables if t.lower() not in lower]
    if missing:
        problems.append(f"the design names the table(s) {', '.join(missing[:MAX_LISTED])}, but db/app.db does not have {'it' if len(missing) == 1 else 'them'}; create every table the design names, spelled the same way")
    facts = {"tables": created, "missing": missing}
    if problems:
        return CheckResult(name, FAILED, f"{len(created)} table(s) created; missing: {', '.join(missing) or 'none'}", tuple(problems), facts)
    return CheckResult(name, PASSED, f"db/init_db.py ran twice and created {len(created)} table(s): {', '.join(created)}", facts=facts)


# ---- backend ---------------------------------------------------------------------------------------------------------------

def check_backend(run, ws: Workspace) -> CheckResult:
    name = "backend"
    result = run("python", ["-m", "compileall", "-q", "backend"])
    if _files_under(ws, "backend"):
        _remove_caches(ws, "backend")
    early = _could_not_run(name, result)
    if early:
        return early
    count = sum(1 for p in _files_under(ws, "backend") if p.endswith(".py"))
    if count == 0:                                                 # compileall is happy with a folder that is not there; a check must never pass on nothing
        return CheckResult(name, FAILED, "there is no backend code", ("there are no Python files under backend/; write backend/main.py",), {"files": 0})
    if not result.ok:
        return CheckResult(name, FAILED, "the code does not compile", (f"the backend code does not compile: {_tail(result.output)}",), {"files": count, "exit_code": result.exit_code})
    return CheckResult(name, PASSED, f"{count} Python file(s) compile (this does not show they work: the tests do that)", facts={"files": count})


def _remove_caches(ws: Workspace, folder: str) -> None:
    """compileall writes .pyc files even when bytecode is switched off. They are not the model's files and must not count against the workspace."""
    folders: set[str] = set()
    for path in ws.list_files(folder):
        parts = path.split("/")
        if "__pycache__" in parts:
            ws.delete_file(path)
            folders.add("/".join(parts[:parts.index("__pycache__") + 1]))
    for directory in sorted(folders, key=len, reverse=True):
        ws.remove_folder(directory)


# ---- frontend --------------------------------------------------------------------------------------------------------------

class _References(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str, str]] = []     # (tag, attribute, value)

    def handle_starttag(self, tag, attrs):
        for attribute, value in attrs:
            if value and attribute in _ANY_REFERENCE_ATTRS:
                self.found.append((tag, attribute, value.strip()))


def _files_under(ws: Workspace, folder: str) -> list[str]:
    try:
        return ws.list_files(folder)
    except SandboxError:
        return []                                   # the folder does not exist: nothing was written


def _resolve(page: str, reference: str) -> str:
    """The workspace path a reference from `page` points at, or '' for one that is not a file (an anchor, a data address)."""
    target = re.split(r"[?#]", reference, maxsplit=1)[0]
    if not target or reference.startswith("data:"):                # "#top" has no path before the "#", and a data: address holds its own content
        return ""
    if target.startswith("/"):
        return "frontend/" + target.lstrip("/")
    base = PurePosixPath(page).parent
    parts: list[str] = []
    for part in (base / target).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def check_frontend(run, ws: Workspace) -> CheckResult:
    name = "frontend"
    files = _files_under(ws, "frontend")
    scripts = [p for p in files if p.endswith(".js")]
    problems: list[str] = []
    skipped_node = False
    for script in scripts:
        result = run("node", ["--check", script])
        if result.status in ("refused", "unavailable") and "not installed" in result.reason:
            skipped_node = True
            break
        early = _could_not_run(name, result)
        if early:
            return early
        if not result.ok:
            problems.append(f"{script} has a syntax error: {_tail(result.output)}")
    known = set(files)
    for page in (p for p in files if p.endswith(".html")):
        try:
            html = ws.read_file(page)
        except SandboxError:
            continue
        parser = _References()
        parser.feed(html)
        for tag, attribute, value in parser.found:
            if _EXTERNAL.search(" " + value) or value.lower().startswith(("http:", "https:", "//")):
                problems.append(f"{page} refers to {value[:80]}, which is on another machine; the page must work offline, so write that file yourself and refer to it by a relative path")
            elif (tag, attribute) in _LOCAL_REFERENCE_ATTRS:
                target = _resolve(page, value)
                if target and target not in known:
                    problems.append(f"{page} refers to {value[:80]}, but no file {target} was written; write that file or fix the path")
    for path in (p for p in files if p.endswith((".js", ".css"))):
        try:
            text = ws.read_file(path)
        except SandboxError:
            continue
        if _URL_IN_CODE.search(text):
            problems.append(f"{path} contains a web address (http:// or https://); nothing may leave this machine, so call the back end by a path such as /items and load nothing from outside")
    facts = {"files": len(files), "scripts": len(scripts), "node_checked": not skipped_node}
    if problems:
        return CheckResult(name, FAILED, f"{len(problems)} problem(s) in {len(files)} file(s)", tuple(problems[:MAX_LISTED]), facts)
    note = "; scripts not syntax-checked because node is not installed" if skipped_node else ""
    return CheckResult(name, PASSED, f"{len(files)} file(s): scripts parse, local references resolve, nothing leaves the machine{note}", facts=facts)


# ---- tests -----------------------------------------------------------------------------------------------------------------

def check_tests(run, ws: Workspace) -> CheckResult:
    name = "tests"
    result = run("pytest", ["--collect-only", "-q"])
    early = _could_not_run(name, result)
    if early:
        return early
    counted = re.search(r"(\d+) tests? collected", result.output)
    errors = re.search(r"(\d+) errors?", result.output)
    count = int(counted.group(1)) if counted else 0
    if (not result.ok and result.exit_code != 5) or errors:        # exit 5 is pytest's "no tests found": its own answer, below
        lines = [l for l in result.output.splitlines() if l.startswith(("E ", "ERROR")) or "Error" in l][:6]
        return CheckResult(name, FAILED, "the tests could not be collected", (f"pytest could not collect the tests (exit {result.exit_code}): {_tail(chr(10).join(lines) or result.output)}",), {"collected": count, "exit_code": result.exit_code})
    if count == 0:
        return CheckResult(name, FAILED, "no tests were found", ("pytest found no tests; write functions named test_... in files named test_....py under tests/",), {"collected": 0})
    return CheckResult(name, PASSED, f"{count} test(s) collected (not run yet)", facts={"collected": count})
