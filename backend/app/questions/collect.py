"""Collect a phase's open questions, write them as a Markdown file, and read the answers back (S27). Pure: no database, no model, no clock.

What is collected is only what the writer already wrote under the headings `Open questions` (any phase) and `Constraints and assumptions` (the Setup charter). Nothing else from a document is ever put in the file.

The file's FIRST LINE names the stage and the project, and that line, not the file name, is how a returned file is matched to its stage. The answers are read back from `**Answer:**` lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..agents.reply import section_bodies
from ..agents.skills import normalise_heading

QUESTIONS_HEADING = "Open questions"
CONSTRAINTS_HEADING = "Constraints and assumptions"
CONSTRAINT_PHASES = ("setup",)                                    # only the project charter writes this section
KIND_QUESTIONS, KIND_CONSTRAINTS = "questions", "constraints"
ANSWER_LABEL = "**Answer:**"
CONFIRM_LABEL = "**Confirmed or corrected:**"
MAX_ITEMS = 200                                                   # a bound on one file; a writer is told to keep these short
MAX_ITEM_CHARS = 1000
MAX_ANSWER_CHARS = 4000

_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+(.+)$")
_NONE = re.compile(r"^(none|n/?a|nothing|no open questions?|no constraints?)\.?$", re.IGNORECASE)
_LABEL = re.compile(r"^[^:]{1,80}:$")                              # a line that only introduces a list ("Constraints:", "Constraints (confirmed):"): a heading, not an item
_CITATION = re.compile(r"\s*\[(?:[A-Za-z0-9@#,\s-]{2,60})\]")
# A file read back from the document store has lost its `#` markers (the store keeps a heading as a heading path, not as a line of text), so a `#` prefix is optional on the
# first line and on each block heading. The block heading must also carry "(from ...)", which the file always writes, so ordinary prose that starts with an ID is not mistaken for one.
_FIRST_LINE = re.compile(r"^(?:#{1,6}\s+)?(?P<stage>[^:]+?):\s+(?P<what>open questions to answer|constraints and assumptions to confirm)\s+\(project\s+(?P<name>.*)\s+(?P<short>[0-9a-f]{6,12})\)\s*$", re.IGNORECASE)
_BLOCK = re.compile(r"^(?:#{1,6}\s+)?(?P<id>[A-Z]{2,4}-[QC]\d{1,3})\s+\(from\s.*\)\s*$")
_ANSWER_LINE = re.compile(r"^\*\*(?:Answer|Confirmed or corrected):\*\*[ \t]*(?P<text>.*)$")


@dataclass(frozen=True)
class Item:
    id: str                       # e.g. REQ-Q1, SET-C2
    artefact_id: str              # the document it came from
    title: str
    text: str                     # the writer's own words, citations removed


@dataclass(frozen=True)
class Collected:
    phase_key: str
    questions: tuple[Item, ...] = ()
    constraints: tuple[Item, ...] = ()


@dataclass(frozen=True)
class Answers:
    phase_key: str | None
    project_short: str
    kind: str
    answered: dict[str, str] = field(default_factory=dict)       # item id -> the person's answer (non-empty only)
    listed: tuple[str, ...] = ()                                 # every item id the file lists, answered or not
    asked: dict[str, str] = field(default_factory=dict)          # item id -> the question text the file printed under that id (what the person was answering)


def prefix_of(phase_key: str) -> str:
    """Three letters per phase, used in item IDs (REQ-Q1)."""
    return {"setup": "SET", "requirements": "REQ", "design": "DES", "architecture": "ARC", "build": "BLD", "deploy": "DEP", "handover": "HND"}.get(phase_key, phase_key[:3].upper())


_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")


def plain(text: str) -> str:
    """The text without Markdown bold markers (`**Deadline:** 10 May` reads `Deadline: 10 May`). The charter writes its constraints with a bold label; in a list, a file or a prompt the asterisks are
    only noise. Paired markers only, so a lone asterisk (`5 * 3`) stays."""
    return _BOLD.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text or "")


def _clean(text: str) -> str:
    text = plain(_CITATION.sub("", text))                         # the citations belong to the document, not to a question
    return " ".join(text.split())[:MAX_ITEM_CHARS]


def bullets(section: str) -> list[str]:
    """The list items of a section, each with its continuation lines folded in. A section that is not a list is taken line by line."""
    items: list[str] = []
    for line in section.splitlines():
        match = _BULLET.match(line)
        if match:
            items.append(match.group(1).strip())
        elif line.strip() and items and (line.startswith("  ") or line.startswith("\t")):
            items[-1] += " " + line.strip()                       # an indented continuation of the item above
        elif line.strip() and not items and not _LABEL.match(line.strip()):
            items.append(line.strip())                            # prose with no list: one item per paragraph line (a line that only names the list below it is not one)
    return [i for i in (_clean(x) for x in items) if i and not _NONE.match(i)]


def collect(phase_key: str, artefacts: Iterable[tuple[str, str, str]]) -> Collected:
    """`artefacts` is (artefact id, title, body of its LATEST version), in the order to list them."""
    prefix = prefix_of(phase_key)
    questions: list[Item] = []
    constraints: list[Item] = []
    for artefact_id, title, body in artefacts:
        for section in section_bodies(body or "", QUESTIONS_HEADING):
            for text in bullets(section):
                if len(questions) < MAX_ITEMS:
                    questions.append(Item(f"{prefix}-Q{len(questions) + 1}", artefact_id, title, text))
        if phase_key in CONSTRAINT_PHASES:
            for section in section_bodies(body or "", CONSTRAINTS_HEADING):
                for text in bullets(section):
                    if len(constraints) < MAX_ITEMS:
                        constraints.append(Item(f"{prefix}-C{len(constraints) + 1}", artefact_id, title, text))
    return Collected(phase_key, tuple(questions), tuple(constraints))


def text_key(text: str) -> str:
    """What a resolved mark is tied to (2026-11-19): the question's WORDS, with case, spacing, citations, bold markers and a trailing mark ignored. A question number (REQ-Q3) changes whenever a document is
    rewritten, so a mark tied to it could land on a different question; tied to the words, it keeps applying only while a question reads the same, and a reworded question shows as open again,
    which is the safe direction."""
    return " ".join(plain(_CITATION.sub("", text or "")).split()).casefold().rstrip(" ?.!:;")[:MAX_ITEM_CHARS]


def file_name(phase_key: str, kind: str) -> str:
    return f"{phase_key}-open-questions.md" if kind == KIND_QUESTIONS else f"{phase_key}-constraints-and-assumptions.md"


def first_line(stage_name: str, kind: str, project_name: str, short_id: str) -> str:
    what = "open questions to answer" if kind == KIND_QUESTIONS else "constraints and assumptions to confirm"
    return f"# {stage_name}: {what} (project {' '.join(project_name.split())} {short_id})"


def render(collected: Collected, kind: str, *, stage_name: str, project_name: str, short_id: str, answered: Sequence[str] = ()) -> str:
    """The Markdown file. Items already answered in an uploaded answers file are left out, so the file lists only what is still open."""
    items = collected.questions if kind == KIND_QUESTIONS else collected.constraints
    left = [i for i in items if i.id not in set(answered)]
    label = ANSWER_LABEL if kind == KIND_QUESTIONS else CONFIRM_LABEL
    lines = [first_line(stage_name, kind, project_name, short_id), ""]
    if kind == KIND_QUESTIONS:
        lines += [f"These are the questions the {stage_name} documents could not settle from the sources. Write your answer after each **Answer:** line, "
                  "then upload this file to the project as a document, send the phase back for changes, and rewrite. Leave a question blank to leave it open.", ""]
    else:
        lines += [f"These are the constraints and assumptions the project charter states. After each **Confirmed or corrected:** line, confirm it or write the correction, "
                  "then upload this file to the project as a document, send the phase back for changes, and rewrite. Leave a line blank to leave it as written.", ""]
    if not left:
        lines += ["Nothing is open here."]
        return "\n".join(lines).rstrip() + "\n"
    for item in left:
        lines += [f"### {item.id} (from {item.artefact_id}: {item.title})", "", item.text, "", label, ""]
    return "\n".join(lines).rstrip() + "\n"


def parse_answers(text: str) -> Answers | None:
    """Read a returned file. None when it is not one of ours (its first line does not name a stage and a project)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    head = next((ln for ln in lines if ln.strip()), "")
    match = _FIRST_LINE.match(head.strip())
    if match is None:
        return None
    stage = normalise_heading(match.group("stage"))
    kind = KIND_QUESTIONS if match.group("what").lower().startswith("open") else KIND_CONSTRAINTS
    answered: dict[str, str] = {}
    listed: list[str] = []
    asked: dict[str, list[str]] = {}
    current: str | None = None
    buffer: list[str] = []
    collecting = False

    def close() -> None:
        nonlocal current, buffer, collecting
        if current is not None:
            answer = " ".join(" ".join(buffer).split())[:MAX_ANSWER_CHARS]
            if answer:
                answered[current] = answer
        current, buffer, collecting = None, [], False

    for line in lines:
        block = _BLOCK.match(line.strip())
        if block:
            close()
            current = block.group("id")
            listed.append(current)
            asked.setdefault(current, [])
            continue
        if current is None:
            continue
        label = _ANSWER_LINE.match(line.strip())
        if label:
            collecting = True
            if label.group("text").strip():
                buffer.append(label.group("text").strip())
            continue
        if collecting:
            buffer.append(line.strip())
        elif line.strip():
            asked[current].append(line.strip())                       # between the block heading and the answer label: the question as the file printed it
    close()
    return Answers(_phase_of(stage), match.group("short").lower(), kind, answered, tuple(listed), {k: " ".join(" ".join(v).split()) for k, v in asked.items() if v})


