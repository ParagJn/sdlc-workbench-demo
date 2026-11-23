"""Reading a reviewer's reply and turning it into findings the system can trust.

    <review>
    Summary: one or two sentences.
    - [HIGH] FR-004: What is wrong. Fix: what would fix it.
    - [LOW] Open questions: Another problem. Fix: its fix.
    </review>

The parser is strict about the shape and lenient about the usual mess (chatter outside the block, a code fence
around the whole reply). The VERDICT is not the reviewer's to give: it is computed here from the findings,
so a model cannot write "looks great" next to a list of serious problems.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from ..agents.reply import MAX_PROBLEMS, TRUNCATED_REASONS, ReplyFormatError, unfenced_lines
from ..agents.skills import normalise_heading

SEVERITIES = ("high", "medium", "low")
GENERAL = "General"
MAX_TEXT_CHARS = 1500           # the summary and the problem
MAX_FIX_CHARS = 3000            # a fix is the replacement text itself, so it may be a whole paragraph or a few list entries
MAX_TARGET_WORDS = 8
MAX_TARGET_IDS = 12     # one finding may name up to this many items (raised from 6 on 2026-11-23: a real review of 17 user stories named 8 in one finding and was refused twice); a longer list is left whole, fails validation and goes back to the reviewer

_OPEN = re.compile(r"^\s*<review>\s*$", re.IGNORECASE)
_CLOSE = re.compile(r"^\s*</review>\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_SUMMARY = re.compile(r"^\s*Summary\s*:\s*(?P<text>.*)$", re.IGNORECASE)
_FINDING = re.compile(r"^\s*(?:[-*+]\s*)?\[(?P<severity>[A-Za-z]+)\]\s*(?P<rest>.*)$")  # the bullet is optional
_TARGET_ID = re.compile(r"^(?P<target>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3,})\s*[:\-–—]\s*(?P<rest>.*)$")
# 'Fix:' is the marker. The re-run after S33 found the reviewer also writing 'Fix for COMP-001:' and 'Fix, for RUL-013:' when it had a different fix for each place; the old marker missed those and
# left the whole fix inside the problem. The 'for ...' form is read only with a capital F and a short place name, so a sentence such as "a quick fix for this: ..." is left alone.
_FIX = re.compile(r"\bFix\s*:\s*|\b(?-i:Fix),?\s+for\s+[^:\n]{1,60}:\s*", re.IGNORECASE)
# A file of the Build instruction set, optionally with a line: `UC-002-reserve-a-room.md` or `UC-002-reserve-a-room.md:41`. Read BEFORE the item-ID form, because a file name
# starts with an item ID (`UC-002-...`) and would otherwise be cut at its first dash.
_FILE_TARGET = re.compile(r"^[*_`'\"]*(?P<target>[A-Za-z0-9][A-Za-z0-9._-]*\.md(?::\d{1,5})?)[*_`'\"]*\s*:\s*(?P<rest>.*)$")


@dataclass(frozen=True)
class Finding:
    severity: str  # "high" | "medium" | "low"
    target: str  # an item ID, a section heading, a file (`UC-001-a.md`) or a line in one (`UC-001-a.md:41`), or "General"
    problem: str
    fix: str
    source: str = ""  # "" for the reviewer; "system" for a finding the system's own checks made (no model, no cost)
    others: tuple[str, ...] = ()  # S33: the further places ONE cause applies to (`target` is the first), so a cause is one finding, not one copy per place

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.target, *self.others)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"severity": self.severity, "target": self.target, "problem": self.problem, "fix": self.fix}
        if self.source:
            out["source"] = self.source          # only when set, so every review stored before this existed reads exactly as it did
        if self.others:
            out["targets"] = list(self.targets)   # only for a finding with more than one place, so every review stored before S33 reads exactly as it did
        return out


@dataclass(frozen=True)
class ParsedReview:
    summary: str
    findings: tuple[Finding, ...]


def parse_review(text: str) -> ParsedReview:
    """The summary and findings between the <review> tags. Raises ReplyFormatError, in words the model can act on."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body: list[str] = []
    state = "before"  # before -> inside -> after
    fence: str | None = None
    for line in lines:
        if state == "before":
            if _OPEN.match(line):
                state = "inside"
            continue
        if state == "inside":
            fence_match = _FENCE.match(line)
            if fence_match:
                marker = fence_match.group(1)
                fence = marker if fence is None else (None if marker == fence else fence)
                body.append(line)
                continue
            if fence is None and _CLOSE.match(line):
                state = "after"
                continue
            if fence is None and _OPEN.match(line):
                raise ReplyFormatError("a second <review> tag was opened before the first was closed; write exactly one block")
            body.append(line)
            continue
        if _OPEN.match(line):
            raise ReplyFormatError("the reply contains more than one <review> block; write exactly one")
    if state == "before":
        raise ReplyFormatError("no <review> block was found; put the whole review between <review> and </review>, each tag on its own line")
    if state == "inside":
        raise ReplyFormatError("the <review> block is never closed; end it with </review> on its own line")

    summary = ""
    findings: list[Finding] = []
    in_fix: list[bool] = []         # for each finding: has its 'Fix:' started? Lines that wrap after it belong to the FIX, not the problem
    for raw in unfenced_lines("\n".join(body)):
        if not raw.strip():
            continue
        summary_match = _SUMMARY.match(raw)
        if summary_match and not findings and not summary:
            summary = summary_match.group("text").strip()
            continue
        finding = _FINDING.match(raw)
        if finding is None:
            if findings and raw.startswith((" ", "\t")):
                at = len(findings) - 1                                      # a wrapped continuation of the finding above
                last = findings[at]
                if in_fix[at]:
                    findings[at] = replace(last, fix=(last.fix + "\n" + raw.strip())[:MAX_FIX_CHARS].strip())
                else:
                    findings[at] = _rebuild(last, (last.problem + " " + raw.strip()))
                    in_fix[at] = bool(_FIX.search(raw))
                continue
            if summary and not findings and raw.startswith((" ", "\t")):
                summary += " " + raw.strip()  # a wrapped summary; only an INDENTED line may continue it
                continue
            raise ReplyFormatError(
                f"this line is not a finding or the summary: {raw.strip()[:80]!r}. Write 'Summary: ...' first, then one "
                "'- [HIGH|MEDIUM|LOW] <item ID or section>: <problem>. Fix: <fix>' line per finding"
            )
        severity = finding.group("severity").lower()
        if severity not in SEVERITIES:
            raise ReplyFormatError(f"severity [{finding.group('severity')}] is not one of HIGH, MEDIUM, LOW")
        target, rest = _split_target(finding.group("rest"))
        problem, fix = _split_fix(rest)
        targets = _each_target(target)     # one finding about several items stays ONE finding that names every item (S33); the screen marks each of them
        findings.append(Finding(severity, targets[0], problem, fix, others=tuple(targets[1:])))
        in_fix.append(bool(_FIX.search(rest)))
    if not summary:
        raise ReplyFormatError("the review has no 'Summary:' line; start with 'Summary:' and one or two sentences")
    for item in findings:
        if not item.problem:
            raise ReplyFormatError(f"the [{item.severity.upper()}] finding about {item.target} says nothing about what is wrong")
    return ParsedReview(summary[:MAX_TEXT_CHARS], tuple(findings))


