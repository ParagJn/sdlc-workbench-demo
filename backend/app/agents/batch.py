"""Writing one artefact in several parts (2026-11-10): for a backlog too big for one reply.

User stories are written for a feature at a time. A real project had 31 features: two replies hit the 8,000-token output limit and the third dropped three features, so nothing could
be saved. The runtime now splits the features into even groups and asks for the stories of one group per call, then joins the parts into ONE document and checks the whole of it
exactly as a single reply is checked. This module holds the pure steps (no model, no database): where to split, which earlier stories belong to which part, what each part is told,
and how the parts are joined.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from .items import Item, leading_id
from .reply import section_bodies, section_names, unfenced_lines

BATCHED_SKILLS = frozenset({"user_stories", "lowfi_wireframes", "hifi_wireframes"})          # the skills written in parts when what they cover does not fit one call

# What each batched skill writes and what it covers, for the sentences of a part's note and of the joined Overview: (the items written, the covered items), and which setting says
# how many covered items one call takes. Stories cover features; the low-fidelity wireframes cover journey steps; the high-fidelity ones cover the low-fidelity screens (S42).
PART_NOUNS: dict[str, tuple[str, str]] = {
    "user_stories": ("stories", "features"),
    "lowfi_wireframes": ("screens", "journey steps"),
    "hifi_wireframes": ("screens", "low-fidelity screens"),
}
PART_SIZE_SETTING = {"user_stories": "agent_batch_features", "lowfi_wireframes": "agent_batch_steps", "hifi_wireframes": "agent_batch_screens"}

# S41: a skill written in STAGES. Stage 1 writes the bounded, shared part once (here: the tables and how they relate); the later parts, one per group of covered items, write the part
# that grows with the requirements (here: the endpoints and the rules) and are shown the finished stage 1. Keyed by skill: (the sections of stage 1, the item prefixes of stage 1,
# the sections written in every later part, the item prefixes of the later parts).
STAGED_SKILLS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "low_level_design": (("Overview", "Tables", "Database design", "Open questions"), ("TBL",), ("Overview", "Endpoints", "Rules", "Coverage check", "Open questions"), ("API", "RUL")),
}

# The same staging for a project of kind `automation` (S40 part 2c): the FILES are written once (stage 1), then the scripts and rules in parts of requirements.
STAGED_AUTOMATION: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "low_level_design": (("Overview", "Files", "Open questions"), ("FIL",), ("Overview", "Scripts", "Rules", "Coverage check", "Open questions"), ("SCR", "RUL")),
}
STAGED_BY_KIND = {"software": STAGED_SKILLS, "automation": STAGED_AUTOMATION}

# What each item kind is called in a sentence, and what the first stage is called in a label.
NOUN = {"API": "endpoints", "TBL": "tables", "RUL": "rules", "SCR": "scripts", "FIL": "files"}
STAGE_ONE_LABEL = {"TBL": "the tables and how they relate", "FIL": "the files"}


def staging_for(skill_key: str, kind: str = "software"):
    """(stage-1 sections, stage-1 item kinds, later sections, later item kinds) for a skill of this kind of project, or None when it is written in one call."""
    return STAGED_BY_KIND.get(kind, {}).get(skill_key)


def stage_overview(first_kinds: Sequence[str], later_kinds: Sequence[str], counts: Mapping[str, int], requirements: int, stages: int, parts: int, size: int) -> str:
    """The one-sentence Overview of a joined staged document: how many of each kind follow, and how it was written. For the software Low-level design it reads exactly as it always has."""
    def n(prefix: str) -> str:
        return f"{counts.get(prefix, 0)} {NOUN.get(prefix, prefix)}"

    listed = [later_kinds[0], first_kinds[0], *later_kinds[1:]]
    head = ", ".join(n(p) for p in listed[:-1]) + f" and {n(listed[-1])}" if len(listed) > 1 else n(listed[0])
    first_noun = NOUN.get(first_kinds[0], first_kinds[0])
    later_nouns = " and ".join(NOUN.get(p, p) for p in later_kinds)
    return (f"{head} follow for {requirements} requirement{'s' if requirements != 1 else ''}. "
            f"They were written in {stages} stages and joined: the {first_noun} first, then the {later_nouns} in {parts} part{'s' if parts != 1 else ''}, "
            f"each for at most {size} requirement{'s' if size != 1 else ''}.")


_TOP_LEVEL = re.compile(r"^ ?(?:[-*+]|\d{1,3}[.)])[ \t]+(?P<rest>\S.*)$")
_NONE = re.compile(r"^\W*none\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class Part:
    """One call's share of an artefact."""
    index: int                        # 1-based
    count: int
    features: tuple[str, ...]         # the covered items (FEAT-...) this part writes for
    current: frozenset[str]           # IDs from the previous version that belong to this part
    highest: int                      # the highest item number used before this part, so numbering runs on
    draft: str | None                 # the previous version's items for this part, as written