_STAGES = {"setup": "setup", "requirements": "requirements", "design": "design", "architecture": "architecture", "build and test": "build", "build": "build", "deploy": "deploy", "bau handover": "handover", "handover": "handover"}


def _phase_of(stage: str) -> str | None:
    return _STAGES.get(stage)


def block_answer(passage: str) -> tuple[str, str] | None:
    """(item id, answer) when this ONE passage is an answered block of our file, else None. A passage of our file is one block (the store splits at headings), so the
    answer can be cited by the passage's own id. A blank answer is not an answer."""
    current: str | None = None
    buffer: list[str] = []
    collecting = False
    for line in (passage or "").replace("\r\n", "\n").split("\n"):
        block = _BLOCK.match(line.strip())
        if block and current is None:
            current = block.group("id")
            continue
        if current is None:
            continue
        label = _ANSWER_LINE.match(line.strip())
        if label:
            collecting = True
            if label.group("text").strip():
                buffer.append(label.group("text").strip())
            continue
        if collecting:
            buffer.append(line.strip())
    answer = " ".join(" ".join(buffer).split())[:MAX_ANSWER_CHARS]
    return (current, answer) if current is not None and answer else None


def answers_as_notes(answers: Answers, items: Sequence[Item], source_id: str) -> list[str]:
    """The filled-in answers as plain sentences for a rewrite's prompt, each naming the question it answers. Empty answers are not included."""
    by_id = {i.id: i for i in items}
    out = []
    for item_id, answer in answers.answered.items():
        question = by_id.get(item_id)
        asked = f" ({question.text[:300]})" if question else ""
        out.append(f"The person answered {item_id}{asked}: {answer}")
    return out