def _rebuild(finding: Finding, text: str) -> Finding:
    problem, fix = _split_fix(text) if _FIX.search(text) else (text, finding.fix)
    return replace(finding, problem=problem.strip()[:MAX_TEXT_CHARS], fix=fix or finding.fix)


def _split_target(rest: str) -> tuple[str, str]:
    rest = rest.strip()
    as_file = _FILE_TARGET.match(rest)
    if as_file:
        return as_file.group("target"), as_file.group("rest").strip()
    as_id = _TARGET_ID.match(rest)
    if as_id:
        return as_id.group("target"), as_id.group("rest").strip()
    head, sep, tail = rest.partition(":")
    name = head.strip().strip("*_`'\"")
    # A section heading is short and is not a sentence. "the text is odd. Fix" is a sentence that ran into "Fix:".
    if sep and name and (len(name.split()) <= MAX_TARGET_WORDS or _is_id_list(name)) and not re.search(r"[.!?]\s|[.!?]$|\bfix$", name, re.IGNORECASE):
        return name, tail.strip()
    return GENERAL, rest


_NOTE_IN_BRACKETS = re.compile(r"\s*\([^()]*\)\s*$")
_ID_ONLY = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3,}")
_TARGET_LIST = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)
# A RANGE of consecutive items, "US-001 to US-004" or "US-001 through US-004" (2026-11-23: a real reviewer wrote "US-001 to US-008" and the review was refused twice). Both ends must be the same kind
# of item with the same number of digits, the first before the last, and no more items than a list may name (MAX_TARGET_IDS), so it is exactly "US-001, US-002, US-003, US-004" and never a guess.
_RANGE = re.compile(r"^(?P<prefix>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)-(?P<a>\d{3,})\s*(?:to|through|thru|-|\u2013|\u2014)\s*(?:(?P=prefix)-)?(?P<b>\d{3,})$", re.IGNORECASE)