def split_evenly(ids: Sequence[str], size: int) -> list[tuple[str, ...]]:
    """The IDs in as few groups as `size` allows, as even as possible (31 and 10 give 8, 8, 8, 7), keeping their order. Nothing gives no groups."""
    if size < 1:
        raise ValueError("a part holds at least one feature")
    if not ids:
        return []
    groups = math.ceil(len(ids) / size)
    base, extra = divmod(len(ids), groups)
    out: list[tuple[str, ...]] = []
    start = 0
    for k in range(groups):
        step = base + (1 if k < extra else 0)
        out.append(tuple(ids[start:start + step]))
        start += step
    return out


def item_blocks(body: str, section: str) -> list[tuple[str | None, str]]:
    """(ID, the whole entry as written) for each top-level list entry of a section, nested bullets included. The ID is None for an entry that has none."""
    blocks: list[list[str]] = []
    for chunk in section_bodies(body, section):
        for line in unfenced_lines(chunk):
            if _TOP_LEVEL.match(line):
                blocks.append([line.rstrip()])
            elif blocks and line.strip():
                blocks[-1].append(line.rstrip())
    out: list[tuple[str | None, str]] = []
    for lines in blocks:
        first = _TOP_LEVEL.match(lines[0])
        out.append((leading_id(first.group("rest")) if first else None, "\n".join(lines)))
    return out


def assign_previous(items: Sequence[Item], parts: Sequence[Sequence[str]]) -> list[frozenset[str]]:
    """Which part each item of the previous version belongs to: the part that holds the first feature it derives from. An item whose features are no longer
    among the parts goes to the LAST part, so a model is shown it and can withdraw it."""
    owner = {feature: k for k, features in enumerate(parts) for feature in features}
    out: list[set[str]] = [set() for _ in parts]
    for item in items:
        k = next((owner[d] for d in item.derives if d in owner), len(parts) - 1)
        out[k].add(item.id)
    return [frozenset(s) for s in out]


def draft_for(previous_body: str | None, section: str, wanted: frozenset[str]) -> str | None:
    """The previous version's entries for one part, under their section heading; None when it has none."""
    if not previous_body:
        return None
    blocks = [text for item_id, text in item_blocks(previous_body, section) if item_id in wanted]
    return f"## {section}\n" + "\n".join(blocks) if blocks else None


def part_note(part: Part, what: str = "features") -> str:
    """What a part's prompt adds: which covered items (features, by default) are its own, and how its numbering and overview fit into the joined document."""
    return (
        f"# This is part {part.index} of {part.count}\n"
        f"The artefact is written in parts and joined afterwards. Write items ONLY for the {what} listed here; the other parts cover the other {what}.\n"
        f"{what.capitalize()} for this part: {', '.join(part.features)}.\n"
        "Number your new items upward from the number given under Numbering, so the parts join into one list. "
        f"Write every required section. In Overview write one short sentence naming the themes of these {what}; it is replaced by an overview of the whole document when the parts are joined, so do not mention parts or a total."
    )


@dataclass(frozen=True)
class StageCall:
    """What one call of a staged artefact needs beyond the skill (S41): the note that says where it sits, the covered items it is responsible for (None for stage 1), the previous
    version's share, the covered pool / earlier IDs / numbering floor that narrow its item rules (None keeps the whole's), and the finished stage-1 items it may name in 'Derives from'."""
    text: str
    scope: tuple[str, ...] | None
    draft: str | None
    pools: Mapping[str, tuple[str, ...]] | None = None
    current: frozenset[str] | None = None
    highest: Mapping[str, int] | None = None
    finished_items: tuple = ()


def part_note_staged(index: int, count: int, features: Sequence[str], prefixes: Sequence[str]) -> str:
    """What a LATER part's prompt adds: which covered items are its own, and that numbering and the overview fit into the joined document."""
    kinds = " and ".join(prefixes)
    return (
        f"# This is part {index} of {count}\n"
        f"The document is written in parts and joined afterwards. Write {kinds} items ONLY for the items listed here; the other parts cover the others.\n"
        f"Items for this part: {', '.join(features)}.\n"
        "Number your new items upward from the numbers given under Numbering, so the parts join into one list of each kind. "
        "Write every required section. In Overview write one short sentence naming the themes of these items; it is replaced by an overview of the whole document when the parts are joined, so do not mention parts or a total."
    )


