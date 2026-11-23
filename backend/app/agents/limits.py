"""The limits and formats the skills state, checked in code (S35). Pure: no database, no model.

The skills tell a writer things like "at most thirty characters" or "two to four acceptance criteria". A model breaks them now and then and a hand edit breaks
the format ones; the reviewer finds each in about two reviews out of three. Code finds every one, for nothing, so `check_body` calls this for an agent's reply and
a person's edit alike (see docs/steps/S35-mechanical-checks-in-the-writer.md for the measurements behind each rule).

Every rule is keyed to one item, so a person editing an artefact that ALREADY had a defect is not blocked by it: `previous_body` waives a finding that is unchanged
(same rule, same item, same measure) and flags a new or worse one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..architecture.diagram import FLOW, edges_of
from ..design.sanitise import drawings_in
from .items import parse_items
from .reply import section_bodies, unfenced_lines
from .skills import WITHDRAWN_SECTION, ItemSpec, normalise_heading

# ---- the numbers. Each appears in its skill's text (a test builds the phrase from the number, so a skill and its check cannot drift apart) -------------------------
FLOW_WORDS_MAX = 40           # flow_diagram: "a few plain words (at most forty characters)" (raised from 30 on 2026-11-23: a real run missed 30 by two characters on one line, three attempts running; the key under the picture shows 62)
DRAWING_MAX_CHARS = 9000      # lowfi_wireframes: "Keep each drawing under 9000 characters" (was 2500, then 6000: a real project's drawings came out at 6,275 to 6,362 and failed three repairs. The cleaner allows 12,000)
HIFI_DRAWING_MAX_CHARS = 11000  # hifi_wireframes: the same screens with colour, type sizes and states added, so each is bigger than its low-fidelity drawing (FieldOps: 9,084 to 12,247 against 9,000). Stays under the cleaner's 12,000
DRAWING_MAX = {"lowfi_wireframes": DRAWING_MAX_CHARS, "hifi_wireframes": HIFI_DRAWING_MAX_CHARS}      # the limit for each skill that draws
CRITERIA_MIN = 2              # user_stories: "two to three indented acceptance criteria" (was two to four until S39)
CRITERIA_MAX = 3
STORIES_PER_FEATURE_MAX = 3   # user_stories: "never more than three stories for one feature" (S39)
COMPONENTS_MAX = 12           # component_overview: "Use at most twelve components"
SCREENS_MAX = 12              # lowfi_wireframes: "the whole artefact to twelve screens or fewer" (was six, which one reply could not hold; the wireframes are now written in parts, S42)
FILES_MAX = 12                # low_level_design for an automation project: "Use at most twelve files" (S40 part 2c). Scripts are NOT capped here, as endpoints are not: the parts of a staged document
                              # cannot repair a total, and a joined document that fails its check is lost.
NEAR_LIMIT_PERCENT = 85      # S41: a writer call that used this much of its output ceiling, or more, is flagged before it becomes a failure
PERSONA_POINT_WORDS = 30      # personas: "each point is one sentence of at most thirty words, not counting citations" (S38)
PERSONA_KEY = "personas"
PERSONA_LABELS = ("Goal", "Frustration", "Success")

LISTED = 12                   # entries named in one sentence; the rest are counted

WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 6: "six", 12: "twelve", 30: "thirty", 40: "forty"}
DRAWING_KEYS = ("lowfi_wireframes", "hifi_wireframes")
STORY_KEY = "user_stories"
COMPONENT_KEY = "component_overview"
LOWFI_KEY = "lowfi_wireframes"
LLD_KEY = "low_level_design"

# The phrase each skill must contain for each number, built from the constants.
SKILL_PHRASES: dict[str, tuple[str, ...]] = {
    FLOW: (f"at most {WORDS.get(FLOW_WORDS_MAX, FLOW_WORDS_MAX)} characters",),
    "lowfi_wireframes": (f"under {DRAWING_MAX_CHARS} characters", f"{WORDS.get(SCREENS_MAX, SCREENS_MAX)} screens or fewer"),
    "hifi_wireframes": (f"under {HIFI_DRAWING_MAX_CHARS} characters",),
    STORY_KEY: (f"{WORDS.get(CRITERIA_MIN, CRITERIA_MIN)} to {WORDS.get(CRITERIA_MAX, CRITERIA_MAX)} indented acceptance criteria", f"never more than {WORDS[STORIES_PER_FEATURE_MAX]} stories for one feature"),
    COMPONENT_KEY: (f"at most {WORDS.get(COMPONENTS_MAX, COMPONENTS_MAX)} components",),
    PERSONA_KEY: (f"at most {WORDS.get(PERSONA_POINT_WORDS, PERSONA_POINT_WORDS)} words", f"exactly {WORDS[len(PERSONA_LABELS)]} nested bullets"),
}

_INSTRUCTION = re.compile(r"\b(?:add|remove|replace|delete)\s+open\s+questions?\s+\d+", re.IGNORECASE)
_TOP_LINE = re.compile(r"^ ?(?:[-*+]|\d{1,3}[.)])[ \t]+(?P<rest>\S.*)$")
_NESTED = re.compile(r"^(?: {2,}|\t)[-*+][ \t]+\S")
_RUN_IN = re.compile(r"derives[ \t]+from[ \t]*:[^\n]*?\s[-*•]\s+\S", re.IGNORECASE)
_TITLE = re.compile(r"<title>\s*([A-Z][A-Z0-9]*-\d{3,})")
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s")
_BRACKETS = re.compile(r"\[[^\[\]\n]*\]")
_DERIVES_TAIL = re.compile(r"derives[ \t]+from[ \t]*:", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    rule: str       # L1 .. L7
    key: str        # the item (or "all") it is about
    detail: str     # the measure: a length, a count, the matched words. The same item with the same measure is "the same defect"
    text: str       # how the sentence names it ("FLOW-001 (31)")


def _items(body: str, specs: Sequence[ItemSpec]):
    return parse_items(body, specs).items


def near_limit(tokens_out: int, limit: int | None) -> int | None:
    """How much of its output ceiling a call used, as a whole percent, when that is 85% or more; None below it or when the ceiling or the tokens are unknown (S41)."""
    if not limit or limit <= 0 or not tokens_out or tokens_out <= 0:
        return None
    if tokens_out * 100 < NEAR_LIMIT_PERCENT * limit:                       # compared exactly: 84.99% is below the line, however it would round
        return None
    return min(100, round(100 * tokens_out / limit))


def find_limits(skill_key: str, body: str, specs: Sequence[ItemSpec]) -> list[Finding]:
    """Every limit the body breaks, one finding per item. Never raises."""
    out: list[Finding] = []
    items = _items(body, specs)

    for item in items:                                            # L1: "FR-010 FR-010 The system..."
        if re.match(rf"{re.escape(item.id)}(?![A-Za-z0-9-])", item.text.lstrip()):
            out.append(Finding("L1", item.id, "twice", item.id))
    for item in items:                                            # L2: "...Derives from: PER-001. Add Open question 14: ..."
        found = _INSTRUCTION.search(item.text)
        if found:
            words = " ".join(found.group(0).split())
            out.append(Finding("L2", item.id, words.lower(), f"{item.id} ('{words}')"))

    if skill_key == FLOW:                                         # L3: the words after the colon
        edges, _ = edges_of(tuple(i for i in items if i.prefix == FLOW_PREFIX))
        for edge in edges:
            n = len(edge.label.rstrip(" ."))
            if n > FLOW_WORDS_MAX:
                out.append(Finding("L3", edge.item_id, str(n), f"{edge.item_id} ({n})"))

    if skill_key in DRAWING_KEYS:                                 # L4: one drawing
        for first, _last, text in drawings_in(body):
            n = len(text.strip())
            if n > DRAWING_MAX[skill_key]:
                title = _TITLE.search(text)
                name = title.group(1) if title else f"the drawing at line {first}"
                out.append(Finding("L4", name, str(n), f"{name} ({n})"))

    if skill_key == STORY_KEY and specs:                          # L5 and L6: a story and the bullets nested under it
        for story_id, count, line in _stories(body, specs[0]):
            if _RUN_IN.search(line):
                out.append(Finding("L6", story_id, "run-in", story_id))
            elif not CRITERIA_MIN <= count <= CRITERIA_MAX:
                out.append(Finding("L5", story_id, str(count), f"{story_id} has {count}"))
        per_feature: dict[str, list[str]] = {}                  # L10: stories per feature (S39). A withdrawn story sits in the same section but is listed, not live
        withdrawn = set(parse_items(body, specs).withdrawn)
        for item in (i for i in items if i.prefix == specs[0].prefix and i.id not in withdrawn):
            for target in item.derives:
                if target.startswith("FEAT-"):
                    per_feature.setdefault(target, []).append(item.id)
        for feature, ids in per_feature.items():
            if len(ids) > STORIES_PER_FEATURE_MAX:
                out.append(Finding("L10", feature, str(len(ids)), f"{feature} has {len(ids)} ({', '.join(ids)})"))

    if skill_key == PERSONA_KEY and specs:                        # L8 and L9: a persona is a title line and three short points (S38)
        for persona_id, points, line in _personas(body, specs[0]):
            labels = [label for label, _ in points]
            missing = [label for label in PERSONA_LABELS if label not in labels]
            on_title = any(re.search(rf"\b{label}[ \t]*:", _DERIVES_TAIL.split(line, maxsplit=1)[0]) for label in PERSONA_LABELS)
            if missing or len(points) != len(PERSONA_LABELS) or on_title:
                why = f"missing {', '.join(missing)}" if missing else f"{len(points)} points" if len(points) != len(PERSONA_LABELS) else "points written on the title line"
                out.append(Finding("L8", persona_id, why, f"{persona_id} ({why})"))
            for label, text in points:
                n = len(_BRACKETS.sub(" ", text).split())
                if n > PERSONA_POINT_WORDS:
                    out.append(Finding("L9", f"{persona_id} {label}", str(n), f"{persona_id} {label} ({n})"))

    if skill_key == COMPONENT_KEY and specs:                      # L7: counts that nothing else enforces
        n = sum(1 for i in items if i.prefix == specs[0].prefix)
        if n > COMPONENTS_MAX:
            out.append(Finding("L7", "components", str(n), str(n)))
    if skill_key == LOWFI_KEY and specs:
        n = sum(1 for i in items if i.prefix == specs[0].prefix)
        if n > SCREENS_MAX:
            out.append(Finding("L7", "screens", str(n), str(n)))
    if skill_key == LLD_KEY and any(spec.prefix == "FIL" for spec in specs):          # L7: files, for a design written as scripts and files
        n = sum(1 for i in items if i.prefix == "FIL")
        if n > FILES_MAX:
            out.append(Finding("L7", "files", str(n), str(n)))
    return out


FLOW_PREFIX = "FLOW"


def _stories(body: str, spec: ItemSpec) -> list[tuple[str, int, str]]:
    """(story ID, nested bullets under it, its own line) for each story. A withdrawn item or a heading ends the story above it."""
    out: list[list] = []
    for chunk in section_bodies(body, spec.section):
        current: list | None = None
        withdrawn = False
        for line in unfenced_lines(chunk):
            heading = _HEADING_LINE.match(line)
            if heading:
                current = None
                withdrawn = normalise_heading(line.strip().lstrip("#").strip()) == normalise_heading(WITHDRAWN_SECTION)      # the items under it are listed, not written
                continue
            if withdrawn:
                continue
            top = _TOP_LINE.match(line)
            if top:
                lead = re.match(rf"^(?:\*\*|__)?({spec.prefix}-\d{{3,}})(?![A-Za-z0-9-])", top.group("rest"))
                current = [lead.group(1), 0, line] if lead else None
                if current is not None:
                    out.append(current)
                continue
            if current is not None and _NESTED.match(line):
                current[1] += 1
    return [(a, b, c) for a, b, c in out]


def _personas(body: str, spec: ItemSpec) -> list[tuple[str, list[tuple[str, str]], str]]:
    """(persona ID, [(label, text) for each nested point], its own line) for each persona. A point is a nested bullet; one with no label is kept as ('', text)."""
    out: list[list] = []
    for chunk in section_bodies(body, spec.section):
        current: list | None = None
        withdrawn = False
        for line in unfenced_lines(chunk):
            if _HEADING_LINE.match(line):
                current = None
                withdrawn = normalise_heading(line.strip().lstrip("#").strip()) == normalise_heading(WITHDRAWN_SECTION)
                continue
            if withdrawn:
                continue
            top = _TOP_LINE.match(line)
            if top:
                lead = re.match(rf"^(?:\*\*|__)?({spec.prefix}-\d{{3,}})(?![A-Za-z0-9-])", top.group("rest"))
                current = [lead.group(1), [], line] if lead else None
                if current is not None:
                    out.append(current)
                continue
            if current is not None and _NESTED.match(line):
                text = re.sub(r"^\s*[-*+][ \t]+", "", line).strip()
                label = re.match(r"^(?:\*\*|__)?(" + "|".join(PERSONA_LABELS) + r")(?:\*\*|__)?[ \t]*:[ \t]*(.*)$", text, re.IGNORECASE)
                current[1].append((label.group(1).capitalize(), label.group(2)) if label else ("", text))
    return [(a, b, c) for a, b, c in out]


def _listed(parts: list[str]) -> str:
    shown = ", ".join(parts[:LISTED])
    return shown + (f" and {len(parts) - LISTED} more" if len(parts) > LISTED else "")


def limit_problems(skill_key: str, body: str, specs: Sequence[ItemSpec], previous_body: str | None = None, waive_rules: frozenset[str] | None = None) -> list[str]:
    """Plain sentences for the model or the person. [] means the body is within every limit.

    With `previous_body`, a finding the previous version already had (same rule, item and measure) is waived: only a new or changed defect is reported.
    `waive_rules` narrows that to the named rules (S39: a model's REWRITE of a backlog may keep the stories-per-feature count it was shown, but not any other defect)."""
    found = find_limits(skill_key, body, specs)
    if previous_body is not None and found:
        before = {(f.rule, f.key, f.detail) for f in find_limits(skill_key, previous_body, specs)}
        found = [f for f in found if (f.rule, f.key, f.detail) not in before or (waive_rules is not None and f.rule not in waive_rules)]
    by_rule: dict[str, list[Finding]] = {}
    for f in found:
        by_rule.setdefault(f.rule, []).append(f)
    problems: list[str] = []
    if "L1" in by_rule:
        problems.append(f"{_listed([f.text for f in by_rule['L1']])} start{'s' if len(by_rule['L1']) == 1 else ''} with the item's ID twice; write the ID once at the start of the line")
    if "L2" in by_rule:
        problems.append(
            f"{_listed([f.text for f in by_rule['L2']])} contain{'s' if len(by_rule['L2']) == 1 else ''} an instruction to the editor; the text of an item is the item itself, "
            "so remove the instruction (an open question is added in the Open questions section)"
        )
    if "L3" in by_rule:
        problems.append(
            f"these calls say more than {FLOW_WORDS_MAX} characters after the colon: {_listed([f.text for f in by_rule['L3']])}; "
            f"shorten the words after the colon on each to {FLOW_WORDS_MAX} characters or fewer"
        )
    if "L4" in by_rule:
        problems.append(f"these drawings are longer than {DRAWING_MAX[skill_key]} characters: {_listed([f.text for f in by_rule['L4']])}; draw fewer elements or shorter labels")
    if "L6" in by_rule:
        problems.append(
            f"the acceptance criteria of {_listed([f.text for f in by_rule['L6']])} are written on the story's own line after 'Derives from:'; "
            "end the story line with its 'Derives from:' IDs and write each criterion as its own nested bullet underneath, indented two spaces"
        )
    if "L10" in by_rule:
        problems.append(
            f"a feature gets at most {WORDS[STORIES_PER_FEATURE_MAX]} stories, and most need only {WORDS[1]} or {WORDS[2]}: {_listed([f.text for f in by_rule['L10']])}; "
            "merge stories that cover the same behaviour, keep the ones that are clearly separate, and list the feature under Open questions as needing a split if it needs more"
        )
    if "L5" in by_rule:
        problems.append(
            f"each story needs {WORDS[CRITERIA_MIN]} to {WORDS[CRITERIA_MAX]} acceptance criteria as nested bullets: {_listed([f.text for f in by_rule['L5']])}"
        )
    if "L8" in by_rule:
        problems.append(
            f"each persona is a title line and exactly {WORDS[len(PERSONA_LABELS)]} nested bullets, Goal, Frustration and Success: {_listed([f.text for f in by_rule['L8']])}; "
            "write each as its own bullet under the title line, indented two spaces, and keep 'Derives from:' on the title line"
        )
    if "L9" in by_rule:
        problems.append(
            f"these persona points are longer than {WORDS[PERSONA_POINT_WORDS]} words (citations not counted): {_listed([f.text for f in by_rule['L9']])}; "
            "say it in one sentence, and put a constraint or anything unresolved in Open questions"
        )
    for f in by_rule.get("L7", []):
        if f.key == "components":
            problems.append(f"the component overview lists {f.text} components; the limit is {COMPONENTS_MAX}. Group related parts into one component and say what you grouped in Open questions")
        elif f.key == "files":
            problems.append(f"the design lists {f.text} files; the limit is {FILES_MAX}. Keep the files that matter most and list the rest in Open questions")
        else:
            problems.append(f"the screens list has {f.text} screens; the limit is {SCREENS_MAX}. Combine journey steps onto one screen, or list the rest in Open questions")
    return problems
