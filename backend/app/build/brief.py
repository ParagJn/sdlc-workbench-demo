"""S45: the Build brief. What a coding agent needs to build the project, ASSEMBLED FROM THE DESIGN by the system; the model writes only what the design does not say.

The earlier Build asked a model to restate a 255,000-character design as 335,000 characters of instructions, then held the restatement to string checks that gave a false alarm on every
real run. This module does the opposite: it COPIES the design's own words into files, so there is nothing to restate and nothing to check against it. Everything here is plain functions
on text; nothing calls a model and nothing is written to disk or to the database.

The set is a start-here file, a screen-to-API map, one BACKEND file for each group of APIs (grouped by the component that owns them), one FRONTEND file for each screen (with the APIs
it calls), and, written by the system, a traceability file and a checklist.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

KEY = "build_brief"
ITEM_PREFIX = "BRF"
START_NAME, MAP_NAME, TRACE_NAME, CHECK_NAME = "00-start-here.md", "01-screen-api-map.md", "98-traceability.md", "99-checklist.md"
FILE_MARK = re.compile(r"^=== FILE: (\S+) ===[ \t]*$")
_BE_NAME = re.compile(r"^BE-(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_FE_NAME = re.compile(r"^FE-(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_ID = re.compile(r"\b([A-Z]{1,5}-\d{3})\b")
_ENDPOINT = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S*?)\.?(?:\s|$)")
_FENCE = re.compile(r"^```.*?^```[ \t]*$", re.MULTILINE | re.DOTALL)
MAX_NAME = 80


def norm_path(path: str) -> str:
    """A route with every parameter written `{}`: `{reservation_id}`, `%s`, `<id>` and a bare number all become the same thing. Verify uses it to match a route in code to an API in the design."""
    p = path.strip().rstrip(".,;:)\"'")
    p = re.sub(r"\{[^}]*\}|%s|<[^>]*>", "{}", p)
    p = re.sub(r"(?<=/)\d+(?=/|$)", "{}", p)
    return p.rstrip("/") or "/"


def fingerprint(text: str) -> str:
    """A short, stable stamp of a text (12 hex characters), used to see whether what a file was copied from has changed."""
    return hashlib.sha256((text or "").strip("\n").encode("utf-8")).hexdigest()[:12]


def slug(text: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (words[:40].strip("-")) or "item"


def short_title(text: str) -> str:
    """A short name from a design item's text: what comes before the first full stop or colon ('Asset register. Responsible for ...' gives 'Asset register'), without formatting marks."""
    plain = re.sub(r"\s+", " ", re.sub(r"\bDerives\s+from\s*:.*$", "", text or "", flags=re.IGNORECASE | re.DOTALL)).strip()
    plain = re.sub(r"\*\*|__|`", "", plain)
    head = re.split(r"(?<=[A-Za-z0-9\)])[.:](?:\s|$)", plain, maxsplit=1)[0]
    head = head.strip(" .*_")
    return (head[:80].rstrip() + ("…" if len(head) > 80 else "")) or "(no title)"


def title_of(text: str) -> str:
    """The text of a file's first '# ' line."""
    for line in (text or "").split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ---- the design, as items ----------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DItem:
    id: str
    text: str                       # the rest of the line after the ID, as the design wrote it
    derives: tuple[str, ...]        # the IDs it names after 'Derives from:' (only ID-shaped ones)
    source: str = ""                # 'Low-level design v2': where it was copied from

    @property
    def prefix(self) -> str:
        return self.id.split("-")[0]

    @property
    def line(self) -> str:
        return f"{self.id} {self.text}".strip()


class Design:
    """Every item of the upstream design, by ID, with the artefact and version each came from. Withdrawn items are not here."""

    def __init__(self, items: Mapping[str, DItem], sections: Mapping[str, Mapping[str, str]] | None = None, sources: Mapping[str, tuple[str, int]] | None = None):
        self.items = dict(items)
        self.sections = {k: dict(v) for k, v in (sections or {}).items()}      # artefact key -> section name -> text (only the sections the brief copies whole)
        self.sources = dict(sources or {})                                       # artefact key -> (title, version)

    def of(self, prefix: str) -> list[DItem]:
        return sorted((i for i in self.items.values() if i.prefix == prefix), key=lambda i: i.id)

    def get(self, item_id: str) -> DItem | None:
        return self.items.get(item_id)

    def has(self, item_id: str) -> bool:
        return item_id in self.items

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self.items)


