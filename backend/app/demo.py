"""The demo's recordings, and how to tell when a change to a prompt has made them stale (S36).

Replay answers a call only when the request is IDENTICAL to a recorded one (model, whole prompt, limits). So any edit to a skill, a rule or the sample text makes
some recorded calls miss, silently, until someone runs the demo. `StaleCheck` replays the whole demo path against the CURRENT code with a provider that answers from
the recordings when it can and, when it cannot, remembers the request and answers with the nearest recorded reply for the same role, so the run goes on and EVERY
miss is found, not only the first. It needs no key and calls no model.

What it proves: the prompts the code sends today match the recordings. What it does not prove: that a model would answer the same today. A staleness alarm, not a quality test.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Sequence

from .llm.errors import ReplayMiss
from .llm.providers.recording import ReplayProvider
from .llm.recordings import RecordingStore, request_key
from .llm.types import LLMRequest, ProviderResponse

DEMO_SET = "demo"
MAX_SHOWN_LINES = 8

# The script the demo was recorded with. The stages are cumulative; the note is part of the recorded prompts, so it must not change without a re-record.
# The demo runs Setup to Architecture to approval, then WRITES the Build brief and stops there (2026-11-23, the user's choice): Build can be marked done only after Build Verify has checked a real
# backend against the design, which a recording cannot do, and Deploy cannot start until Build is approved. So Deploy and Handover are not part of the demo.
PHASE_ORDER = ("setup", "requirements", "design", "architecture", "build")
STAGES = {1: ("setup", "requirements"), 2: ("design", "architecture"), 3: ("build",)}
LEFT_RUNNING = ("build",)                      # phases the demo writes and then leaves open: no review, no hand-over, no approval
SEND_BACK_NOTE = (
    "The brief says staff may book a room for up to eight hours but the policy note says four. State which one applies and mark the other as a conflict to resolve; "
    "do not pick one silently."
)
SEND_BACK_TYPE = "problem_statement"          # the artefact the script sends back, found by its type
MARKER = "recorded_stages.txt"                 # written by the recording driver: how many stages have been recorded


OUT_OF_DATE = ("The demo recordings no longer match this version of the app (a prompt changed since they were made). "
               "Run `python -m scripts.demo_record --check` in the backend folder to see what changed and which stage to re-record.")


class DemoUnavailable(Exception):
    """The prepared demo cannot be started, for a reason the person can act on (plain sentence)."""


def recorded_phases(settings) -> list[str]:
    """The phases the demo recordings cover, in order: [] when this server is not answering from a recorded demo set."""
    if settings.app_mode != "replay":
        return []
    marker = settings.recordings_path / MARKER
    try:
        stages = int(marker.read_text().strip())
    except (OSError, ValueError):
        return []
    return [phase for n in sorted(STAGES) if n <= stages for phase in STAGES[n]]


@dataclass(frozen=True)
class Miss:
    role: str
    model: str
    key: str
    system_diff: tuple[str, ...]      # lines that differ in the system message, against the nearest recorded request for the same role
    message_diff: tuple[str, ...]     # the same for the first user message
    nearest: str | None               # the key of that recording, None when the role has no recording at all

    def describe(self) -> str:
        if self.nearest is None:
            return f"{self.role} on {self.model}: no recording exists for this role at all"
        lines = [f"{self.role} on {self.model}: the prompt changed since it was recorded ({len(self.system_diff)} line(s) of the system message, {len(self.message_diff)} of the task)"]
        lines += [f"    {l[:200]}" for l in (*self.system_diff, *self.message_diff)[:MAX_SHOWN_LINES]]
        return "\n".join(lines)


@dataclass
class StaleReport:
    calls: int = 0
    misses: list[Miss] = field(default_factory=list)

    @property
    def fresh(self) -> bool:
        return not self.misses

    def sentence(self) -> str:
        if self.fresh:
            return f"the demo is current: all {self.calls} request(s) match a recording"
        first = self.misses[0]
        return (f"the demo needs a re-record: {len(self.misses)} of {self.calls} request(s) changed since the recordings were made. "
                f"The first is for {first.role}.\n{first.describe()}\nRe-record the stage it belongs to (1 Setup and Requirements, 2 Design and Architecture, 3 Build, written and left running) with: python -m scripts.demo_record --stage N\n"
                f"Calls already recorded are replayed free, so only what changed is paid for.")


def _diff(old: str, new: str) -> tuple[str, ...]:
    out = [l for l in difflib.unified_diff(old.split("\n"), new.split("\n"), "recorded", "now", lineterm="", n=0) if not l.startswith(("---", "+++", "@@"))]
    return tuple(out)


class StaleProvider:
    """Answers from the recordings; on a miss, notes it and answers with the nearest recorded reply for the same role so the run can continue."""

    label = "stale-check"

    def __init__(self, store: RecordingStore, entries: Sequence) -> None:
        self._replay = ReplayProvider(store)
        self._entries = list(entries)
        self.report = StaleReport()

    def _nearest(self, request: LLMRequest, model_id: str):
        same_role = [r for r in self._entries if r.role == request.role]
        if not same_role:
            return None
        text = request.system + (request.messages[0].content if request.messages else "")
        return max(same_role, key=lambda r: difflib.SequenceMatcher(None, r.system + (r.messages[0][1] if r.messages else ""), text).quick_ratio())

    async def complete(self, model_id: str, request: LLMRequest) -> ProviderResponse:
        self.report.calls += 1
        try:
            return await self._replay.complete(model_id, request)
        except ReplayMiss:
            near = self._nearest(request, model_id)
            self.report.misses.append(Miss(
                role=request.role, model=model_id, key=request_key(model_id, request),
                system_diff=_diff(near.system, request.system) if near else (),
                message_diff=_diff(near.messages[0][1], request.messages[0].content) if near and near.messages and request.messages else (),
                nearest=near.key if near else None,
            ))
            if near is None:
                raise
            return ProviderResponse(near.text, near.usage, near.finish_reason)


def stale_provider_for(folder) -> StaleProvider:
    store = RecordingStore(folder)
    return StaleProvider(store, store.entries())


# ---- the prepared demo: replay the script's steps through the app's own parts, as the person who asked, up to a chosen phase -----------------------------------

async def prepare(state, actor: str, through: str) -> str:
    """Create the sample project and play the recorded script up to `through`: every earlier phase is run, reviewed, (Requirements: sent back once with the reviewer's findings and
    rewritten,) handed over and approved; `through` itself is run, reviewed and handed to review, and left for the person to approve. Returns the project id.

    Everything is done as `actor` through the same objects the screen's buttons use, so the audit trail is true: this person asked for each step. The roles are checked BEFORE
    anything is created (a person who cannot approve a phase is told which role is needed and nothing is made). If a step fails, for instance because the recordings are stale,
    the half-made project is archived (never deleted) and the reason is given."""
    from .sample import BRIEF, FILENAME, NAME, POLICY, SELECTION
    settings = state.settings
    if settings.app_mode != "replay":
        raise DemoUnavailable("The prepared demo is available only when the server answers from recordings (APP_MODE=replay).")
    phases = recorded_phases(settings)
    if not phases:
        raise DemoUnavailable("There is no recorded demo to start from: set REPLAY_SET to the recorded demo set and restart.")
    if through not in phases:
        raise DemoUnavailable(f"The demo is recorded through {phases[-1]}; {through!r} is not one of its phases ({', '.join(phases)}).")
    steps = phases[: phases.index(through) + 1]
    human, runner, engine, store, reviews = state.human, state.runner, state.engine, state.store, state.reviews
    for phase in steps[:-1]:
        human.approving_role(actor, phase, "approved")
    if "requirements" in steps:
        human.approving_role(actor, "requirements", "sent back")

    project = store.projects.create(name=NAME, brief=BRIEF, mode="replay", selection=SELECTION, actor=actor)
    engine.open_project(project.id)
    store.context.add_document(project.id, FILENAME, POLICY, actor=actor)
    pid = project.id
    try:
        for phase in steps:
            result = await runner.run_phase(pid, phase)
            if result.status not in ("finished", "partial"):
                reasons = "; ".join(f"{o.key}: {o.reason}" for o in result.outcomes if o.state != "done" and o.reason)
                if "no recording" in reasons:
                    raise DemoUnavailable(OUT_OF_DATE)           # the runner reports a miss as a failed artefact, not as an exception, so it has to be recognised here too
                raise DemoUnavailable(f"{phase} did not finish ({result.status}). {reasons[:300]}".strip())
            if phase in LEFT_RUNNING:
                continue                                         # written, and left for the person: Build waits for Build Verify
            engine.finish(pid, phase, actor="system")
            for artefact in store.artefacts.list(pid, phase_key=phase):
                if artefact.latest_version is not None and not reviews.is_retired(pid, artefact.id):
                    await reviews.review(pid, artefact.id, actor=actor)
            if phase == "requirements":
                target = next(a for a in store.artefacts.list(pid, phase_key=phase) if a.type == SEND_BACK_TYPE)
                human.request_changes(pid, phase, actor=actor, notes={target.id: SEND_BACK_NOTE}, include_findings=[target.id])
                rerun = await human.rerun_rejected(pid, phase, actor=actor)
                if rerun.failed:
                    raise DemoUnavailable(f"the rewrite after the send-back failed: {'; '.join(rerun.failed.values())[:300]}")
                engine.finish(pid, phase, actor="system")
            if phase != through:
                human.approve(pid, phase, actor=actor)
    except Exception as exc:                                             # noqa: BLE001 - whatever went wrong, do not leave a half-made project in the list
        try:
            store.projects.archive(pid, actor=actor)
        except Exception:                                                # noqa: BLE001 - the original reason matters more
            pass
        if isinstance(exc, DemoUnavailable):
            raise
        detail = str(exc)
        if "no recording" in detail:
            raise DemoUnavailable(OUT_OF_DATE) from exc
        raise DemoUnavailable(f"The demo could not be prepared at {phase}: {type(exc).__name__}: {detail[:300]}") from exc
    return pid
