"""How the Build phase plugs into the phase runner (S17c-2).

The runner knows nothing about code. It asks a `BuildHooks` two questions: "does this artefact need an extra check on its reply?" (the four code artefacts
do: their files must write and pass their real check) and "what do you want to do once an artefact is saved?" (after the test suite, run the fix loop
once). Every other phase passes no hooks, so it runs exactly as before.

The loop's ending is REPORTED, never raised and never an approval: a person reviews the phase whatever it says.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from .files import KINDS
from .report import REPORT_AUTHOR, build_report, coverage, ending_of, last_loop
from . import code_review, review_store
from .inner import latest_body, make_extra_check, sync_workspace
from .loop import CONVERGED, FixLoop, LoopResult
from .service import BuildService
from .brief import KEY as INSTRUCTIONS_KEY

PHASE = "build"
LOOP_AFTER = "test_suite"             # the loop needs the backend AND the tests saved; the tests wait for the backend, so this is the last of them
NEEDED_FOR_LOOP = ("backend_code", "test_suite")
HOOK_WRITES = ("test_report", "code_security_review")   # written by this module after the loop: the report (S18a) and the two code reviews (S18b)
NEEDED_FOR_REVIEW = ("backend_code", "frontend_code", "test_suite")


@dataclass
class BuildOutcome:
    """What the fix loop did in a run, in the words the run result and the events use."""
    ran: bool
    result: str                  # converged | cap_reached | ... | skipped
    reason: str
    rounds: int = 0
    calls: int = 0
    cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.result == CONVERGED


class BuildHooks:
    def __init__(self, loop: FixLoop, build: BuildService, store, library, settings, review_slots=code_review.SLOTS, composer=None):
        self.composer = composer                       # the SetComposer (S19b), or None: then the instruction set is not written here
        self.review_slots = tuple(review_slots)        # which reviewers run after the loop; () runs none
        self.loop = loop
        self.build = build
        self.store = store
        self.library = library
        self.settings = settings

    def wants(self, phase_key: str) -> bool:
        """Does the code generator's fix loop run for this phase? Only when the catalogue being run HAS the code it works on (S19b: the live Build phase
        writes instruction files, so the loop, the test report and the code reviews stay quiet; the frozen catalogue in `legacy` still runs them)."""
        return phase_key == PHASE and all(self.library.get(k) is not None for k in NEEDED_FOR_LOOP)

    def writes(self, phase_key: str) -> tuple[str, ...]:
        """The artefacts of this phase that the hook writes itself, so the runner does not try to write them as model work."""
        return HOOK_WRITES if self.wants(phase_key) else ()

    def composes(self, phase_key: str, artefact_key: str) -> bool:
        """Is this artefact written in pieces by the composer (the instruction set) instead of by one model call?"""
        return self.composer is not None and phase_key == PHASE and artefact_key == INSTRUCTIONS_KEY

    async def compose(self, project_id: str, run_id: str, meter, emit: Callable[[str, dict], None], categories=None, again=None, screen_map=None):
        """Write the chosen categories of the Build brief (S45: all of them when none are named) and save what passed as one version. Returns the AgentResult of the save; raises
        like a single-call artefact would, or `BriefPartlySaved` / `RunStopped` (with `saved`) when some files passed and were kept."""
        composed = await self.composer.compose(project_id, run_id=run_id, meter=meter, emit=emit, categories=categories, again=again, screen_map=screen_map)
        return composed.result

    def category_problems(self, project_id: str, categories, again: bool) -> list[str]:
        """Why the chosen categories cannot be built now, in words (an unknown one, one the design lacks, one whose inputs are not written, one with nothing to write). Reads only."""
        from . import brief as B
        return B.category_problems(categories, self.composer.categories_now(project_id), again)

    async def categories(self, project_id: str):
        """The categories of the instruction set as it stands (S44): each file saved, missing or older, and whether the category can be built now. Reads only."""
        return await self.composer.categories(project_id)

    def extra_check(self, phase_key: str, artefact_key: str, project_id: str):
        """The 17b check for a code artefact, or None for anything else."""
        if phase_key != PHASE or artefact_key not in KINDS:
            return None
        skill = self.library.get(artefact_key)           # every key in KINDS has a skill (a test pins this), so there is no 'missing skill' case to handle
        return make_extra_check(self.build, self.store, self.library, project_id, skill, actor=f"agent:{skill.artefact.role}@build")

    async def after_artefacts(
        self, project_id: str, run_id: str, done: set[str], failed: dict[str, str], skipped: dict[str, str], meter,
        on_event: Callable[[str, dict], None], stop_reason: str | None = None,
    ) -> BuildOutcome:
        """The end of a Build run: run the fix loop once if everything it needs was saved, otherwise say why not. Either way, the workspace ends equal to
        the latest SAVED version of every component, because an artefact that failed or was dropped leaves its last unsaved attempt lying in its folder."""
        if stop_reason is not None:
            await self._put_back(project_id)
            return BuildOutcome(False, "skipped", f"the tests were not run because the run was stopped ({stop_reason})")
        missing = [k for k in NEEDED_FOR_LOOP if k not in done]
        if missing:
            await self._put_back(project_id)
            why = "; ".join(f"{k} {failed.get(k) or skipped.get(k) or 'was not written in this run'}" for k in missing)
            return BuildOutcome(False, "skipped", f"the tests were not run because {why}")
        result: LoopResult = await self.loop.run(project_id, run_id=run_id, meter=meter, actor="system", on_event=on_event)   # puts the workspace back itself
        await self.write_report(project_id)
        reviewed = await self.review_code(project_id, run_id, done, meter, self.review_slots)
        return BuildOutcome(True, result.result, result.reason, rounds=len(result.rounds), calls=result.calls + reviewed, cost_usd=result.cost_usd)

    async def review_code(self, project_id: str, run_id: str | None, done: set[str], meter, slots=code_review.SLOTS) -> int:
        """Both code reviewers read the saved code, together, once each, and the results are saved as ONE new version of the review artefact.
        Returns the number of model calls made. Skipped (and nothing saved) if the code the review waits for is not all there, or there is no code."""
        if not slots or any(k not in done for k in NEEDED_FOR_REVIEW):
            return 0
        runtime = self.loop.runtime
        bodies = {key: await asyncio.to_thread(_latest, self.store, project_id, self.library.get(key)) for key in code_review.CODE_ARTEFACTS}
        code = code_review.gather_code(bodies)
        if not code.files:
            return 0
        authors = await asyncio.to_thread(_authors, self.store, project_id, self.library)
        brief = (await asyncio.to_thread(self.store.projects.get, project_id)).brief
        calls = 0

        def on_call(result, attempt, problems):
            nonlocal calls
            calls += 1
            if meter is None:
                return None
            meter.add_call(result.cost_usd, result.tokens_in, result.tokens_out)
            return meter.should_stop()
        if meter is not None and meter.should_stop():
            return 0
        results = await code_review.run_both(runtime.gateway, self.settings, code, brief, authors, project_id=project_id, run_id=run_id, on_call=on_call,
                                             scanner=runtime.scanner, slots=slots)
        await asyncio.to_thread(review_store.save, self.store, self.library, project_id, results, run_id=run_id)
        return calls

    async def write_report(self, project_id: str) -> int | None:
        """Write the test report from the recorded rounds as a new version of `test_report`. No model. Returns the version number."""
        return await asyncio.to_thread(self._write_report, project_id)

    def _write_report(self, project_id: str) -> int | None:
        skill_tests, skill_design = self.library.get("test_suite"), self.library.get("low_level_design")
        loop = last_loop(self.store.build_rounds.list(project_id))
        tests_body = _latest(self.store, project_id, skill_tests)
        design_body = _latest(self.store, project_id, skill_design)
        cover = coverage(design_body, skill_design.items, tests_body, skill_tests.items) if skill_design and skill_tests else None
        body = build_report(loop, tests_body, skill_tests.items if skill_tests else (), cover)
        artefact = self.library.get("test_report")
        _, version = self.store.artefacts.create_and_add_version(
            project_id, artefact_type="test_report", phase_key=PHASE, title=artefact.artefact.name if artefact else "Test report", prefix="BLD-RPT",
            body=body, author=REPORT_AUTHOR, generation={"skill": "test_report", "written_by": "system", "ending": ending_of(loop), "rounds": len(loop)},
        )
        return version.number

    async def _put_back(self, project_id: str) -> None:
        await asyncio.to_thread(sync_workspace, self.build, self.store, self.library, project_id, actor="system")


def _latest(store, project_id: str, skill) -> str | None:
    if skill is None:
        return None
    from .inner import latest_body
    return latest_body(store, project_id, skill)


def _authors(store, project_id: str, library) -> list[str]:
    """The models that wrote the code the reviewers read (for the independence label). A person's edit counts as a person."""
    out: list[str] = []
    for key in code_review.CODE_ARTEFACTS:
        skill = library.get(key)
        found = store.artefacts.list(project_id, phase_key=PHASE, artefact_type=key)
        if skill is None or not found or found[0].latest_version is None:
            continue
        author = store.artefacts.get_version(project_id, found[0].id).author
        out.append(author.split("@", 1)[1] if author.startswith("agent:") and "@" in author else code_review.HUMAN)
    return out