def ids_in(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ID.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def read_design(upstream: Iterable, library, section_reader) -> Design:
    """The items of every upstream artefact the library knows how to read, plus the whole sections the brief copies (the low-level design's 'Database design')."""
    from ..agents.items import parse_items

    items: dict[str, DItem] = {}
    sections: dict[str, dict[str, str]] = {}
    sources: dict[str, tuple[str, int]] = {}
    for u in upstream:
        skill = library.get(u.key)
        sources[u.key] = (u.title, u.version)
        if u.key == "low_level_design":
            sections[u.key] = {"Database design": "\n".join(section_reader(u.text, "Database design")).strip()}
        if skill is None or not skill.items:
            continue
        label = f"{u.title} v{u.version}" if u.version else u.title
        for it in parse_items(u.text, skill.items).items:
            if it.text.lstrip().startswith("Withdrawn"):
                continue
            items.setdefault(it.id, DItem(it.id, it.text.strip(), tuple(d for d in it.derives if _ID.fullmatch(d)), label))
    return Design(items, sections, sources)


# ---- the backend: APIs grouped by the component that owns them ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Api:
    item: DItem
    method: str
    path: str
    owner: str | None

    @property
    def id(self) -> str:
        return self.item.id

    @property
    def head(self) -> str:
        return f"{self.method} {self.path}"


def api_of(item: DItem) -> Api | None:
    m = _ENDPOINT.match(item.text.strip())
    if m is None:
        return None
    owner = re.search(r"\bOwner:\s*(COMP-\d{3})", item.text)
    if owner is not None:
        comp = owner.group(1)
    else:
        comp = next((d for d in item.derives if d.startswith("COMP-")), None)
    return Api(item, m.group(1), m.group(2).rstrip(".,;:"), comp)


_INPUT_FIRST = re.compile(r"^(takes|accepts|gets|needs|requires|uses|sends|returns|gives back)\b", re.IGNORECASE)
_NOT_A_DESCRIPTION = re.compile(r"^(error cases?|errors?)\b", re.IGNORECASE)
ABOUT_MAX = 110
ABOUT_SHORT = 45                # a first sentence shorter than this that only says what goes in is joined to the next one


def about_of(text: str) -> str:
    """A very short description of an API, in the design's own words: its first sentence, after the method, the path, the owner and the citations are taken away. When that sentence only says
    what the API takes in or gives back AND is short ('Takes a username and password'), the next sentence is joined to it unless it is about errors. Cut at a word near `ABOUT_MAX`
    characters. '' when there is nothing to say. Nothing is invented: every word is the design's."""
    t = re.sub(r"\bDerives\s+from\s*:.*$", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"\[[A-Z]{2,5}-\d{3}[^\]]*\]", "", t)
    t = re.sub(r"\bOwner:\s*COMP-\d{3}\.?", "", t)
    t = re.sub(r"^\s*(?:API-\d{3}\s+)?(?:GET|POST|PUT|PATCH|DELETE)\s+\S+?(?=\s|\.\s|\.$|$)\.?", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,;:])", r"\1", t).strip(" .")                 # 'action .' in the design reads 'action.'
    sentences = [s.strip() for s in re.split(r"(?<=[A-Za-z0-9\)])\.\s+", t) if s.strip(" .")]
    if not sentences:
        return ""
    out = sentences[0]
    if _INPUT_FIRST.match(out) and len(out) < ABOUT_SHORT and len(sentences) > 1 and not _NOT_A_DESCRIPTION.match(sentences[1]):
        out = f"{out}. {sentences[1]}"
    out = out.strip(" .")
    out = out[:1].upper() + out[1:]
    if len(out) > ABOUT_MAX:
        cut = out[:ABOUT_MAX].rsplit(" ", 1)[0].rstrip(" ,;:.-")
        out = cut + "…"
    return out


def apis_of(design: Design) -> list[Api]:
    return [a for a in (api_of(i) for i in design.of("API")) if a is not None]


@dataclass(frozen=True)
class Group:
    """One backend file: the APIs one component owns (or one part of them, when a component owns too many for one file)."""
    index: int
    comp: str | None
    title: str
    apis: tuple[Api, ...]
    part: tuple[int, int] = (1, 1)

    @property
    def name(self) -> str:
        return f"BE-{self.index:02d}-{slug(self.title)}.md"[:MAX_NAME]

    @property
    def key(self) -> str:
        return self.name

    @property
    def shown_title(self) -> str:
        return self.title if self.part[1] == 1 else f"{self.title} (part {self.part[0]} of {self.part[1]})"


def component_title(design: Design, comp: str | None) -> str:
    if comp is None:
        return "Other endpoints"
    item = design.get(comp)
    return short_title(item.text) if item is not None else comp


def groups_of(design: Design, max_apis: int) -> list[Group]:
    """One group per owning component, in the order of its first API; a component with more than `max_apis` APIs becomes several, kept in order, as even as possible."""
    apis = apis_of(design)
    by_owner: dict[str | None, list[Api]] = {}
    for a in apis:
        by_owner.setdefault(a.owner, []).append(a)
    ordered = sorted(by_owner.items(), key=lambda kv: kv[1][0].id)
    out: list[Group] = []
    for comp, members in ordered:
        title = component_title(design, comp)
        parts = max(1, math.ceil(len(members) / max(1, max_apis)))
        base, extra = divmod(len(members), parts)
        start = 0
        for k in range(parts):
            size = base + (1 if k < extra else 0)
            out.append(Group(len(out) + 1, comp, title, tuple(members[start:start + size]), (k + 1, parts)))
            start += size
    return out


def tables_of(group: Group, design: Design) -> list[DItem]:
    wanted = {d for a in group.apis for d in a.item.derives if d.startswith("TBL-")}
    return [t for t in design.of("TBL") if t.id in wanted]