def _expand_range(part: str) -> list[str] | None:
    """The items a range names ("US-001 to US-004" -> US-001 .. US-004), or None when `part` is not a range, is backwards, mixes widths, or spans more than MAX_TARGET_IDS items."""
    m = _RANGE.match(part.strip())
    if m is None:
        return None
    a, b = m.group("a"), m.group("b")
    if len(a) != len(b) or int(a) >= int(b) or int(b) - int(a) + 1 > MAX_TARGET_IDS:
        return None
    prefix = m.group("prefix").upper() if m.group("prefix").isupper() or m.group("prefix").islower() else m.group("prefix")
    return [f"{prefix}-{n:0{len(a)}d}" for n in range(int(a), int(b) + 1)]


def _is_id_list(text: str) -> bool:
    """Is `text` only item IDs (and ranges of them) joined by commas, 'and' or '&'? Then it may be as long as a list of places may be, however many words that is: it is a list, not a sentence."""
    parts = [p for p in _TARGET_LIST.split(text.strip()) if p]
    return len(parts) > 1 and len(parts) <= MAX_TARGET_IDS and all(_ID_ONLY.fullmatch(p) or _expand_range(p) is not None for p in parts)


def _each_target(target: str) -> list[str]:
    """"LFS-001 and LFS-003" or "LFS-001, LFS-003" is two item IDs: return both; "US-001 to US-004" is the four items it spans. Anything else (an ID, a heading, even a heading with a
    comma in it) is returned as it came. Split only when EVERY part is an item ID or a range of them, so a section name is never cut up, and at most MAX_TARGET_IDS items are named."""
    parts = [p for p in _TARGET_LIST.split(target.strip()) if p]
    spread: list[str] = []
    for p in parts:
        if _ID_ONLY.fullmatch(p):
            spread.append(p)
        elif (r := _expand_range(p)) is not None:
            spread.extend(r)
        else:
            return [target]
    spread = list(dict.fromkeys(spread))
    if (len(parts) > 1 or len(spread) > 1) and len(spread) <= MAX_TARGET_IDS:
        return spread
    return [target]


_PLACE_SEPARATOR = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)


def _segments(target: str, is_place) -> list[str] | None:
    """Cut `target` at separators (a comma, a semicolon, '&' or 'and') into parts that are ALL places, or None when it cannot be done. Tries the fewest parts first, so a
    section called 'Constraints and assumptions' stays whole and 'Constraints and assumptions, Open questions' becomes two."""
    text = target.strip()
    if is_place(text):
        return [text]
    cuts = [m for m in _PLACE_SEPARATOR.finditer(text) if m.start() > 0 and m.end() < len(text)]
    for m in cuts:
        head = text[: m.start()].strip()
        if not is_place(head):
            continue
        rest = _segments(text[m.end():], is_place)
        if rest is not None:
            return [head, *rest]
    return None


