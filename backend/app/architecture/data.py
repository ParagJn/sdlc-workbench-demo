"""Database pictures for the two design documents (S30).

The high-level design has a **Data overview** and the low-level design a **Database design**. Like the Architecture diagrams (S14b), a model never draws:
it writes plain list lines, this module checks every line points at something that exists, and the picture is drawn when the document is read, from its
current text, and never stored.

    High-level design, Data overview (the CONCEPTUAL picture: the main things kept, no columns)
        Things kept:
        - Booking: a request to use a room for a time.
        Relationships:
        - Booking -> Room: each booking is for one room (many to one).

    Low-level design, Database design (the LOGICAL picture: every table, with its columns, and how they relate)
        - bookings -> rooms: bookings.room_id refers to rooms.id (many to one).

The tables of the logical picture are the TBL items of the same document, so the picture can never show a table the Tables section does not have or miss
one. Either section may instead say "Not Applicable" and a reason (a system that keeps nothing); nothing is drawn then. Nothing here raises.
"""
from __future__ import annotations

import re

from ..agents.items import ItemSpec, list_entries, not_applicable, parse_items
from ..agents.reply import section_bodies, unfenced_lines
from ..build.checks import table_names_in_design
from .diagram import NAME_LINE_CHARS, _clip, _frame, _paint_links_and_key, _text, _wrap, short_name
from .layout import NODE_H, Edge, layout_flow
from .style import DEFAULT, style_of

CONCEPTUAL = "high_level_design"
LOGICAL = "low_level_design"
SECTION = {CONCEPTUAL: "Data overview", LOGICAL: "Database design"}
TABLES_SECTION = "Tables"
TBL = ItemSpec("TBL", TABLES_SECTION)

MAX_THINGS = 12                # things kept, or tables
MAX_RELATIONSHIPS = 20
MAX_COLUMNS_SHOWN = 8          # a table box lists this many columns, then "and N more"
COLUMN_ROW = 14
COLUMN_CHARS = 24

_ARROW = r"(?:->|→|—>|-->)"
_RELATION = re.compile(rf"^(?P<a>[^:]+?)\s*{_ARROW}\s*(?P<b>[^:]+?)\s*:\s*(?P<label>\S.*)$")
_LABEL_THINGS = re.compile(r"^\W*things\s+kept\W*$", re.IGNORECASE)
_LABEL_RELATIONS = re.compile(r"^\W*relationships?\W*$", re.IGNORECASE)
_DERIVES = re.compile(r"\bderives[ \t]+from[ \t]*:", re.IGNORECASE)
_OWNED = re.compile(r"\bowned[ \t]+by\b", re.IGNORECASE)
_TICKED = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_PLAIN = re.compile(r"([a-z_][a-z0-9_]*)(?=$|[\s:,;.(])")
_KEY_WORD = re.compile(r"\b(?:key|primary)\b", re.IGNORECASE)


def norm(name: str) -> str:
    """Names are compared without caring about case or spacing. (Bold marks and quotes are taken off where a name is read: things_of, relations_of.)"""
    return " ".join(name.lower().split())


# ---- reading the sections ---------------------------------------------------------------------------------------------------------------------

def _section(body: str, kind: str) -> str | None:
    found = section_bodies(body, SECTION[kind])
    return found[0] if found else None


def _split_labelled(text: str) -> tuple[str, str]:
    """(the lines under 'Things kept:', the lines under 'Relationships:'). A line before any label belongs to neither."""
    things: list[str] = []
    relations: list[str] = []
    into: list[str] | None = None
    for line in unfenced_lines(text):
        if _LABEL_THINGS.match(line):
            into = things
        elif _LABEL_RELATIONS.match(line):
            into = relations
        elif into is not None:
            into.append(line)
    return "\n".join(things), "\n".join(relations)


def things_of(entries: list[str]) -> list[str]:
    """A thing is named by the words before the first colon ("Booking: a request to use a room" -> "Booking")."""
    out: list[str] = []
    for entry in entries:
        name = short_name(entry).strip(" `*")                     # short_name stops at the first colon; bold marks and quotes are not part of a name
        if name:
            out.append(name)
    return out