def requirements_of(group: Group, design: Design, rules: Sequence[DItem]) -> list[DItem]:
    wanted = {d for a in group.apis for d in a.item.derives if d.startswith(("FR-", "NFR-"))} | {d for r in rules for d in r.derives if d.startswith(("FR-", "NFR-"))}
    return [design.items[i] for i in sorted(wanted) if i in design.items]


def assign_rules(groups: Sequence[Group], design: Design) -> tuple[dict[str, list[DItem]], list[DItem]]:
    """Which rules belong to which backend file, and which belong to none (those go in the start-here file, as shared rules).

    A rule scores against a group by what it names: an API of the group 10, the group's component 5, a table the group uses 2, a requirement the group serves 1. A rule goes to every
    group with the best score; a rule that scores nothing anywhere is shared."""
    info = []
    for g in groups:
        info.append((g, {a.id for a in g.apis}, {g.comp} if g.comp else set(), {t.id for t in tables_of(g, design)}, {d for a in g.apis for d in a.item.derives if d.startswith(("FR-", "NFR-"))}))
    by_group: dict[str, list[DItem]] = {g.key: [] for g in groups}
    shared: list[DItem] = []
    for rule in design.of("RUL"):
        named = set(rule.derives)
        scores = [(10 * len(named & apis) + 5 * len(named & comp) + 2 * len(named & tbls) + len(named & frs), g) for g, apis, comp, tbls, frs in info]
        best = max((s for s, _ in scores), default=0)
        if best <= 0:
            shared.append(rule)
            continue
        for s, g in scores:
            if s == best:
                by_group[g.key].append(rule)
    return by_group, shared


# ---- the frontend: screens and the APIs they call ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Screen:
    index: int
    item: DItem
    title: str

    @property
    def id(self) -> str:
        return self.item.id

    @property
    def name(self) -> str:
        return f"FE-{self.index:02d}-{slug(self.title)}.md"[:MAX_NAME]

    @property
    def key(self) -> str:
        return self.name

    @property
    def journey(self) -> list[str]:
        return [d for d in self.item.derives if d.startswith("JS-")]

    @property
    def low_fi(self) -> list[str]:
        """The low-fidelity screens a high-fidelity screen improves. A screen that IS a low-fidelity one has none: it is not its own low-fidelity layout."""
        return [d for d in self.item.derives if d.startswith("LFS-")] if self.item.prefix == "HFS" else []


SCREEN_PREFIXES = ("HFS", "LFS")        # the high-fidelity screens when the design has them, otherwise the low-fidelity ones (2026-11-23: the high-fidelity wireframes are off by default)


def screen_prefix(design: Design) -> str:
    """Which items are THE screens of this design: the high-fidelity ones (HFS) when there are any, else the low-fidelity ones (LFS). A project that has both is therefore exactly as it always was."""
    return next((p for p in SCREEN_PREFIXES if design.of(p)), SCREEN_PREFIXES[0])


def screens_of(design: Design) -> list[Screen]:
    return [Screen(n, i, short_title(i.text)) for n, i in enumerate(design.of(screen_prefix(design)), start=1)]


def candidate_apis(screen: Screen, apis: Sequence[Api]) -> list[str]:
    """The APIs that share a journey step with the screen: where the design itself links them. The model confirms and adds; it does not start from nothing."""
    steps = set(screen.journey)
    return [a.id for a in apis if steps & set(a.item.derives)]


_MAP_LINE = re.compile(r"^\s*[-*]\s*((?:HFS|LFS)-\d{3})\b[^:\n]*:\s*(.*)$")


def parse_map(text: str) -> dict[str, list[str]]:
    """{screen ID: its API IDs} from a screen-to-API map. 'none' (or nothing) is an empty list."""
    out: dict[str, list[str]] = {}
    for line in (text or "").split("\n"):
        m = _MAP_LINE.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = [i for i in ids_in(m.group(2)) if i.startswith("API-")]
    return out


def map_problems(mapping: Mapping[str, Sequence[str]], screens: Sequence[Screen], apis: Sequence[Api]) -> list[str]:
    """What is wrong with a screen-to-API map: a screen missing or repeated, a screen that is not in the design, an API that is not."""
    known_apis = {a.id for a in apis}
    wanted = [s.id for s in screens]
    out = [f"{s} has no line; write '- {s}: <API IDs>' (or 'none')" for s in wanted if s not in mapping]
    out += [f"{s} is not a screen of the design" for s in mapping if s not in wanted]
    for s, ids in mapping.items():
        bad = [i for i in ids if i not in known_apis]
        if bad:
            out.append(f"{s} names {', '.join(bad)}, which {'is' if len(bad) == 1 else 'are'} not in the design's APIs")
    return out


