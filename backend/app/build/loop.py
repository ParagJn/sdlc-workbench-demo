"""The write -> test -> fix loop (S17b-3).

The generated tests are REALLY run in the sandbox. Their failures go back to a developer agent, who writes a new version of ONE artefact; its files are written
and the tests are run again, up to `build_fix_rounds` times. Everything that decides what happens is a small pure function (who fixes what, what the model is
told, when the loop has stopped getting anywhere); `fix_loop` is the only part that does I/O.

Every ending is a distinct result and none is an exception:
  converged       every test passed and at least one test ran
  cap_reached     still failing after the last round
  not_converging  the same tests failed two rounds running, so it stopped one round early
  stopped         the run's money or time cap, or a cancel
  blocked         the sandbox cannot run anything, so nothing can be judged
  no_tests        nothing was collected (never reported as a pass)
  invalid         the model could not produce a valid reply even after its repairs

Every round is recorded (counts and short failure lines, never file contents or a full output). Whatever happens, the workspace is put back to the latest
SAVED version of every component at the end, so an attempt that was never saved cannot linger.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from ..agents.errors import OutputInvalid, RunStopped
from ..sandbox.pytest_report import Failure, RunReport, run_tests
from ..sandbox.runner import Limits
from .inner import make_extra_check, sync_workspace

CONVERGED, CAP_REACHED, NOT_CONVERGING = "converged", "cap_reached", "not_converging"
STOPPED, BLOCKED, NO_TESTS, INVALID = "stopped", "blocked", "no_tests", "invalid"
RESULTS = (CONVERGED, CAP_REACHED, NOT_CONVERGING, STOPPED, BLOCKED, NO_TESTS, INVALID)

TESTS, BACKEND = "test_suite", "backend_code"
SHOWN_FAILURES = 10
SHOWN_MESSAGE_CHARS = 400
# A failure that says the TESTS are broken (they could not be imported or set up), not that the code is wrong.
TEST_ERROR_STATUSES = ("collection_error",)


@dataclass(frozen=True)
class RoundNote:
    """One round as the caller sees it (the permanent record holds the same facts)."""
    number: int
    artefact: str                # who was asked to fix things AFTER this run of the tests ("" if nobody was)
    version: int | None          # the version of that artefact that was then saved
    outcome: str                 # passed | failed | no_tests | blocked | stopped | invalid | not_converging | cap_reached
    passed: int
    failed: int
    errors: int
    skipped: int
    fingerprint: str


@dataclass(frozen=True)
class LoopResult:
    result: str                  # one of RESULTS
    reason: str                  # a sentence
    rounds: tuple[RoundNote, ...]
    last: RunReport | None
    calls: int
    cost_usd: float | None
    loop_id: str
    workspace: dict = field(default_factory=dict)   # what was put back at the end: {artefact: "written" | "removed" | "none" | "refused"}

    @property
    def ok(self) -> bool:
        return self.result == CONVERGED


# ---- the pure parts ------------------------------------------------------------------------------------------------------------------

def fingerprint(report: RunReport) -> str:
    """A short stable code for WHICH tests are failing (not why, not how many times). The same failing set gives the same code, so a loop that is
    repeating itself can be told from one that is making progress. A run with nothing failing has the empty code."""
    names = sorted({f"{f.kind}:{f.test}" for f in report.failures})
    if not names:
        return ""
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:16]


def route(report: RunReport) -> str | None:
    """Which artefact is asked to fix this, or None if nobody should be. One artefact per round.

    Tests that cannot even be imported or set up say nothing about the code, so the test engineer fixes those first. An assertion that fails means the code
    does not do what the design says (the test skill tells the tests to assert the design), so the backend developer fixes those."""
    if report.status in TEST_ERROR_STATUSES:
        return TESTS
    if any(f.kind == "error" for f in report.failures):
        return TESTS
    if report.failures or report.failed or report.errors:
        return BACKEND
    return None


def classify(report: RunReport) -> str:
    """The loop's reading of one run of the tests: ok | blocked | no_tests | broken.  Only a run that executed tests can be 'ok' or 'broken'."""
    if report.status in ("unavailable", "refused"):
        return "blocked"
    if report.status == "no_tests" or (report.status == "ran" and report.total == 0):
        return "no_tests"
    if report.all_passed:
        return "ok"
    return "broken"