def relations_of(entries: list[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """((from, to, what it says), the entries that are not written as 'A -> B: what it says')."""
    good: list[tuple[str, str, str]] = []
    bad: list[str] = []
    for entry in entries:
        match = _RELATION.match(entry.strip())
        if match is None:
            bad.append(_clip(entry, 60))
        else:
            good.append((match.group("a").strip(" `*"), match.group("b").strip(" `*"), match.group("label").strip()))
    return good, bad


def _tbl_items(body: str):
    return parse_items(body, [TBL]).items


_LABELLED_COLUMNS = re.compile(r"\bColumns\s*:\s*", re.IGNORECASE)
_LABELLED_KEY = re.compile(r"\bKey\s*:\s*`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)
_AFTER_COLUMNS = re.compile(r"\.\s+(?=[A-Z])")          # the sentence after the last column: ". Holds the 6 seeded depots..."


def _labelled(text: str) -> tuple[list[str], str | None] | None:
    """The columns and the key of a table written the way the Low-level design writes it now: `name. Key: id (key, uuid). Columns: a (text, unique); b (text); c (uuid).` followed by free text.
    None when the entry has no `Columns:` label, so the older shape (`name: id (key), room id, start`) is read as before. A column is the word before its first bracket; columns are split on
    `;` outside brackets, and the free sentence after the last column ("Holds the 6 seeded depots... Owner: COMP-001") is not a column."""
    found = _LABELLED_COLUMNS.search(text)
    if found is None:
        return None
    rest = _DERIVES.split(text[found.end():], maxsplit=1)[0]
    rest = _OWNED.split(rest, maxsplit=1)[0]
    parts: list[str] = []
    depth, current = 0, ""
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    if parts:
        parts[-1] = _AFTER_COLUMNS.split(parts[-1], maxsplit=1)[0]          # the last column runs to the end of its sentence
    columns: list[str] = []
    for part in parts:
        label = " ".join(part.split()).strip(" .")
        name = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?", label)
        if name and name.group(1) not in columns:
            columns.append(name.group(1))
    key_match = _LABELLED_KEY.search(text[:found.start()])
    key = key_match.group(1) if key_match else next((c for c, part in zip(columns, parts) if _KEY_WORD.search(part)), None)
    if key is not None and key not in columns:
        columns.insert(0, key)                                                # the key leads the box even when it is named only in 'Key:'
    return columns, key


def table_of(item) -> tuple[str, list[str], str | None]:
    """(name, columns, the key column) of a TBL item, or ("", [], None) when its name cannot be read. The name is read as the Build phase reads it
    (`table_names_in_design`): backticked, or a lower-case word with underscores. Columns are the comma-separated words after the colon, up to
    "Owned by" or "Derives from"; the key is the one marked "key" or "primary"."""
    text = item.text.lstrip(" :.-")
    named = _TICKED.match(text) or _PLAIN.match(text)
    if named is None:
        return "", [], None
    name = named.group(1)
    rest = text[named.end():].lstrip("`")
    labelled = _labelled(text[named.end():])
    if labelled is not None:
        return name, labelled[0], labelled[1]
    rest = _DERIVES.split(rest, maxsplit=1)[0]                  # cut these first: the colon in "Derives from:" is not the start of the columns
    rest = _OWNED.split(rest, maxsplit=1)[0]
    if ":" not in rest.split("(", 1)[0]:
        return name, [], None
    rest = rest.split(":", 1)[1].strip().rstrip(".;")
    parts: list[str] = []
    depth, current = 0, ""
    for ch in rest:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    columns: list[str] = []
    key: str | None = None
    for part in parts:
        label = " ".join(part.split())
        if not label:
            continue
        shown = " ".join(label.split("(", 1)[0].replace("`", "").split()) or label          # "id (key)" -> "id"; "status (open (old), closed)" -> "status"
        columns.append(shown)
        if key is None and _KEY_WORD.search(label):
            key = shown
    return name, columns, key


def tables_of(body: str) -> list[tuple[str, list[str], str | None]]:
    """Every readable TBL item of the document, once per name."""
    seen: set[str] = set()
    out: list[tuple[str, list[str], str | None]] = []
    for item in _tbl_items(body):
        name, columns, key = table_of(item)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append((name, columns, key))
    return out


# ---- the check --------------------------------------------------------------------------------------------------------------------------------

def _na_problem(kind: str, reason: str) -> list[str]:
    return [] if reason else [f"'{SECTION[kind]}' says Not Applicable but gives no reason; write 'Not Applicable' and one sentence saying why (for example, that the system keeps nothing between uses)"]


def data_problems(kind: str, body: str) -> list[str]:
    """Plain sentences about the database section of a design document. [] means it can be drawn (or says, with a reason, that there is nothing to draw)."""
    if kind not in SECTION:
        return []
    text = _section(body, kind)
    if text is None:
        return []                                             # a missing section is reported by the required-sections check
    name = SECTION[kind]
    reason = not_applicable(text)
    problems: list[str] = []
    if kind == LOGICAL:
        tbl = _tbl_items(body)
        tables_na = any(not_applicable(t) is not None for t in section_bodies(body, TABLES_SECTION))
        if reason is not None:
            problems += _na_problem(kind, reason)
            if tbl:
                names = ", ".join(table_names_in_design(body, [TBL])[:6]) or "the TBL items"
                problems.append(f"'{name}' says Not Applicable, but this design has tables ({names}); describe how they relate, or remove the tables if nothing is kept")
            return problems
        if tables_na:
            return [f"'{TABLES_SECTION}' says Not Applicable, so '{name}' must say Not Applicable too, with the same reason"]
        tables = tables_of(body)
        for item in tbl:
            if not table_of(item)[0]:
                problems.append(f"{item.id} does not start with a table name; write it as '{item.id} bookings: id (key), room id, start', a lower-case name with underscores first")
        for table, columns, _ in tables:
            if not columns:
                problems.append(f"the table {table} lists no columns; write it as '{table}: id (key), name, ...' so the picture can show them")
        if len(tables) > MAX_THINGS:
            problems.append(f"the design has {len(tables)} tables; the limit is {MAX_THINGS}. Keep the ones that matter most and list the rest under Open questions")
        known = {norm(t[0]): t[0] for t in tables}
        pairs, bad = relations_of(list_entries(text))
        unknown_word = "table"
    else:
        if reason is not None:
            return _na_problem(kind, reason)
        things_text, relations_text = _split_labelled(text)
        names = things_of(list_entries(things_text))
        if not names:
            return [f"'{name}' lists no things kept; under 'Things kept:' write '- Booking: what it is', or write Not Applicable and a reason if the system keeps nothing"]
        if len(names) > MAX_THINGS:
            problems.append(f"'{name}' lists {len(names)} things; the limit is {MAX_THINGS}. Keep the main ones")
        dupes = sorted({n for n in names if [norm(m) for m in names].count(norm(n)) > 1})
        for dupe in dupes:
            problems.append(f"'{dupe}' is listed more than once under 'Things kept:'; list each thing once")
        known = {norm(n): n for n in names}
        pairs, bad = relations_of(list_entries(relations_text))
        unknown_word = "thing"
    for line in bad:
        problems.append(f"'{line}' under '{name}' is not written as a relationship; write '{_example(kind)}', the two names, an arrow and a few words after the colon")
    for a, b, _ in pairs:
        for end in (a, b):
            if norm(end) not in known:
                listed = ", ".join(sorted(known.values())[:8]) or "none yet"
                problems.append(f"the relationship {a} -> {b} names '{end}', which is not a {unknown_word} in this design ({listed}); use the names exactly as listed")
        if norm(a) == norm(b):
            problems.append(f"the relationship {a} -> {b} joins {unknown_word} to itself; a relationship joins two different {unknown_word}s")
    if len(pairs) > MAX_RELATIONSHIPS:
        problems.append(f"'{name}' has {len(pairs)} relationships; the limit is {MAX_RELATIONSHIPS}. Keep the ones that matter most")
    seen: set[str] = set()
    return [p for p in problems if not (p in seen or seen.add(p))]


def _example(kind: str) -> str:
    return "- Booking -> Room: each booking is for one room (many to one)." if kind == CONCEPTUAL else "- bookings -> rooms: bookings.room_id refers to rooms.id (many to one)."


# ---- drawing ----------------------------------------------------------------------------------------------------------------------------------

def _readable(kind: str, body: str) -> tuple[list[str], dict[str, list[str]], dict[str, str | None], list[Edge]] | None:
    """(box names in order, columns of each, key of each, the relationships as edges), or None when there is nothing to draw."""
    text = _section(body, kind)
    if text is None or not_applicable(text) is not None:
        return None
    columns: dict[str, list[str]] = {}
    keys: dict[str, str | None] = {}
    if kind == LOGICAL:
        tables = tables_of(body)[:MAX_THINGS]
        names = [t[0] for t in tables]
        columns = {t[0]: t[1] for t in tables}
        keys = {t[0]: t[2] for t in tables}
        pairs, _ = relations_of(list_entries(text))
    else:
        things_text, relations_text = _split_labelled(text)
        names = []
        for n in things_of(list_entries(things_text)):
            if norm(n) not in [norm(m) for m in names]:
                names.append(n)
        names = names[:MAX_THINGS]
        pairs, _ = relations_of(list_entries(relations_text))
    by_norm = {norm(n): n for n in names}
    edges: list[Edge] = []
    for a, b, label in pairs:
        if norm(a) in by_norm and norm(b) in by_norm and norm(a) != norm(b) and len(edges) < MAX_RELATIONSHIPS:
            edges.append(Edge(f"REL-{len(edges) + 1:03d}", by_norm[norm(a)], by_norm[norm(b)], label))
    return names, columns, keys, edges


def draw_data(kind: str, body: str, style: str = DEFAULT) -> str:
    """The picture for the database section of a design document in one look, or "" when there is nothing to draw."""
    read = _readable(kind, body)
    if read is None:
        return ""
    names, columns, keys, edges = read
    st = style_of(style)
    logical = kind == LOGICAL
    shown = {n: min(len(columns.get(n, [])), MAX_COLUMNS_SHOWN) for n in names}
    extra = {n: len(columns.get(n, [])) - shown[n] for n in names}
    most = max([shown[n] + (1 if extra[n] > 0 else 0) for n in names] + [0])
    node_h = max(NODE_H, 38 + COLUMN_ROW * most) if logical else NODE_H
    layout = layout_flow(edges, {n: n for n in names}, nodes=tuple(names), node_h=node_h)
    if layout is None:
        return ""
    title = (f"Database design: {len(names)} tables, {len(edges)} relationships" if logical
             else f"Data overview: {len(names)} things kept, {len(edges)} relationships")
    parts = _frame(layout.width, layout.height, title, st)
    for box in layout.boxes:
        parts.append(f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" rx="{st.box_radius}" fill="{st.box_fill}" '
                     f'stroke="{st.box_stroke}" stroke-width="{st.box_stroke_width}"/>')
        cx = box.x + box.w / 2
        if not logical:
            lines = _wrap(box.key, NAME_LINE_CHARS)
            top = box.y + (box.h - 16 * len(lines)) / 2
            for i, line in enumerate(lines):
                parts.append(_text(cx, top + 12 + 16 * i, line, 12.5, st.title, st, "middle", st.title_weight))
            continue
        parts.append(_text(cx, box.y + 18, _clip(box.key, NAME_LINE_CHARS), 12.5, st.title, st, "middle", "bold"))
        parts.append(f'<line x1="{box.x}" y1="{box.y + 26}" x2="{box.x + box.w}" y2="{box.y + 26}" stroke="{st.box_stroke}" stroke-width="1"/>')
        for i, column in enumerate(columns.get(box.key, [])[:MAX_COLUMNS_SHOWN]):
            y = box.y + 26 + 15 + COLUMN_ROW * i
            parts.append(_text(box.x + 10, y, _clip(column, COLUMN_CHARS), 10.5, st.title, st))
            if keys.get(box.key) == column:
                parts.append(_text(box.x + box.w - 10, y, "key", 9, st.id_ink, st, "end", "bold"))
        if extra[box.key] > 0:
            parts.append(_text(box.x + 10, box.y + 26 + 15 + COLUMN_ROW * shown[box.key], f"and {extra[box.key]} more", 10, st.key_muted, st))
    _paint_links_and_key(parts, layout, st, "Relationships")
    parts.append("</svg>")
    return "".join(parts)