def resolve_places(parsed: ParsedReview, *, item_ids: Iterable[str], sections: Iterable[str]) -> ParsedReview:
    """Read a finding's target against what the artefact really has (S33). The parser only splits lists made wholly of item IDs, because it cannot know which words are a section's name.
    Here the item IDs and section headings are known, so 'Scope, Stakeholders' becomes two places, while 'Constraints and assumptions' stays one. A target that cannot be read as places
    that all exist is left as it came, and validation sends it back naming it."""
    ids = set(item_ids)
    names = {normalise_heading(x) for x in sections}

    def is_place(text: str) -> bool:
        return text == GENERAL or text in ids or normalise_heading(text) in names

    out: list[Finding] = []
    for f in parsed.findings:
        if f.others or all(is_place(t) for t in f.targets):
            out.append(f)
            continue
        found = _segments(f.target, is_place)
        if found is None:
            # A place followed by a note in brackets: "Stories (FR-051, FR-052 not covered)" is the section "Stories". The note is kept in the finding's problem text by the
            # reviewer already; the target must be only the place. Seen in the first real writer check (a review failed twice on it).
            bare = _NOTE_IN_BRACKETS.sub("", f.target).strip()
            found = _segments(bare, is_place) if bare and bare != f.target else None
        out.append(replace(f, target=found[0], others=tuple(found[1:MAX_TARGET_IDS])) if found and len(found) <= MAX_TARGET_IDS else f)
    return ParsedReview(parsed.summary, tuple(out))


def _split_fix(text: str) -> tuple[str, str]:
    match = _FIX.search(text)
    if match is None:
        return text.strip()[:MAX_TEXT_CHARS], ""
    return text[: match.start()].strip()[:MAX_TEXT_CHARS], text[match.end():].strip()[:MAX_FIX_CHARS]


def verdict_of(findings: Iterable[Finding]) -> str:
    """`revise` when any finding is high, otherwise `ok`. Decided here, never by the model."""
    return "revise" if any(f.severity == "high" for f in findings) else "ok"


def validate_review(
    parsed: ParsedReview, *, item_ids: Iterable[str], sections: Iterable[str], max_findings: int, finish_reason: str | None
) -> list[str]:
    """Problems with a parsed review as plain sentences for the model. [] means usable."""
    problems: list[str] = []
    if finish_reason in TRUNCATED_REASONS:
        problems.append("the reply was cut off by the output limit; write fewer, shorter findings")
    if len(parsed.findings) > max_findings:
        problems.append(f"the review has {len(parsed.findings)} findings; the limit is {max_findings}. Keep the most important")
    ids = set(item_ids)
    names = {normalise_heading(s) for s in sections}
    for finding in parsed.findings:
        for target in finding.targets:
            if target == GENERAL or target in ids or normalise_heading(target) in names:
                continue
            problems.append(
                f"a finding is about '{target}', which is not an item ID or a section of this artefact; "
                f"name an item ID that exists, a section heading that exists, or {GENERAL}"
            )
    return problems[:MAX_PROBLEMS]


def merge_repeats(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Findings with the same severity, problem, fix and source become ONE finding that names every place, in the order first seen (S33). Deterministic, no model.
    The same cause worded differently is not merged here: that is for the reviewer's prompt to prevent."""
    order: list[tuple[str, str, str, str]] = []
    found: dict[tuple[str, str, str, str], Finding] = {}
    for f in findings:
        key = (f.severity, f.problem, f.fix, f.source)
        have = found.get(key)
        if have is None:
            order.append(key)
            found[key] = f
            continue
        extra = tuple(t for t in f.targets if t not in have.targets)
        if extra:
            found[key] = replace(have, others=(*have.others, *extra))
    return tuple(found[k] for k in order)


def merge_targets(findings: Sequence[Finding]) -> list[dict[str, object]]:
    return [f.as_dict() for f in findings]