def failure_lines(report: RunReport, limit: int = SHOWN_FAILURES) -> list[Failure]:
    return list(report.failures)[:limit]


def fix_feedback(report: RunReport, artefact: str, round_number: int, rounds: int) -> list[str]:
    """What the developer is told. Real facts only: the round, the counts, and a few failures with their first lines. Never the whole output."""
    counts = f"{report.passed} passed, {report.failed} failed, {report.errors} errors, {report.skipped} skipped"
    lines = [f"The generated tests were really run (round {round_number} of {rounds}). Result: {counts}."]
    if report.status == "collection_error":
        lines.append("pytest could not collect the tests: a test file has an import or syntax error. Fix it so the tests can be collected.")
    elif report.status in ("timed_out", "cut_off", "crashed"):
        lines.append(f"The test run ended abnormally ({report.status}): {report.reason or 'see the failures below'}.")
    shown = failure_lines(report)
    if shown:
        lines.append(f"The {'failures' if len(shown) > 1 else 'failure'} (first {len(shown)} of {len(report.failures)}):")
        for f in shown:
            lines.append(f"- {f.test} [{f.kind}]: {f.message[:SHOWN_MESSAGE_CHARS]}")
    owner = "tests" if artefact == TESTS else "code"
    lines.append(f"Fix the {owner}. Keep every file that is already correct, and change only what these failures need.")
    return lines


def round_event(number: int, artefact: str, failed: int, errors: int) -> dict:
    """The data of the `build.round` event: which round, who was asked to fix things, and how many tests were failing. Counts and a name only."""
    return {"round": number, "artefact": artefact, "failed": failed, "errors": errors}


def record_failures(report: RunReport) -> list[dict]:
    return [{"test": f.test, "kind": f.kind, "message": f.message} for f in failure_lines(report)]


# ---- the loop --------------------------------------------------------------------------------------------------------------------------