def render_map(mapping: Mapping[str, Sequence[str]], screens: Sequence[Screen], apis: Sequence[Api]) -> str:
    """The map file. The system writes it from the validated IDs: the person reads (and can change) one line per screen."""
    by_id = {a.id: a for a in apis}
    lines = ["# Screen to API map", "", "## Goal", "Which APIs each screen calls, so the frontend file of a screen and the backend files it depends on are built together. One line per screen.", "",
             "## Map"]
    for s in screens:
        chosen = [i for i in mapping.get(s.id, []) if i in by_id]
        lines.append(f"- {s.id} {s.title}: {', '.join(chosen) if chosen else 'none'}")
    lines += ["", "## Reading the map"]
    for s in screens:
        chosen = [i for i in mapping.get(s.id, []) if i in by_id]
        lines.append(f"### {s.id} {s.title}")
        lines += [f"- {i} {by_id[i].head}" for i in chosen] or ["- no API"]
    return "\n".join(lines) + "\n"


# ---- the files the system writes ------------------------------------------------------------------------------------------------------------------------

def strip_drawings(text: str) -> str:
    """The text of a design item without its fenced drawing: a coding agent reads the description, not the markup of a picture."""
    return re.sub(r"\n{3,}", "\n\n", _FENCE.sub("", text or "")).strip()


def _quote(item: DItem) -> str:
    return f"- {item.line}"


def backend_copy(group: Group, design: Design, rules: Sequence[DItem]) -> str:
    """Everything of a backend file that the system copies from the design: the APIs, their tables, the rules that apply and the requirements they serve."""
    tables = tables_of(group, design)
    reqs = requirements_of(group, design, rules)
    lines = ["## APIs to build"]
    for a in group.apis:
        lines += [f"### {a.id} {a.head}", a.item.text, f"Source: {a.item.source}", ""]
    lines += ["## Tables these APIs use"] + ([_quote(t) for t in tables] or ["None named by these APIs."]) + [""]
    lines += ["## Rules that apply"] + ([_quote(r) for r in rules] or ["None specific to these APIs; the shared rules in 00-start-here.md apply."]) + [""]
    lines += ["## Requirements these APIs serve"] + ([_quote(r) for r in reqs] or ["None named."])
    return "\n".join(lines).rstrip("\n") + "\n"


def backend_derives(group: Group, design: Design, rules: Sequence[DItem]) -> list[str]:
    out = [a.id for a in group.apis] + [t.id for t in tables_of(group, design)] + [r.id for r in rules] + [r.id for r in requirements_of(group, design, rules)]
    if group.comp and design.has(group.comp):
        out.insert(0, group.comp)
    return list(dict.fromkeys(out))


def frontend_copy(screen: Screen, design: Design, called: Sequence[Api], groups: Sequence[Group], rules: Sequence[DItem]) -> str:
    """Everything of a frontend file that the system copies: the screen's design, the APIs it calls (and which backend file builds each), the journey steps and the rules shown."""
    file_of = {a.id: g.name for g in groups for a in g.apis}
    lines = ["## The screen", strip_drawings(screen.item.text), f"Source: {screen.item.source}", ""]
    low = [design.items[i] for i in screen.low_fi if i in design.items]
    if low:
        lines += ["## Low-fidelity layout"] + [_quote(i) for i in low] + [""]
    lines += ["## APIs this screen calls", "| API | Method and path | Built in |", "|---|---|---|"]
    lines += [f"| {a.id} | {a.head} | {file_of.get(a.id, 'not in a backend file')} |" for a in called] or ["| none | | |"]
    lines += ["", "## Journey steps this screen serves"] + ([_quote(design.items[i]) for i in screen.journey if i in design.items] or ["None named."]) + [""]
    lines += ["## Rules shown on this screen"] + ([_quote(r) for r in rules] or ["None named by the APIs it calls."])
    return "\n".join(lines).rstrip("\n") + "\n"


def frontend_rules(called: Sequence[Api], rules_by_group: Mapping[str, Sequence[DItem]], groups: Sequence[Group]) -> list[DItem]:
    """The rules that apply to the APIs a screen calls (those it will show messages for), without repeats, in ID order."""
    wanted = {a.id for a in called}
    out: dict[str, DItem] = {}
    for g in groups:
        if not wanted & {a.id for a in g.apis}:
            continue
        for r in rules_by_group.get(g.key, ()):
            if set(r.derives) & wanted:
                out[r.id] = r
    return [out[k] for k in sorted(out)]


def frontend_derives(screen: Screen, called: Sequence[Api], rules: Sequence[DItem], design: Design) -> list[str]:
    out = [screen.id] + [i for i in screen.low_fi + screen.journey if design.has(i)] + [a.id for a in called] + [r.id for r in rules]
    return list(dict.fromkeys(out))


def compose_file(title: str, notes: str, copied: str, derives: Sequence[str], intro: str) -> str:
    """One brief file: a title, a line saying what it is for, the model's build notes (when there are any), what the system copied, and its traceability."""
    notes_block = notes.strip() or "Not written yet."
    return (f"# {title}\n\n## Goal\n{intro}\n\n## Build notes\n{notes_block}\n\n{copied.rstrip()}\n\n## Traceability\nDerives from: {', '.join(derives)}\n")


