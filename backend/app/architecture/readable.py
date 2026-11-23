"""The Low-level design's tables and endpoints, read into labelled parts for a person (2026-11-22, the user's request: the text is written for an agent and is hard for a person to read).

Two readers, both pure and both bounded:
  * `table_of_entry`    `TBL-003 assets. Key: id (key, uuid). Columns: a (text, unique); b (uuid, refers to x). Holds ... Owner: COMP-002. Derives from: ...`
  * `endpoint_of_entry` `API-001 GET /api/v1/assets. Owner: COMP-002. Lets a ... Takes in ... Gives back ... Errors: 401; 403 ...`  and the second style the writer also produces,
                        `API-007 PATCH /api/v1/work-orders/{id} edits a ... Takes in: ... Gives back: ... Derives from: ...`

NOTHING IS LOST AND NOTHING IS INVENTED. Every word of an entry lands in one of the parts, in its order, so the parts can always be put back together; a label the entry does not have is simply
absent (it is never made up), and an entry that is not in either shape returns None so the caller keeps it as it was written. The stored text is never changed: this only reads it.

`reshape` writes a design's Markdown again with each such entry as a small block (a table for a database table; labelled lines for an endpoint; a sentence with its sources on a line of their own for a rule,
a decision or an interface; a two-column table for the high-level design's "Things kept"). It is used twice and nowhere else is the reading
repeated: the Word file is made from it, and the artefact detail the screen reads carries it as `readable`, so the screen and the Word file can never lay an entry out differently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_ENTRY_CHARS = 20000
MAX_COLUMNS = 200

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_OWNER = re.compile(r"\bOwner:\s*(COMP-\d{3,})\.?", re.IGNORECASE)
_DERIVES = re.compile(r"\bDerives\s+from\s*:\s*(?P<tail>.*)$", re.IGNORECASE | re.DOTALL)
_KEY = re.compile(r"\bKey\s*:\s*`?(?P<name>[A-Za-z_][A-Za-z0-9_]*)`?\s*(?:\((?P<inner>[^)]*)\))?", re.IGNORECASE)
_COLUMNS = re.compile(r"\bColumns\s*:\s*", re.IGNORECASE)
_AFTER_COLUMNS = re.compile(r"\.\s+(?=[A-Z])")
_TABLE_HEAD = re.compile(r"^(?P<id>TBL-\d{3,})\s+`?(?P<name>[A-Za-z_][A-Za-z0-9_]*)`?\s*(?P<rest>.*)$", re.DOTALL)
_ENDPOINT_HEAD = re.compile(rf"^(?P<id>API-\d{{3,}})\s+(?P<method>{'|'.join(_METHODS)})\s+(?P<path>\S+?)\.?(?=\s|$)\s*(?P<rest>.*)$", re.DOTALL)
# a label starts a part when it begins the text or follows the end of a sentence; "Takes in" in the middle of a sentence is just words
# "Error cases:" is the same label as "Errors:" (the writer uses both). It needs its colon, so a sentence that merely starts with those words ("Error cases are rare.") is not taken for the label.
_PART = re.compile(r"(?:(?<=^)|(?<=\.\s)|(?<=\.\)\s))(?:(?P<label>Takes in|Gives back|Errors)\b:?|(?P<cases>Error cases?):)\s*", re.MULTILINE)


@dataclass(frozen=True)
class Column:
    name: str
    type: str = ""
    notes: str = ""
    key: bool = False


@dataclass(frozen=True)
class TableEntry:
    id: str
    name: str
    owner: str = ""
    columns: tuple[Column, ...] = ()
    note: str = ""                     # the sentences after the last column that are not the owner or the sources
    derives: str = ""                  # the text after "Derives from:", as written


@dataclass(frozen=True)
class EndpointEntry:
    id: str
    method: str
    path: str
    owner: str = ""
    summary: str = ""
    takes_in: str = ""
    gives_back: str = ""
    errors: str = ""
    derives: str = ""
    labels: tuple[str, ...] = field(default=())     # which of the three labels the entry really had, in order


@dataclass(frozen=True)
class RuleEntry:
    id: str
    text: str
    derives: str = ""


@dataclass(frozen=True)
class DecisionEntry:
    id: str
    summary: str
    purpose: str = ""
    considered: str = ""
    why: str = ""
    derives: str = ""
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterfaceEntry:
    id: str
    source: str
    target: str
    text: str = ""
    reply: str = ""                    # "Waits." or "Does not wait for a reply.", as the entry says it; empty when it says nothing
    derives: str = ""


def _split_top(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth, current = 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def _tidy(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _column(part: str) -> Column | None:
    label = _tidy(part).strip(" .")
    m = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s*(?:\((.*)\))?\s*$", label, re.DOTALL)
    if not m:
        return None
    inner = _tidy(m.group(2) or "")
    pieces = _split_top(inner, ",")
    first, rest = pieces[0], ",".join(pieces[1:])
    if ":" in first:                                     # "text: INVITED, ACTIVE or LOCKED": a type, then the values it may hold
        kind, _, values = inner.partition(":")
        return Column(m.group(1), _tidy(kind), f"values: {_tidy(values)}" if _tidy(values) else "")
    return Column(m.group(1), _tidy(first), _tidy(rest))


def table_of_entry(entry: str) -> TableEntry | None:
    """A database table written as `TBL-001 name. Key: id (key, uuid). Columns: a (type, notes); b (type); ...`, or None when the entry has no `Columns:` list."""
    text = (entry or "")[:MAX_ENTRY_CHARS].strip()
    head = _TABLE_HEAD.match(text)
    if head is None:
        return None
    rest = head.group("rest")
    cols_at = _COLUMNS.search(rest)
    if cols_at is None:
        return None
    before, after = rest[: cols_at.start()], rest[cols_at.end():]
    derives_m = _DERIVES.search(after)
    derives = _tidy(derives_m.group("tail")) if derives_m else ""
    if derives_m:
        after = after[: derives_m.start()]
    owner_m = _OWNER.search(after)
    owner = owner_m.group(1) if owner_m else (m.group(1) if (m := _OWNER.search(before)) else "")
    if owner_m:
        after = after[: owner_m.start()]
    parts = _split_top(after, ";")
    note = ""
    if parts:
        split = _AFTER_COLUMNS.split(parts[-1], maxsplit=1)
        parts[-1] = split[0]
        note = _tidy(split[1]) if len(split) > 1 else ""
    columns: list[Column] = []
    for part in parts[:MAX_COLUMNS]:
        col = _column(part)
        if col is not None and col.name not in {c.name for c in columns}:
            columns.append(col)
    key_m = _KEY.search(before)
    lead = _tidy((before[: key_m.start()] + " " + before[key_m.end():]) if key_m else before)          # what sits between the name and the columns
    lead = _tidy(_OWNER.sub("", lead)).strip(". ")
    if lead:
        note = _tidy(f"{lead}. {note}") if note else f"{lead}."
    key_name = key_m.group("name") if key_m else next((c.name for c in columns if re.search(r"\b(?:key|primary)\b", c.notes, re.IGNORECASE)), "")
    if key_name:
        inner = _tidy(key_m.group("inner") or "") if key_m else ""
        kind = _tidy(re.sub(r"\b(?:key|primary(?:\s+key)?)\b,?\s*", "", inner, flags=re.IGNORECASE)).strip(", ")
        have = next((c for c in columns if c.name == key_name), None)
        keyed = Column(key_name, have.type if have and have.type else kind, "key" if not (have and have.notes) else f"key, {have.notes}", True)
        columns = [keyed] + [c for c in columns if c.name != key_name]
    if not columns:
        return None
    return TableEntry(head.group("id"), head.group("name"), owner, tuple(columns), note, derives)


def endpoint_of_entry(entry: str) -> EndpointEntry | None:
    """An endpoint written as `API-001 GET /path. Owner: COMP-002. words. Takes in ... Gives back ... Errors: ...`, in either of its two styles, or None when it is neither."""
    text = (entry or "")[:MAX_ENTRY_CHARS].strip()
    head = _ENDPOINT_HEAD.match(text)
    if head is None:
        return None
    rest = head.group("rest")
    derives_m = _DERIVES.search(rest)
    derives = _tidy(derives_m.group("tail")) if derives_m else ""
    if derives_m:
        rest = rest[: derives_m.start()]
    owner_m = _OWNER.search(rest)
    owner = owner_m.group(1) if owner_m else ""
    if owner_m:
        rest = rest[: owner_m.start()] + " " + rest[owner_m.end():]
    cuts = [(m.start(), m.end(), m.group("label") or "Errors") for m in _PART.finditer(rest)]
    kept: list[tuple[int, int, str]] = []
    for c in cuts:                                         # the first time each label appears starts that part
        if all(k[2] != c[2] for k in kept):
            kept.append(c)
    summary_end = kept[0][0] if kept else len(rest)
    parts = {"Takes in": "", "Gives back": "", "Errors": ""}
    for i, (_, end, label) in enumerate(kept):
        stop = kept[i + 1][0] if i + 1 < len(kept) else len(rest)
        parts[label] = _tidy(rest[end:stop])
    summary = _tidy(rest[:summary_end])
    if summary and summary[0].islower():
        summary = summary[0].upper() + summary[1:]
    return EndpointEntry(head.group("id"), head.group("method"), head.group("path").rstrip("."), owner, summary, parts["Takes in"], parts["Gives back"], parts["Errors"], derives,
                         tuple(k[2] for k in kept))


_RULE_HEAD = re.compile(r"^(?P<id>RUL-\d{3,})\s+(?P<rest>.+)$", re.DOTALL)
_DECISION_HEAD = re.compile(r"^(?P<id>DEC-\d{3,})\s+(?P<rest>.+)$", re.DOTALL)
_INTERFACE_HEAD = re.compile(r"^(?P<id>IFC-\d{3,})\s+(?P<a>COMP-\d{3,})\s+(?:to|->|\u2192)\s+(?P<b>COMP-\d{3,})\.?\s*(?P<rest>.*)$", re.DOTALL)
# a label starts a part only after the end of a sentence AND with its colon: "Why" in the middle of a sentence is just a word
_DECISION_PART = re.compile(r"(?:(?<=^)|(?<=\.\s)|(?<=\.\)\s)|(?<=\]\s))(?P<label>Purpose|Considered|Why):\s*", re.MULTILINE)
_REPLY = re.compile(r"(?:(?<=^)|(?<=\.\s)|(?<=\.\)\s))(?P<reply>(?:Waits|Does not wait)\b[^.]*\.?)\s*$")


def _without_sources(text: str) -> tuple[str, str]:
    """(the text before 'Derives from:', what follows it). The sources are always the last thing in these entries."""
    m = _DERIVES.search(text)
    return (text[: m.start()], _tidy(m.group("tail"))) if m else (text, "")


def rule_of_entry(entry: str) -> RuleEntry | None:
    """A rule written as `RUL-001 sentence. Derives from: FR-001, ...`, or None when it is not (or has no words)."""
    head = _RULE_HEAD.match((entry or "")[:MAX_ENTRY_CHARS].strip())
    if head is None:
        return None
    body, derives = _without_sources(head.group("rest"))
    body = _tidy(body)
    return RuleEntry(head.group("id"), body, derives) if body else None


def decision_of_entry(entry: str) -> DecisionEntry | None:
    """A decision written as `DEC-001 sentence. Purpose: ... Considered: ... Why: ... Derives from: ...`. A label the entry does not have is absent (twelve of FieldOps' decisions have no Purpose)."""
    head = _DECISION_HEAD.match((entry or "")[:MAX_ENTRY_CHARS].strip())
    if head is None:
        return None
    body, derives = _without_sources(head.group("rest"))
    kept: list[tuple[int, int, str]] = []
    for m in _DECISION_PART.finditer(body):
        if all(k[2] != m.group("label") for k in kept):
            kept.append((m.start(), m.end(), m.group("label")))
    parts = {"Purpose": "", "Considered": "", "Why": ""}
    for i, (_, end, label) in enumerate(kept):
        parts[label] = _tidy(body[end: kept[i + 1][0] if i + 1 < len(kept) else len(body)])
    summary = _tidy(body[: kept[0][0] if kept else len(body)])
    if not summary:
        return None
    return DecisionEntry(head.group("id"), summary, parts["Purpose"], parts["Considered"], parts["Why"], derives, tuple(k[2] for k in kept))


def interface_of_entry(entry: str) -> InterfaceEntry | None:
    """An interface written as `IFC-001 COMP-012 to COMP-011. What is asked or sent. Waits. Derives from: ...`, or None when it does not start with the two components."""
    head = _INTERFACE_HEAD.match((entry or "")[:MAX_ENTRY_CHARS].strip())
    if head is None:
        return None
    body, derives = _without_sources(head.group("rest"))
    body = _tidy(body)
    reply = _REPLY.search(body)
    if reply:
        text, said = _tidy(body[: reply.start()]), _tidy(reply.group("reply"))
    else:
        text, said = body, ""
    return InterfaceEntry(head.group("id"), head.group("a"), head.group("b"), text, said, derives)


# ---- the Markdown for a Word file -------------------------------------------------------------------------------------------------------------------

_LIST_START = re.compile(r"^ ?(?:[-*+]|\d{1,3}[.)])[ \t]+(?:\*\*|__)?(?P<id>(?:TBL|API|RUL|DEC|IFC)-\d{3,})")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
SECTIONS = {"tables": "TBL", "endpoints": "API", "rules": "RUL", "decisions": "DEC", "interfaces": "IFC"}


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("`", "'") or "-"


def table_markdown(t: TableEntry) -> str:
    head = f"### {t.id} · {t.name}"
    meta = " · ".join(x for x in (f"Owner: {t.owner}" if t.owner else "", f"Key: {next((c.name for c in t.columns if c.key), '-')}") if x)
    lines = [head, "", f"*{meta}*", "", "| Column | Type | Notes |", "|---|---|---|"]
    lines += [f"| {'**' + _cell(c.name) + '**' if c.key else _cell(c.name)} | {_cell(c.type)} | {_cell(c.notes)} |" for c in t.columns]
    if t.note:
        lines += ["", t.note]
    if t.derives:
        lines += ["", f"*Derives from: {t.derives}*"]
    return "\n".join(lines)


def endpoint_markdown(e: EndpointEntry) -> str:
    lines = [f"### {e.id} · `{e.method} {e.path}`"]
    if e.owner:
        lines += ["", f"*Owner: {e.owner}*"]
    if e.summary:
        lines += ["", e.summary]
    for label, value in (("Takes in", e.takes_in), ("Gives back", e.gives_back), ("Errors", e.errors)):
        if value:
            lines += ["", f"**{label}:** {value}"]
    if e.derives:
        lines += ["", f"*Derives from: {e.derives}*"]
    return "\n".join(lines)


def _sources(derives: str) -> list[str]:
    return ["", f"*Derives from: {derives}*"] if derives else []


def rule_markdown(r: RuleEntry) -> str:
    return "\n".join([f"**{r.id}** {r.text}", *_sources(r.derives)])


def decision_markdown(d: DecisionEntry) -> str:
    lines = [f"**{d.id}** {d.summary}"]
    for label, value in (("Purpose", d.purpose), ("Considered", d.considered), ("Why", d.why)):
        if value:
            lines += ["", f"**{label}:** {value}"]
    return "\n".join([*lines, *_sources(d.derives)])


def interface_markdown(i: InterfaceEntry) -> str:
    lines = [f"### {i.id} \u00b7 {i.source} \u2192 {i.target}"]
    if i.text:
        lines += ["", i.text]
    if i.reply:
        lines += ["", f"**{i.reply}**"]
    return "\n".join([*lines, *_sources(i.derives)])


_THINGS_LABEL = re.compile(r"^\W*things\s+kept\W*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s{0,3}[-*+][ \t]+(?P<text>\S.*)$")


def things_table(lines: list[str], i: int) -> tuple[list[str], int] | None:
    """The 'Things kept:' list of a high-level design's Data overview as a two-column table (Thing, What it is), starting at the label line `i`. Returns (the lines to write, the index after the list),
    or None when it is not a list of `Name: what it is` bullets (then it is left as written). Continuation lines indented under a bullet belong to it."""
    rows: list[tuple[str, str]] = []
    j = i + 1
    while j < len(lines):
        m = _BULLET.match(lines[j])
        if m is None:
            break
        text = m.group("text")
        k = j + 1
        while k < len(lines) and lines[k].strip() and re.match(r"^(?:\s{2,}|\t)\S", lines[k]) and not _BULLET.match(lines[k]):
            text += " " + lines[k].strip()
            k += 1
        name, colon, what = text.partition(":")
        name = _tidy(name).strip("*_` ")
        if not colon or not name or len(name) > 80 or not _tidy(what):
            return None
        rows.append((name, _tidy(what)))
        j = k
    if not rows:
        return None
    return [lines[i], "", "| Thing | What it is |", "|---|---|", *[f"| **{_cell(n)}** | {_cell(w)} |" for n, w in rows], ""], j


READERS = {"TBL": (table_of_entry, table_markdown), "API": (endpoint_of_entry, endpoint_markdown), "RUL": (rule_of_entry, rule_markdown),
           "DEC": (decision_of_entry, decision_markdown), "IFC": (interface_of_entry, interface_markdown)}


# which sections each design document has laid out: a section name in the wrong document is left as written
DOCUMENT_SECTIONS = {
    "low_level_design": frozenset({"tables", "endpoints", "rules"}),
    "high_level_design": frozenset({"decisions", "interfaces", "data overview"}),
}


def reshape(body: str, sections: frozenset[str] | None = None) -> str:
    """The Markdown with every readable table and endpoint entry of the Tables and Endpoints sections written as a block (a table, or labelled lines). Everything else, and every entry that is not
    in a shape this reads, is left exactly as written. The text is the same words in the same order; only the layout changes. `sections` limits which sections are laid out (all of them when None)."""
    lines = (body or "").split("\n")
    out: list[str] = []
    section = ""
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        f = _FENCE.match(line)
        if f:
            mark = f.group(1)[0]
            fence = mark if fence is None else (None if fence == mark else fence)
            out.append(line)
            i += 1
            continue
        if fence is None:
            h = _H2.match(line)
            if h:
                section = re.sub(r"[^a-z0-9]+", " ", h.group(1).lower()).strip()
        active = sections is None or section in sections
        start = _LIST_START.match(line) if fence is None and active and SECTIONS.get(section) else None
        if start and start.group("id").split("-")[0] == SECTIONS[section]:
            j = i + 1
            while j < len(lines) and lines[j].strip() and re.match(r"^(?:\s{2,}|\t)\S", lines[j]) and not _LIST_START.match(lines[j]):
                j += 1
            entry = " ".join(re.sub(r"^ ?(?:[-*+]|\d{1,3}[.)])[ \t]+", "", lines[i]).split() + [p.strip() for p in lines[i + 1: j]])
            entry = re.sub(r"^(?:\*\*|__)(.+?)(?:\*\*|__)", r"\1", entry)
            reader, writer = READERS[SECTIONS[section]]
            read = reader(entry)
            if read is not None:
                out += ["", writer(read), ""]
                i = j
                continue
        if fence is None and active and section == "data overview" and _THINGS_LABEL.match(line):
            table = things_table(lines, i)
            if table is not None:
                out += table[0]
                i = table[1]
                continue
        out.append(line)
        i += 1
    return "\n".join(out)
