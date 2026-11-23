"""When is a Build 'verified'? (S45, 2026-11-17)

The Build phase can be marked done only after the real backend has been checked against the design (Build Verify, S20). This module is the whole rule, on plain data, so it can be read,
tested and changed in one place. It reads a stored Verify run and the design versions NOW; it calls nothing, writes nothing and never runs the person's code.

The rule, in words:
  1. There is a Verify run at all.
  2. It was made against the design as it is now: no design artefact has a different version from the one the run compared with.
  3. Every designed ENDPOINT and TABLE was found in the code (with or without differences in its details), and none is 'not found'. A 'possible' or 'weak' match does not count as found.
  4. No RULE is 'not found'. A rule that was found only weakly is reported but does not block (a rule is prose; the code cannot always show it).
Details that differ (a field, a code) are shown to the person and do not block: the person decides whether they matter.

Every sentence returned is plain text for a screen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

REQUIRED_AREAS = ("endpoint", "table")        # every designed one must be found
BLOCKING_NOT_FOUND = ("endpoint", "table", "rule")   # none of these may be 'not found'


@dataclass(frozen=True)
class Verdict:
    verified: bool
    reasons: tuple[str, ...]            # why not, in sentences; empty when verified
    run_id: int | None = None
    designed: int = 0
    found: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"verified": self.verified, "reasons": list(self.reasons), "run_id": self.run_id, "designed": self.designed, "found": self.found}


def design_changed(run_design: Mapping[str, int], now: Mapping[str, int]) -> bool:
    """Has any design artefact a different version from the one the run compared against? An artefact new since the run, or gone, counts."""
    keys = set(run_design) | set(now)
    return any((run_design.get(k) or 0) != (now.get(k) or 0) for k in keys)


def judge(run: Any | None, design_now: Mapping[str, int]) -> Verdict:
    """Is the Build verified, given the newest Verify run (or None) and the design versions now?"""
    if run is None:
        return Verdict(False, ("Build Verify has not been run yet: point it at the folder of the real backend and press Check.",))
    reasons: list[str] = []
    if design_changed(run.design, design_now):
        reasons.append("The design has changed since the last check. Run Build Verify again.")
    counts: Sequence[Mapping[str, Any]] = run.counts or []
    by_area = {c.get("area"): c for c in counts}
    designed = found = 0
    for area in BLOCKING_NOT_FOUND:
        c = by_area.get(area)
        if c is None:
            continue
        designed += int(c.get("designed", 0))
        found += int(c.get("found", 0)) + int(c.get("differences", 0))
        missing = int(c.get("not_found", 0))
        if missing:
            word = {"endpoint": "endpoint", "table": "table", "rule": "rule"}[area]
            reasons.append(f"{missing} designed {word}{'s are' if missing != 1 else ' is'} not found in the code.")
    for area in REQUIRED_AREAS:
        c = by_area.get(area)
        if c is None:
            continue
        weak = int(c.get("possible", 0)) + int(c.get("weak", 0))
        if weak:
            word = {"endpoint": "endpoint", "table": "table"}[area]
            reasons.append(f"{weak} designed {word}{'s were' if weak != 1 else ' was'} matched only weakly; check {'them' if weak != 1 else 'it'} and run the check again.")
    if not by_area:
        reasons.append("The last check found nothing to compare: it listed no endpoints, tables or rules.")
    return Verdict(not reasons, tuple(reasons), run.id, designed, found)