class FixLoop:
    """Runs the generated tests and asks for fixes. Needs the pieces a phase run already has; calls no model itself except through `runtime`.

    The Build phase must be running (the runtime only writes into a running phase). `rounds` counts FIXES, so a loop makes at most rounds + 1 runs of the
    tests: the first, then one after each fix."""

    def __init__(self, settings, store, runtime, library, build, sandbox_service, *, new_id: Callable[[], str] | None = None):
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self.library = library
        self.build = build
        self.sandbox_service = sandbox_service
        self._new_id = new_id or (lambda: uuid.uuid4().hex[:12])

    def _run_tests(self, project_id: str) -> RunReport:
        workspace = self.sandbox_service.workspace(project_id)
        return run_tests(self.sandbox_service.sandbox, workspace, None, Limits.from_settings(self.settings, tests=True))

    async def run(
        self, project_id: str, *, run_id: str | None = None, meter=None, rounds: int | None = None, actor: str = "system",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> LoopResult:
        """Run the loop. `meter` is the run's Meter (money and clock); without one there is no cap except the round limit.
        Each run of the tests is one recorded round: its number, and who was then asked to fix things (nobody, on the last)."""
        cap = rounds if rounds is not None else self.settings.build_fix_rounds
        loop_id = self._new_id()
        notes: list[RoundNote] = []
        calls, costs = 0, []
        last: RunReport | None = None
        previous_print: str | None = None
        final: tuple[str, str] = (CAP_REACHED, "")

        def emit(kind: str, data: dict) -> None:
            if on_event is not None:
                on_event(kind, data)

        def on_call(result, attempt, problems):
            nonlocal calls
            calls += 1
            costs.append(result.cost_usd)
            if meter is not None:
                meter.add_call(result.cost_usd, result.tokens_in, result.tokens_out)
                return meter.should_stop() if problems else None
            return None

        try:
            # The first run of the tests is against what is SAVED, not whatever an earlier unsaved attempt left behind.
            await asyncio.to_thread(sync_workspace, self.build, self.store, self.library, project_id, actor="system")
            for number in itertools.count(1):                      # ends only below: `cap` fixes means up to cap + 1 runs of the tests, the last one judging the last fix
                if meter is not None and (why := meter.should_stop()):
                    final = (STOPPED, f"the run was stopped ({why}) before round {number}")
                    break
                last = await asyncio.to_thread(self._run_tests, project_id)
                state = classify(last)
                shot = fingerprint(last)
                counts = dict(passed=last.passed, failed=last.failed, errors=last.errors, skipped=last.skipped)

                if state != "broken":
                    outcome = {"ok": "passed", "blocked": "blocked", "no_tests": "no_tests"}[state]
                    self._record(project_id, run_id, loop_id, number, "", None, outcome, last, shot, actor)
                    notes.append(RoundNote(number, "", None, outcome, **counts, fingerprint=shot))
                    final = {
                        "ok": (CONVERGED, f"all {last.passed} test(s) passed after {number} run(s) of the tests"),
                        "blocked": (BLOCKED, last.reason or "the sandbox cannot run anything, so the tests could not be judged"),
                        "no_tests": (NO_TESTS, "no tests were found, which is never reported as a pass"),
                    }[state]
                    break

                if shot and shot == previous_print:
                    self._record(project_id, run_id, loop_id, number, "", None, "not_converging", last, shot, actor)
                    notes.append(RoundNote(number, "", None, "not_converging", **counts, fingerprint=shot))
                    final = (NOT_CONVERGING, f"the same {len(last.failures)} test(s) failed twice running, so another round would repeat itself")
                    break

                if number == cap + 1:
                    self._record(project_id, run_id, loop_id, number, "", None, "cap_reached", last, shot, actor)
                    notes.append(RoundNote(number, "", None, "cap_reached", **counts, fingerprint=shot))
                    final = (CAP_REACHED, f"{last.failed + last.errors} of {last.total} test(s) still failing after {cap} fix round(s)")
                    break

                artefact = route(last) or BACKEND
                skill = self.library.get(artefact)
                emit("build.round", round_event(number, artefact, last.failed, last.errors))
                try:
                    saved = await self.runtime.run(
                        project_id, artefact, feedback=fix_feedback(last, artefact, number, cap), previous_draft=self._previous(project_id, skill),
                        run_id=run_id, on_call=on_call,
                        extra_check=make_extra_check(self.build, self.store, self.library, project_id, skill, actor=f"agent:{skill.artefact.role}@loop"),
                    )
                except OutputInvalid as exc:
                    self._record(project_id, run_id, loop_id, number, artefact, None, "invalid", last, shot, actor)
                    notes.append(RoundNote(number, artefact, None, "invalid", **counts, fingerprint=shot))
                    final = (INVALID, f"the {skill.artefact.name.lower()} could not be fixed: {str(exc)[:200]}")
                    break
                except RunStopped as exc:
                    self._record(project_id, run_id, loop_id, number, artefact, None, "stopped", last, shot, actor)
                    notes.append(RoundNote(number, artefact, None, "stopped", **counts, fingerprint=shot))
                    final = (STOPPED, str(exc)[:200])
                    break
                self._record(project_id, run_id, loop_id, number, artefact, saved.version, "failed", last, shot, actor)
                notes.append(RoundNote(number, artefact, saved.version, "failed", **counts, fingerprint=shot))
                previous_print = shot
        finally:
            # Whatever happened, each folder goes back to its latest SAVED version.
            put_back = await asyncio.to_thread(sync_workspace, self.build, self.store, self.library, project_id, actor="system")

        cost = None if (not costs or any(c is None for c in costs)) else sum(costs)
        return LoopResult(final[0], final[1], tuple(notes), last, calls, cost, loop_id, put_back)

    def _previous(self, project_id: str, skill) -> str | None:
        from .inner import latest_body
        return latest_body(self.store, project_id, skill)

    def _record(self, project_id, run_id, loop_id, number, artefact, version, outcome, report: RunReport, shot: str, actor: str) -> None:
        self.store.build_rounds.append(
            project_id, loop_id, number, artefact=artefact, outcome=outcome, version=version, run_id=run_id, passed=report.passed, failed=report.failed,
            errors=report.errors, skipped=report.skipped, fingerprint=shot, failures=record_failures(report), seconds=report.seconds,
        )
        self.store.audit.append(
            actor=actor, action="build.round", target_type="build", target_id=f"{loop_id}#{number}", project_id=project_id,
            detail={"round": number, "artefact": artefact, "outcome": outcome, "passed": report.passed, "failed": report.failed, "errors": report.errors},
        )