def notes_of(text: str) -> str:
    """The 'Build notes' section of a saved brief file."""
    m = re.search(r"^## Build notes\n(.*?)(?=^## |\Z)", text or "", re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def backend_intro(group: Group) -> str:
    return (f"Build the backend for {group.shown_title}: {len(group.apis)} API{'s' if len(group.apis) != 1 else ''}, owned by {group.comp or 'no named component'}. "
            "Read 00-start-here.md first for the stack, the layout and the rules every file follows. The APIs, tables and rules below are the design's own words; build them as written.")


def frontend_intro(screen: Screen) -> str:
    return (f"Build the screen {screen.id} {screen.title}. Read 00-start-here.md first. The screen calls only the APIs listed below, which are built in the backend files named beside them. "
            "Describe nothing the design does not say; where it is silent, follow 00-start-here.md.")


# ---- the traceability file and the checklist (no model) ----------------------------------------------------------------------------------------------

TRACED = ("UC", "API", "TBL", "RUL", "HFS", "LFS", "NFR")            # LFS is listed only for a design that has no HFS (below), so a project with both is traced as it always was


def traceability(files: Mapping[str, str], design: Design) -> str:
    """Every design ID against the file that builds it, and every one no file holds, listed as not built. Computed from the files, so it cannot disagree with them."""
    where: dict[str, list[str]] = {}
    for name, text in files.items():
        if name in (TRACE_NAME, CHECK_NAME, MAP_NAME):
            continue
        for i in ids_in(text):
            where.setdefault(i, [])
            if name not in where[i]:
                where[i].append(name)
    lines = ["# Traceability", "", "## Goal", "Every item of the design against the file of this brief that holds it. An item no file holds is listed under Not built.", ""]
    missing: list[str] = []
    for prefix in TRACED:
        if prefix in SCREEN_PREFIXES and prefix != screen_prefix(design):
            continue                                          # the screens are the high-fidelity ones, or (when there are none) the low-fidelity ones: never both in one list
        items = design.of(prefix)
        if not items:
            continue
        lines.append(f"## {prefix}")
        for it in items:
            found = where.get(it.id)
            if found:
                lines.append(f"- {it.id}: {', '.join(found)}")
            else:
                missing.append(it.id)
        lines.append("")
    lines += ["## Not built"] + ([f"- {i}: no file of this brief holds it yet" for i in missing] or ["None"])
    return "\n".join(lines).rstrip("\n") + "\n"


def checklist(groups: Sequence[Group], screens: Sequence[Screen], mapping: Mapping[str, Sequence[str]], written: Iterable[str]) -> str:
    """A tick-box list, one block per backend file and per screen, made from the design's own APIs. A person ticks it, or a coding agent works down it and reports."""
    have = set(written)
    lines = ["# Checklist", "", "## Goal", "Tick each box when the thing exists and does what the design says. A coding agent: work down the list and report any box you could not tick, with the reason.", ""]
    lines.append("## Backend")
    for g in groups:
        lines.append(f"### {g.name}" + ("" if g.name in have else " (not written yet)"))
        lines += [f"- [ ] {a.id} {a.head}" for a in g.apis]
    lines += ["", "## Frontend"]
    for s in screens:
        lines.append(f"### {s.name}" + ("" if s.name in have else " (not written yet)"))
        lines.append(f"- [ ] {s.id} {s.title} shows what the design says")
        lines += [f"- [ ] calls {i}" for i in mapping.get(s.id, [])]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---- the saved set: files in one body ---------------------------------------------------------------------------------------------------------------------

def split_set(body: str) -> dict[str, str]:
    """The files in a saved brief. Each file's text runs from its mark to the next; never raises."""
    files: dict[str, str] = {}
    current: str | None = None
    chunk: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        mark = FILE_MARK.match(line)
        if mark is None:
            if current is not None:
                chunk.append(line)
            continue
        if current is not None and current not in files:
            files[current] = "\n".join(chunk).strip("\n") + "\n"
        current, chunk = mark.group(1), []
    if current is not None and current not in files:
        files[current] = "\n".join(chunk).strip("\n") + "\n"
    return files


def order_key(name: str) -> tuple[int, str]:
    if name == START_NAME:
        return (0, name)
    if name == MAP_NAME:
        return (1, name)
    if _BE_NAME.match(name):
        return (2, name)
    if _FE_NAME.match(name):
        return (3, name)
    if name == TRACE_NAME:
        return (4, name)
    if name == CHECK_NAME:
        return (5, name)
    return (9, name)


def kind_of(name: str) -> str | None:
    return {START_NAME: "start", MAP_NAME: "map", TRACE_NAME: "trace", CHECK_NAME: "checklist"}.get(name) or ("backend" if _BE_NAME.match(name) else "frontend" if _FE_NAME.match(name) else None)


def head_ids(body: str) -> list[tuple[str, str]]:
    """(ID, rest of line) for every file the saved brief lists, withdrawn ones included, so a rebuilt brief keeps each file's number."""
    head = (body or "").split("=== FILE:", 1)[0]
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^\s*[-*]\s*(BRF-\d{3,})\s+(.*)$", head, re.MULTILINE)]


def assemble(files: Mapping[str, str], derives: Mapping[str, Sequence[str]], overview: str, *, previous: Iterable[tuple[str, str]] = (), highest: int = 0, gaps: Sequence[str] = ()) -> str:
    """The saved body: an overview, the list of files as numbered items (so the checks, links and traceability of every artefact apply), the gaps, then each file after its mark.

    A file keeps the number it had; a new one takes the next after `highest` and after every number ever used; a file that is gone is listed under Withdrawn items."""
    ordered = sorted(files, key=order_key)
    by_name: dict[str, str] = {}
    for item_id, rest in previous:
        name = rest.split(":", 1)[0].strip(" `")
        if name:
            by_name[name] = item_id
    ever = [highest] + [int(i.split("-")[1]) for i in by_name.values()]
    next_number = max(ever) + 1
    ids: dict[str, str] = {}
    for name in ordered:
        if name in by_name:
            ids[name] = by_name[name]
        else:
            ids[name] = f"{ITEM_PREFIX}-{next_number:03d}"
            next_number += 1
    out = ["## Overview", overview.strip() or f"{len(ordered)} file(s) for a coding agent.", "", "## Files"]
    for name in ordered:
        d = [i for i in derives.get(name, ()) if i]
        out.append(f"- {ids[name]} {name}: {title_of(files[name]) or 'brief'}." + (f" Derives from: {', '.join(d)}" if d else ""))
    out += ["", "## Coverage check"] + ([f"- {g}" for g in gaps] or ["None"])
    gone = sorted((n, i) for n, i in by_name.items() if n not in files)
    if gone:
        out += ["", "## Withdrawn items"] + [f"- {i} {n}: this file is no longer part of the set." for n, i in gone]
    out += ["", "## Instructions"]
    for name in ordered:
        out += [f"=== FILE: {name} ===", files[name].strip("\n"), ""]
    return "\n".join(out).rstrip("\n") + "\n"


# ---- what is saved, missing or older ------------------------------------------------------------------------------------------------------------------

SAVED, MISSING, OLDER = "saved", "missing", "older"


@dataclass(frozen=True)
class SliceRow:
    name: str
    kind: str                  # start | map | backend | frontend
    title: str
    state: str                 # saved | missing | older
    count: int = 0             # APIs in a backend file, APIs called by a screen
    why: str = ""


_SOURCE_LINE = re.compile(r"^Source: .*$", re.MULTILINE)


def _content(text: str) -> str:
    """What was copied, without the 'Source: Low-level design v2' labels: a new VERSION of the design changes every label, and must not make a file older when its own items did not change."""
    return _SOURCE_LINE.sub("", text)


def stamp_backend(group: Group, design: Design, rules: Sequence[DItem]) -> str:
    return fingerprint(_content(backend_copy(group, design, rules)) + "|" + ",".join(backend_derives(group, design, rules)))


def stamp_frontend(screen: Screen, design: Design, called: Sequence[Api], groups: Sequence[Group], rules: Sequence[DItem]) -> str:
    return fingerprint(_content(frontend_copy(screen, design, called, groups, rules)))


def stamp_plan(design: Design) -> str:
    """What the start-here file and the map were planned from: the APIs, the screens and the components. When any of them changes, the plan is older."""
    apis = "\n".join(f"{a.id} {a.head} {a.owner}" for a in apis_of(design))
    screens = "\n".join(f"{s.id} {s.title}" for s in screens_of(design))
    comps = "\n".join(i.line for i in design.of("COMP"))
    return fingerprint(apis + "\n" + screens + "\n" + comps)


def slice_rows(design: Design, saved: Mapping[str, str], stamps: Mapping[str, str], max_apis: int) -> list[SliceRow]:
    """Every file of the brief as it stands: the plan (start-here and the map), each backend file, each frontend file; saved, missing, or older than the design it was copied from."""
    groups = groups_of(design, max_apis)
    screens = screens_of(design)
    apis = apis_of(design)
    rules_by_group, _shared = assign_rules(groups, design)
    mapping = parse_map(saved.get(MAP_NAME, ""))
    by_id = {a.id: a for a in apis}
    rows: list[SliceRow] = []
    plan_now = stamp_plan(design)
    for name, kind, title in ((START_NAME, "start", "Start here"), (MAP_NAME, "map", "Screen to API map")):
        if name not in saved:
            rows.append(SliceRow(name, kind, title, MISSING))
        elif stamps.get(name) and stamps[name] != plan_now:
            rows.append(SliceRow(name, kind, title, OLDER, 0, "the design's APIs, screens or components have changed since it was written"))
        else:
            rows.append(SliceRow(name, kind, title, SAVED))
    for g in groups:
        rules = rules_by_group.get(g.key, [])
        if g.name not in saved:
            rows.append(SliceRow(g.name, "backend", g.shown_title, MISSING, len(g.apis)))
        elif stamps.get(g.name) and stamps[g.name] != stamp_backend(g, design, rules):
            rows.append(SliceRow(g.name, "backend", g.shown_title, OLDER, len(g.apis), "the design it was copied from has changed"))
        else:
            rows.append(SliceRow(g.name, "backend", g.shown_title, SAVED, len(g.apis)))
    for s in screens:
        called = [by_id[i] for i in mapping.get(s.id, []) if i in by_id]
        rules = frontend_rules(called, rules_by_group, groups)
        if s.name not in saved:
            rows.append(SliceRow(s.name, "frontend", f"{s.id} {s.title}", MISSING, len(called)))
        elif stamps.get(s.name) and stamps[s.name] != stamp_frontend(s, design, called, groups, rules):
            rows.append(SliceRow(s.name, "frontend", f"{s.id} {s.title}", OLDER, len(called), "the design it was copied from, or the map, has changed"))
        else:
            rows.append(SliceRow(s.name, "frontend", f"{s.id} {s.title}", SAVED, len(called)))
    return rows


# ---- the categories a person builds, one at a time ------------------------------------------------------------------------------------------------------

PLAN, BACKEND, FRONTEND = "plan", "backend", "frontend"
CATEGORY_ORDER = (PLAN, BACKEND, FRONTEND)
CATEGORY_LABELS = {PLAN: "Plan (start-here and screen-to-API map)", BACKEND: "Backend (one file per API group)", FRONTEND: "Frontend (one file per screen)"}
CATEGORY_NEEDS = {PLAN: (), BACKEND: (PLAN,), FRONTEND: (PLAN,)}


@dataclass(frozen=True)
class CatRow:
    key: str
    label: str
    files: tuple[SliceRow, ...]
    needs: tuple[str, ...] = ()
    blocked: str | None = None

    @property
    def saved(self) -> int:
        return sum(1 for f in self.files if f.state in (SAVED, OLDER))

    @property
    def missing(self) -> int:
        return sum(1 for f in self.files if f.state == MISSING)

    @property
    def older(self) -> int:
        return sum(1 for f in self.files if f.state == OLDER)

    @property
    def complete(self) -> bool:
        return bool(self.files) and self.missing == 0

    def to_write(self, rewrite: bool) -> tuple[SliceRow, ...]:
        return self.files if rewrite else tuple(f for f in self.files if f.state != SAVED)


def categories_from(rows: Sequence[SliceRow]) -> list[CatRow]:
    """The categories of the brief as it stands, from its files: each saved, missing or older, and whether the category can be built yet. A design with no screens has no frontend
    category and its plan is the start-here file alone; a design with no APIs has no backend category."""
    plan = tuple(r for r in rows if r.kind in ("start", "map"))
    be = tuple(r for r in rows if r.kind == "backend")
    fe = tuple(r for r in rows if r.kind == "frontend")
    out = [CatRow(PLAN, CATEGORY_LABELS[PLAN], plan, CATEGORY_NEEDS[PLAN])]
    if be:
        out.append(CatRow(BACKEND, CATEGORY_LABELS[BACKEND], be, CATEGORY_NEEDS[BACKEND]))
    if fe:
        out.append(CatRow(FRONTEND, CATEGORY_LABELS[FRONTEND], fe, CATEGORY_NEEDS[FRONTEND]))
    by_key = {c.key: c for c in out}
    final: list[CatRow] = []
    for c in out:
        unmet = [by_key[n] for n in c.needs if n in by_key and not by_key[n].complete]
        short = {PLAN: "the plan"}
        blocked = ("write " + " and ".join(short.get(u.key, u.label.lower()) for u in unmet) + " first") if unmet else None
        final.append(CatRow(c.key, c.label, c.files, c.needs, blocked))
    return final


def category_problems(chosen: Iterable[str], rows_now: Sequence[CatRow], rewrite: bool) -> list[str]:
    """Why a run of these categories cannot start, in words, before any model call."""
    wanted = list(dict.fromkeys(chosen))
    by_key = {c.key: c for c in rows_now}
    if not wanted:
        return ["no category was chosen"]
    out: list[str] = []
    for key in wanted:
        if key not in CATEGORY_LABELS:
            out.append(f"'{key}' is not a category of the build brief; the categories are {', '.join(CATEGORY_ORDER)}")
        elif key not in by_key:
            out.append(f"the design has nothing for '{key}' to build ('{key}' has no files)")
    if out:
        return out
    earlier: set[str] = set()
    idle: list[CatRow] = []
    for key in [k for k in CATEGORY_ORDER if k in wanted]:
        row = by_key[key]
        for need in CATEGORY_NEEDS[key]:
            if need in by_key and not by_key[need].complete and need not in earlier:
                out.append(f"'{row.label}' needs the plan first: tick it as well, or write it before")
        if not row.to_write(rewrite):
            idle.append(row)
        earlier.add(key)
    # A ticked category with nothing to write is SKIPPED when another ticked category has work; a run where nothing at all would be written is refused, in words.
    if len(idle) == len([k for k in wanted if k in by_key]):
        out += [f"'{row.label}' has nothing to write: every file is saved. Tick 'write these again' to replace {'it' if len(row.files) == 1 else 'them'}" for row in idle]
    return out


# ---- what the set still lacks ----------------------------------------------------------------------------------------------------------------------------

_UNWRITTEN = re.compile(r"^\s*[-*]\s+(\S+\.md)\s+has not been written yet", re.MULTILINE)


def missing_files(body: str) -> list[str]:
    """The files a saved brief says it does not hold yet, from its own Coverage check (the system writes that list)."""
    head = (body or "").split("=== FILE:", 1)[0]
    m = re.search(r"^## Coverage check\n(.*?)(?=^## |\Z)", head, re.DOTALL | re.MULTILINE)
    return _UNWRITTEN.findall(m.group(1)) if m else []


# ---- a person changing ONE file of a saved brief --------------------------------------------------------------------------------------------------------

class FileEditRefused(ValueError):
    """An edit of one file was refused. `problems` are the reasons, in plain sentences; the text the person typed is theirs to keep."""

    def __init__(self, message: str, problems: Sequence[str]):
        super().__init__(message)
        self.problems = tuple(problems)


EDITABLE = ("start", "map", "backend", "frontend")


def splice_file(body: str, name: str, new_text: str, design: Design, *, highest: int = 0, anchor: str = "") -> str:
    """Replace ONE file of a saved brief and rebuild the rest around it: every other file is kept byte for byte, every file keeps its number, and the traceability list, the checklist and
    the file list are rebuilt from the files (no model). The traceability and checklist files cannot be edited by hand: they are written by the system from the other files."""
    files = split_set(body)
    if name not in files:
        raise FileEditRefused(f"'{name}' is not a file of this brief", [f"'{name}' is not a file of this brief; the files are: {', '.join(sorted(files)[:10])}"])
    kind = kind_of(name)
    if kind not in EDITABLE:
        raise FileEditRefused(f"'{name}' cannot be edited by hand", [f"'{name}' is written by the system from the other files and cannot be edited by hand"])
    text = (new_text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise FileEditRefused("the edited text is empty", ["the edited text is empty"])
    if any(FILE_MARK.match(line) for line in text.split("\n")):
        raise FileEditRefused("the text has a '=== FILE:' line", ["the text has a line that starts '=== FILE:'; that line belongs to the set, so take it out"])
    problems: list[str] = []
    if not re.search(r"^#\s+\S", text, re.MULTILINE):
        problems.append("start the file with a '# <title>' line")
    if kind == "map" and (bad := map_problems(parse_map(text), screens_of(design), apis_of(design))):
        problems += bad
    if problems:
        raise FileEditRefused("the edit was not saved: " + "; ".join(problems[:6]), problems)
    files[name] = text + "\n"
    previous = head_ids(body)
    groups_unknown = None  # the groups are not needed to rebuild the traceability list
    rest = {n: t for n, t in files.items() if n not in (TRACE_NAME, CHECK_NAME)}
    files[TRACE_NAME] = traceability(rest, design)
    mapping = parse_map(files.get(MAP_NAME, ""))
    files[CHECK_NAME] = _checklist_from_files(files, design, mapping)
    derives = {n: ids_in((re.search(r"^Derives from:\s*(.*)$", t, re.MULTILINE) or [None, ""])[1]) if (re.search(r"^Derives from:", t, re.MULTILINE)) else [] for n, t in files.items()}
    for n in files:
        if not derives.get(n) and anchor:
            derives[n] = [anchor]
    gaps = [g for g in head_gaps(body)]
    return assemble(files, derives, head_overview(body), previous=previous, highest=max(highest, max([int(i.split("-")[1]) for i, _ in previous] or [0])), gaps=gaps)


def head_overview(body: str) -> str:
    m = re.search(r"^## Overview\n(.*?)(?=^## )", (body or "").split("=== FILE:", 1)[0], re.DOTALL | re.MULTILINE)
    return m.group(1).strip() if m else ""


def head_gaps(body: str) -> list[str]:
    head = (body or "").split("=== FILE:", 1)[0]
    m = re.search(r"^## Coverage check\n(.*?)(?=^## |\Z)", head, re.DOTALL | re.MULTILINE)
    return [ln.strip().lstrip("-* ").strip() for ln in (m.group(1).split("\n") if m else []) if ln.strip().startswith(("-", "*"))]


def _checklist_from_files(files: Mapping[str, str], design: Design, mapping: Mapping[str, Sequence[str]]) -> str:
    """The checklist of a brief from the files it holds (used when one file is edited and no group list is to hand): backend files by their APIs, frontend files by their calls."""
    lines = ["# Checklist", "", "## Goal", "Tick each box when the thing exists and does what the design says. A coding agent: work down the list and report any box you could not tick, with the reason.", "", "## Backend"]
    for name in sorted((n for n in files if kind_of(n) == "backend"), key=order_key):
        lines.append(f"### {name}")
        lines += [f"- [ ] {i}" for i in ids_in(files[name]) if i.startswith("API-")]
    lines += ["", "## Frontend"]
    for name in sorted((n for n in files if kind_of(n) == "frontend"), key=order_key):
        lines.append(f"### {name}")
        screen = next((i for i in ids_in(files[name]) if i.startswith(SCREEN_PREFIXES)), None)
        if screen:
            lines.append(f"- [ ] {screen} shows what the design says")
            lines += [f"- [ ] calls {i}" for i in mapping.get(screen, [])]
    return "\n".join(lines).rstrip("\n") + "\n"
