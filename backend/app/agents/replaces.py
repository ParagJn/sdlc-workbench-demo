"""The "Replaces:" line (2026-11-18/19): when a person's answer replaced something a source said, the writer adds `Replaces: "the old words" [DOC-006#5]` beside the statement.

That line is HISTORY for the person reading the document. It must never reach anything that reads the document as evidence: a later phase's writer (it would see the old words and may
treat them as current), the reviewer (it would flag the old words as a conflict, and the citation after them would pull the old document in) or a rewrite's "previous draft". `strip_replaces`
takes the lines out of the text those readers are given. The stored document is never changed: the person still sees the line, drawn in orange.

It recognises ONLY the form the writer is told to use: the label, then the old words in quotes, then optional citations. A bare `Replaces: legacy ETL jobs` (a real field in a migration
document) is never touched: a line missed is harmless, a line wrongly cut is a person's text lost. Fenced code and inline code are left exactly as they are.

Pure and bounded: one pass over the lines, no backtracking patterns.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LABEL = re.compile(r"(?<![\w-])Replaces:", re.IGNORECASE)
_QUOTED = re.compile(r'\s*(?:"[^"\n]{1,300}"|“[^”\n]{1,300}”)')
_CITE = re.compile(r"\s*[,;]?\s*\[[A-Za-z0-9@#,;\s-]{2,120}\]")
_TAIL = re.compile(r"\s*[.;]?[*_]{0,2}")
_ONLY_MARKER = re.compile(r"\s*(?:[-*+]|\d{1,3}[.)])?\s*[*_]{0,4}\s*")


def _inside_code(line: str, at: int) -> bool:
    """Is position `at` inside an inline code span (an odd number of backticks before it)?"""
    return line.count("`", 0, at) % 2 == 1


def _strip_line(line: str) -> str:
    """Remove each `Replaces: "words" [ids]` from one line. Only when quoted words follow the label, so nothing is cut that cannot be delimited."""
    pos = 0
    while True:
        m = _LABEL.search(line, pos)
        if m is None:
            return line
        q = None if _inside_code(line, m.start()) else _QUOTED.match(line, m.end())
        if q is None:
            pos = m.end()
            continue
        end = q.end()
        while (c := _CITE.match(line, end)) is not None:
            end = c.end()
        end = _TAIL.match(line, end).end()
        head, rest = line[:m.start()].rstrip(), line[end:]
        line = (head + (" " + rest.lstrip() if rest.strip() else "")) if head else rest.lstrip()
        pos = len(head)


def strip_replaces(text: str) -> str:
    """The text without its history lines. A line that held only that is removed whole; one that follows a statement loses just the history. Returns the same object when nothing changed."""
    if not text or "replaces:" not in text.lower():
        return text
    out: list[str] = []
    fence: str | None = None
    changed = False
    for line in text.split("\n"):
        f = _FENCE.match(line)
        if f is not None:
            mark = f.group(1)[0]
            fence = mark if fence is None else (None if fence == mark else fence)
            out.append(line)
            continue
        if fence is not None:
            out.append(line)
            continue
        cut = _strip_line(line)
        if cut != line:
            changed = True
            if _ONLY_MARKER.fullmatch(cut):
                continue                                       # the line held nothing but the history: the whole line goes
            cut = cut.rstrip()
        out.append(cut)
    return "\n".join(out) if changed else text