def draft_of_sections(previous_body: str | None, names: Sequence[str]) -> str | None:
    """The previous version's text of the named sections, under their headings, for a stage that rewrites them whole. None when it has none of them."""
    if not previous_body:
        return None
    blocks = []
    for name in names:
        text = "\n".join(section_bodies(previous_body, name)).strip()
        if text:
            blocks.append(f"## {name}\n{text}")
    return "\n\n".join(blocks) or None


def draft_for_sections(previous_body: str | None, sections: Sequence[str], wanted: frozenset[str]) -> str | None:
    """The previous version's entries (with their nested lines) for one part, under each of their section headings; None when it has none."""
    if not previous_body:
        return None
    blocks = []
    for name in sections:
        entries = [text for item_id, text in item_blocks(previous_body, name) if item_id in wanted]
        if entries:
            blocks.append(f"## {name}\n" + "\n".join(entries))
    return "\n\n".join(blocks) or None


def foreign_sections(body: str, own: Sequence[str], others: Sequence[str]) -> list[str]:
    """The sections of `body` that belong to ANOTHER stage (S41): a stage may write only its own. Plain sentences to send back, [] when it kept to its own.

    Without this a part could add a Tables section, or stage 1 an Endpoints one, and the joined document would silently take one stage's text over another's."""
    mine = {" ".join(n.lower().split()) for n in own}
    found = []
    for name in section_names(body):
        key = " ".join(name.lower().split())
        if key not in mine and any(key == " ".join(o.lower().split()) for o in others) and name not in found:
            found.append(name)
    return [f"the '{name}' section belongs to another stage of this document; write only {', '.join(repr(n) for n in own)}" for name in found]


def narrowed_skill(skill, sections: Sequence[str], prefixes: Sequence[str], covers: bool):
    """The same skill seen by ONE stage of a staged artefact (S41): only that stage's required sections, only its item kinds, and the coverage rule only when this stage writes the
    items that cover (the later parts). The prompt, the item rules and every check read these three, so they follow without being told."""
    from dataclasses import replace

    items = tuple(spec for spec in skill.items if spec.prefix in prefixes)
    return replace(skill, required_sections=tuple(sections), items=items, covers=tuple(skill.covers) if covers else ())


def stage_one_note(prefixes: Sequence[str], later: Sequence[str]) -> str:
    """What the FIRST stage's prompt adds: it writes the shared, bounded part once, and nothing that a later stage writes."""
    return (
        "# This is stage 1 of a document written in stages\n"
        f"The document is written in stages and joined afterwards. In this stage write ONLY the {' and '.join(prefixes)} items and the sections listed above. "
        f"Do not write any {' or '.join(later)} items: later stages write them, and they will be shown the {' and '.join(prefixes)} items you write here, so make them complete and consistent.\n"
        "In Overview write one short sentence about what this document covers; it is replaced by an overview of the whole document when the stages are joined, so do not mention stages."
    )


def finished_stage_note(finished_text: str, prefixes: Sequence[str]) -> str:
    """What every LATER part is shown: the finished first stage, to reuse by ID and name and never to redefine."""
    return (
        f"# The {' and '.join(prefixes)} items already written (stage 1, finished)\n"
        f"Use these exactly as they are: name them by ID in 'Derives from:' and refer to them by the names written here. Do not write, change or repeat any {' or '.join(prefixes)} item.\n\n"
        + finished_text.strip()
    )


_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}[ \t]+(?P<name>.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE_LINE = re.compile(r"^\s{0,3}(```|~~~)")


def split_at_heading(text: str) -> tuple[str, str]:
    """(the text before its first heading, the rest from that heading on). A section's text includes its sub-sections; a part's list must be joined to the next part's
    list WITHOUT a sub-heading in between, or the next part's entries would end up under it."""
    lines = text.split("\n")
    fence: str | None = None
    for number, line in enumerate(lines):
        fenced = _FENCE_LINE.match(line)
        if fenced:
            fence = fenced.group(1) if fence is None else (None if fenced.group(1) == fence else fence)
        elif fence is None and _HEADING_LINE.match(line):
            return "\n".join(lines[:number]), "\n".join(lines[number:])
    return text, ""


def _is_withdrawn_heading(tail: str) -> bool:
    """Whether `tail` opens with a 'Withdrawn items' heading (of any level)."""
    match = _HEADING_LINE.match(tail.partition("\n")[0])
    return bool(match) and " ".join(match.group("name").lower().split()) == "withdrawn items"


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.split("\n") if line.strip() and not _NONE.match(line.strip(" -*+\t"))]


