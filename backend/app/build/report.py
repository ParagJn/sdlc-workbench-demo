"""The test report (S18a): what the generated tests did when they were REALLY run, written by the system with no model.

Every number comes from the recorded rounds of the fix loop (`build_rounds`) and the saved test files. A model is never asked what the tests did: execution
beats claims. The report is a Markdown body in the same shape as every artefact, so it is versioned, listed and traceable like one.

What it cannot say, it says it cannot: the rounds do not record which test covers which endpoint, so coverage is reported as "named by a test", a CLAIM the
test files make about themselves, not as proof that the endpoint is exercised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from ..agents.items import parse_items
from ..agents.skills import ItemSpec
from ..models import BuildRound
from .files import parse_code

REPORT_AUTHOR = "agent:test_engineer@system"      # an author must be a person or an agent; the system acts as the test engineer, with no model behind it
MAX_FAILURES_SHOWN = 10
MAX_MESSAGE_CHARS = 200

# How the loop ended, in words. The loop's own result is in the last round's outcome.
ENDINGS = {
    "passed": "All the generated tests passed.",
    "cap_reached": "The tests were still failing when the fix rounds ran out.",
    "not_converging": "The same tests failed twice running, so another fix would have repeated itself.",
    "blocked": "The tests could not be judged: the sandbox could not run anything.",
    "no_tests": "No tests were found. That is not a pass.",
    "invalid": "The developer could not produce a valid fix, so the tests were left failing.",
    "stopped": "The run was stopped (its money or time cap, or a cancel) before the tests could be fixed.",
    "failed": "The tests were failing and a fix was asked for.",
}
PASSING = ("passed",)
_API_LINE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)", re.IGNORECASE)


@dataclass(frozen=True)
class Coverage:
    """Which design endpoints a test file names in its `Derives from:`. A claim by the tests, not proof."""
    endpoints: tuple[str, ...]
    named: tuple[str, ...]

    @property
    def unnamed(self) -> tuple[str, ...]:
        return tuple(e for e in self.endpoints if e not in self.named)


def last_loop(rounds: Sequence[BuildRound]) -> tuple[BuildRound, ...]:
    """The rounds of the most recent loop, oldest first. An empty list means the tests were never run."""
    if not rounds:
        return ()
    newest = max(rounds, key=lambda r: r.id).loop_id
    return tuple(sorted((r for r in rounds if r.loop_id == newest), key=lambda r: r.round))


def ending_of(loop: Sequence[BuildRound]) -> str:
    """How the loop ended: the outcome of its last round. 'never_run' if there were no rounds."""
    return loop[-1].outcome if loop else "never_run"


def coverage(design_body: str | None, design_specs: Sequence[ItemSpec], tests_body: str | None, test_specs: Sequence[ItemSpec]) -> Coverage | None:
    """Endpoints (API items) in the design, and which of them a test file's entry names. None if there is no design or no tests to compare."""
    if not design_body or not tests_body:
        return None
    endpoints = tuple(i.id for i in parse_items(design_body, design_specs).items if i.prefix == "API")
    named = {t for item in parse_items(tests_body, test_specs).items for t in item.derives}
    return Coverage(endpoints, tuple(e for e in endpoints if e in named))


def _round_line(r: BuildRound) -> str:
    who = f" A fix was asked of the {r.artefact.replace('_', ' ')}" + (f" (saved as version {r.version})." if r.version else ".") if r.artefact else ""
    return f"- Round {r.round}: {r.passed} passed, {r.failed} failed, {r.errors} errors, {r.skipped} skipped, in {r.seconds:.1f}s.{who}"


def build_report(loop: Sequence[BuildRound], tests_body: str | None, test_specs: Sequence[ItemSpec], cover: Coverage | None) -> str:
    """The report's Markdown body. Pure: the same rounds and files give the same text."""
    ending = ending_of(loop)
    out = ["## Result"]
    if not loop:
        out += ["The generated tests have not been run yet, so there is no result to report.", "", "## Rounds", "None."]
    else:
        last = loop[-1]
        out += [ENDINGS.get(ending, f"The loop ended as '{ending}'."),
                f"Final run: {last.passed} passed, {last.failed} failed, {last.errors} errors, {last.skipped} skipped.",
                f"The generated tests were run {len(loop)} time(s) in this loop, taking {sum(r.seconds for r in loop):.1f} seconds in all.",
                "", "## Rounds"]
        out += [_round_line(r) for r in loop]
    out += ["", "## Failing tests"]
    final_failures = list(loop[-1].failures)[:MAX_FAILURES_SHOWN] if loop else []
    if final_failures:
        out += [f"- {f['test']} [{f['kind']}]: {str(f['message'])[:MAX_MESSAGE_CHARS]}" for f in final_failures]
        total = loop[-1].failed + loop[-1].errors
        if total > len(final_failures):
            out.append(f"- ...and {total - len(final_failures)} more not listed here.")
    else:
        out.append("None." if loop and ending in PASSING else "No failing test is recorded for the final run.")

    out += ["", "## Tests and the design"]
    if cover is None:
        out.append("There is no design or no test suite to compare, so nothing can be said about which endpoints the tests name.")
    else:
        out.append(f"{len(cover.named)} of {len(cover.endpoints)} endpoint(s) in the low-level design are named by a test file. "
                   "This is what the tests say about themselves; the run above is what shows whether they pass.")
        if cover.unnamed:
            out.append("Not named by any test: " + ", ".join(cover.unnamed) + ".")
    files = [f.path for f in parse_code(tests_body)[0]] if tests_body else []
    out += ["", "## Test files", *([f"- {p}" for p in files] or ["None."])]
    out += ["", "## How to read this", "Every number above comes from really running the tests in the sandbox, recorded round by round. No model was asked what the tests did."]
    return "\n".join(out) + "\n"