def _joined(texts: Sequence[str], dedupe: bool) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        for line in _lines(text):
            key = " ".join(line.lower().split())
            if dedupe and key in seen:
                continue
            seen.add(key)
            out.append(line)
    return "\n".join(out) if out else "None."


def merge_parts(
    bodies: Sequence[str], required: Sequence[str], item_section: str, *, stories: int, features: int, size: int, nouns: tuple[str, str] = ("stories", "features"),
) -> str:
    """One document from the parts. The items section is the parts' entries in order; Overview is one sentence about the whole (the parts' own Overviews are dropped); every other
    section (Coverage check, Open questions) is the parts' lines in order, with repeats and 'None' dropped, or 'None.' when nothing is left. 'Withdrawn items' is kept when any part has it."""
    out: list[str] = []
    for name in required:
        texts = [t for body in bodies for t in section_bodies(body, name)]
        if name == item_section:
            mains, tails = [], []
            for t in texts:
                main, tail = split_at_heading(t)
                mains.append(main.strip())
                # A 'Withdrawn items' heading a part wrote inside its list is lifted out of the list here; its entries are still collected below, because
                # section_bodies finds a heading by name at any level. Any other sub-heading stays, after all the stories.
                if tail and not _is_withdrawn_heading(tail):
                    tails.append(tail.strip())
            text = "\n".join([m for m in mains if m] + tails)
        elif name.lower() == "overview":
            # One sentence about the whole. A part's own Overview ("This part covers...", "The joined document will have its own total") is about the part, and read in the
            # finished document it sounds like three documents (found by the first fresh-writer check, S35), so it is not kept; a part must still write one to be valid.
            text = (f"{stories} {nouns[0]} follow for {features} {nouns[1]}. They were written in {len(bodies)} parts of up to {size} {nouns[1]} each "
                    "and joined, so the numbering runs on from one part to the next.")
        else:
            text = _joined(texts, dedupe=True)
        out.append(f"## {name}\n{text}")
    withdrawn = _joined([t for body in bodies for t in section_bodies(body, "Withdrawn items")], dedupe=True)
    if withdrawn != "None.":
        out.append(f"## Withdrawn items\n{withdrawn}")
    return "\n\n".join(out)


def merge_stages(
    first: str, parts: Sequence[str], required: Sequence[str], first_sections: Sequence[str], later_sections: Sequence[str], item_sections: Sequence[str], overview: str,
) -> str:
    """One document from a staged artefact (S41): each required section is taken from the stage that writes it, in the skill's own order.

    A section the FIRST stage writes (the tables and how they relate) is that stage's text, untouched. An item section the later parts write (endpoints, rules) is the parts' entries
    in order, with a part's own sub-heading lifted out as for stories. Coverage check and Open questions are the stage-1 and parts' lines in order, repeats and 'None' dropped. The
    Overview is the one given. 'Withdrawn items' is kept when any stage has it. A section nobody wrote is 'None.' so the joined document names every required section."""
    out: list[str] = []
    for name in required:
        low = name.lower()
        if low == "overview":
            text = overview
        elif name in item_sections and name in later_sections:
            mains, tails = [], []
            for t in (t for body in parts for t in section_bodies(body, name)):
                main, tail = split_at_heading(t)
                mains.append(main.strip())
                if tail and not _is_withdrawn_heading(tail):
                    tails.append(tail.strip())
            text = "\n".join([m for m in mains if m] + tails) or "None."
        elif name in first_sections and name not in later_sections:
            text = "\n".join(section_bodies(first, name)).strip() or "None."
        else:
            texts = [t for body in [first, *parts] for t in section_bodies(body, name)]
            text = _joined(texts, dedupe=True)
        out.append(f"## {name}\n{text}")
    withdrawn = _joined([t for body in [first, *parts] for t in section_bodies(body, "Withdrawn items")], dedupe=True)
    if withdrawn != "None.":
        out.append(f"## Withdrawn items\n{withdrawn}")
    return "\n\n".join(out)

