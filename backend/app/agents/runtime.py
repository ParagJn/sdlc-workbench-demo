"""The agent runtime: one artefact, from skill and context to a validated, saved version.

No parallelism and no review here (S09 and S08). It runs one artefact, checks what comes back, asks for
a fix when the check fails, and saves only a reply that passed.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Mapping, Sequence

from ..config import Settings
from ..llm.gateway import Gateway
from ..llm.types import LLMRequest, LLMResult, Message
from ..models import NotFoundError, Project
from ..repos import Store
from ..workflow.definitions import Catalogue
from ..workflow.engine import WorkflowEngine
from .errors import (
    AgentError,
    RunStopped,
    ArtefactNotInPlan,
    InputTooLarge,
    MissingInput,
    NoSkill,
    OutputInvalid,
    PhaseNotRunning,
)
from ..guardrails.scan import Scanner, keepable
from .checks import check_body
from .limits import near_limit
from ..build.brief import KEY as INSTRUCTIONS_KEY
from . import batch as B
from .items import Item, ItemContext, history_of, parse_items, split_id, upstream_pool
from .replaces import strip_replaces
from .notes import unacted_notes
from .prompt import (
    Feedback,
    Prompt,
    Upstream,
    brief_queries,
    build_prompt,
    echo_for_repair,
    repair_message,
    select_hits,
)
from .reply import CUT_OFF_BEFORE_CLOSING, MAX_PROBLEMS, TRUNCATED_REASONS, ReplyFormatError, cited_ids, parse_reply, section_bodies
from .skills import Skill, SkillLibrary

# S39: on a REWRITE, the stories-per-feature count the writer was shown is not held against it (it keeps earlier stories on purpose); a feature that GROWS still is. Every other limit applies in full.
REWRITE_WAIVED = frozenset({"L10"})


@dataclass(frozen=True)
class AgentResult:
    artefact_id: str
    version: int
    author: str
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_usd: float | None  # None when any attempt's cost is unknown; never a partial sum passed off as a total
    citations: tuple[str, ...]
    call_ids: tuple[str, ...]
    served_models: tuple[str, ...]
    created_artefact: bool
    items: tuple[str, ...] = ()  # item IDs the saved version holds
    links: int = 0  # derives-from links the save declared (repeats of an existing link included)
    flags: int = 0  # personal-data findings saved with the version (secrets never get this far)


MAX_LOGGED_PROBLEM_CHARS = 400   # a problem can quote a few words of the reply; the record stays bounded either way


def logged_problems(attempt: int, call_id: str, problems: Sequence[str]) -> dict:
    """One failed attempt as it is kept on the saved version: which attempt, which call, and what was wrong, in the words
    that were sent back to the model. Stored with the version (never published as an event)."""
    return {"attempt": attempt, "call_id": call_id, "problems": [p[:MAX_LOGGED_PROBLEM_CHARS] for p in problems]}


def _marked(log: Sequence[Mapping], part: int, parts: int) -> list[dict]:
    """A repair log with each problem marked by the part it came from."""
    return [{**entry, "problems": [f"(part {part} of {parts}) {p}" for p in entry["problems"]]} for entry in log]


def sum_costs(costs: Sequence[float | None]) -> float | None:
    """A total is only a total if every part is known."""
    if not costs or any(c is None for c in costs):
        return None
    return sum(costs)  # type: ignore[arg-type]


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        gateway: Gateway,
        catalogue: Catalogue,
        engine: WorkflowEngine,
        skills: SkillLibrary,
    ):
        self.settings = settings
        self.store = store
        self.gateway = gateway
        self.catalogue = catalogue
        self.engine = engine
        self.skills = skills
        self.scanner = Scanner.from_settings(settings)

    # ---- the public entry point -------------------------------------------------------------

    async def run(
        self,
        project_id: str,
        artefact_key: str,
        *,
        feedback: Sequence[str] = (),
        previous_draft: str | None = None,
        run_id: str | None = None,
        overrides: Mapping[str, str] | None = None,
        on_call: Callable[[LLMResult, int, list[str]], str | None] | None = None,
        extra_check: Callable[[str], Awaitable[Sequence[str]]] | None = None,
    ) -> AgentResult:
        """Generate one artefact and save it as a new version. Raises (and saves nothing) on any failure.

        `on_call(result, attempt, problems)` is told after every model call, with the problems found in that
        reply. A phase run uses it to count cost as it happens. If it returns a reason ("budget", "time" or
        "cancelled") the artefact is dropped unsaved at once, so a repair loop cannot spend past a cap.

        `extra_check(body)` runs on a reply that passed every ordinary check, and returns more problems in plain sentences (or none). They are
        sent back exactly like any other problem, so what it finds is repaired in the same conversation and nothing bad is ever saved. A Build
        artefact uses it to really try its files (S17b). It is never called on a reply that already has problems, and None means no extra check."""
        if artefact_key == INSTRUCTIONS_KEY:
            # The Build brief is assembled from the design and written piece by piece by the Build phase run, never by one reply.
            raise AgentError("the Build brief is written file by file when the Build phase is run; run the Build phase to write or rewrite it")
        project, skill = await asyncio.to_thread(self._check_runnable, project_id, artefact_key)
        groups = await asyncio.to_thread(self._parts_for, project, skill)
        if groups:
            return await self._run_in_parts(project, skill, groups, feedback, previous_draft, run_id, overrides, on_call, extra_check)
        staged = await asyncio.to_thread(self._stages_for, project, skill)
        if staged:
            return await self._run_staged(project, skill, staged, feedback, previous_draft, run_id, overrides, on_call, extra_check)
        prompt, item_ctx = await asyncio.to_thread(self._assemble, project, skill, feedback, previous_draft)
        calls, repair_log, body = await self.converse(
            project_id, skill, prompt, lambda result: self._check(result, skill, prompt, item_ctx, self.scanner, self.enforces_limits, previous_draft),
            run_id=run_id, overrides=overrides, on_call=on_call, extra_check=extra_check,
        )
        return await asyncio.to_thread(self._save, project_id, skill, body, calls, prompt, run_id, item_ctx, repair_log)

    async def converse(
        self,
        project_id: str,
        skill: Skill,
        prompt: Prompt,
        check: Callable[[LLMResult], tuple[list[str], str]],
        *,
        run_id: str | None = None,
        overrides: Mapping[str, str] | None = None,
        on_call: Callable[[LLMResult, int, list[str]], str | None] | None = None,
        extra_check: Callable[[str], Awaitable[Sequence[str]]] | None = None,
        label: str | None = None,
    ) -> tuple[list[LLMResult], list[dict], str]:
        """The model conversation for ONE reply: call, check, send the problems back, up to the repair budget. Returns (the calls, what was wrong with each
        attempt that had to be repaired, the body of the accepted reply). Raises (and saves nothing) when no attempt is accepted.

        `check(result)` returns (problems, body). `label` names what is being written in the messages (default: the artefact's name); the Build instruction
        set writes one file per conversation and names the file."""
        what = label or skill.artefact.name
        calls: list[LLMResult] = []
        repair_log: list[dict] = []   # what was wrong with each attempt that had to be repaired, kept with the saved version
        messages = [Message("user", prompt.user)]
        problems: list[str] = []
        body = ""
        max_attempts = self.settings.agent_max_repairs + 1
        for attempt in range(1, max_attempts + 1):
            result = await self.gateway.complete(
                LLMRequest(
                    role=skill.artefact.role,
                    messages=tuple(messages),
                    system=prompt.system,
                    max_tokens=self.settings.output_tokens_for(skill.artefact.role),
                    project_id=project_id,
                    phase_key=skill.artefact.phase_key,
                    run_id=run_id,
                ),
                overrides=overrides,
            )
            calls.append(result)
            problems, body = check(result)
            if not problems and extra_check is not None:
                problems = [str(p) for p in await extra_check(body)][:MAX_PROBLEMS]
            if problems:
                repair_log.append(logged_problems(attempt, result.call_id, problems))
            if on_call is not None:
                stop = on_call(result, attempt, problems)
                if stop is not None:
                    raise RunStopped(
                        f"'{what}' was dropped unsaved: the run was stopped ({stop}) after attempt {attempt}", stop,
                    )
            if not problems:
                break
            if attempt < max_attempts:
                messages += [Message("assistant", echo_for_repair(result.text)), Message("user", repair_message(problems))]
        else:
            raise OutputInvalid(
                f"'{what}' could not be produced: after {max_attempts} attempt(s) the reply still "
                f"failed its checks ({'; '.join(problems)}). Nothing was saved.",
                problems, max_attempts,
                cut_off=calls[-1].finish_reason in TRUNCATED_REASONS, limit=self.settings.output_tokens_for(skill.artefact.role),
                attempt_log=repair_log, last_reply=keepable(calls[-1].text, self.scanner, self.settings.keep_refused_reply_chars),
            )
        return calls, repair_log, body

    # ---- an artefact written in parts (user stories for a big backlog) ---------------------------------------

    def _parts_for(self, project: Project, skill: Skill) -> list[tuple[str, ...]]:
        """The groups of covered items (features) to write one part each for. [] when the artefact is written in one call: not a batched skill, or few enough items."""
        if skill.key not in B.BATCHED_SKILLS or not skill.covers or not skill.items:
            return []
        upstream = self._upstream(project.id, skill)
        ctx, _ = self._item_context(project.id, skill, upstream, self._own_artefact_id(project.id, skill))
        pool = [i for prefix in skill.covers for i in (ctx.pools.get(prefix, ()) if ctx else ())]
        size = getattr(self.settings, B.PART_SIZE_SETTING[skill.key])
        return B.split_evenly(pool, size) if len(pool) > size else []

    def _stages_for(self, project: Project, skill: Skill) -> list[tuple[str, ...]]:
        """The groups of covered items (requirements) to write one part each for, when the artefact is written in STAGES (S41). [] when it is written in one call: not a staged skill,
        or few enough requirements. A document of ten requirements or fewer is therefore exactly what it was."""
        if B.staging_for(skill.key, project.kind) is None or not skill.covers or not skill.items:
            return []
        upstream = self._upstream(project.id, skill)
        ctx, _ = self._item_context(project.id, skill, upstream, self._own_artefact_id(project.id, skill))
        pool = [i for prefix in skill.covers for i in (ctx.pools.get(prefix, ()) if ctx else ())]
        size = self.settings.agent_batch_requirements
        return B.split_evenly(pool, size) if len(pool) > size else []

    @staticmethod
    def _narrow_stage(whole: ItemContext, stage: B.StageCall) -> ItemContext:
        """The item rules for ONE call of a staged artefact: its own covered items, its own earlier IDs (every other earlier ID is taken), and its numbering floor per kind."""
        changes: dict = {}
        if stage.pools is not None:
            changes["pools"] = {**whole.pools, **stage.pools}
        if stage.current is not None:
            ever = whole.current | whole.retired
            changes.update(current=stage.current, retired=frozenset(ever - stage.current), previous=tuple(i for i in whole.previous if i.id in stage.current))
        if stage.highest is not None:
            # The floor is above EVERYTHING the saved version used, as well as what this run has written so far. The two differ on a rewrite: a part that owns few earlier items is written
            # before the later parts that own the higher numbers, so "the highest written so far" can be a number the saved version already gave to a later part (FieldOps, part 3 of 9 told to
            # start at API-018 while API-021 to 057 were taken: three repairs, $1.71, nothing saved).
            changes["highest"] = {**whole.highest, **{prefix: max(number, whole.highest.get(prefix, 0)) for prefix, number in stage.highest.items()}}
        return replace(whole, **changes)

    @staticmethod
    def _narrow(whole: ItemContext, part: B.Part, skill: Skill) -> ItemContext:
        """The item rules for ONE part: only its own features need covering, only its own earlier items are expected back, every other earlier ID is taken, and numbering
        carries on from the highest number used so far (earlier parts included)."""
        ever = whole.current | whole.retired
        pools = {prefix: tuple(i for i in part.features if split_id(i)[0] == prefix) for prefix in skill.covers}
        highest = {**whole.highest, **{spec.prefix: max(whole.highest.get(spec.prefix, 0), part.highest) for spec in skill.items[:1]}}
        return replace(
            whole, pools=pools, current=part.current, retired=frozenset(ever - part.current), highest=highest,
            previous=tuple(i for i in whole.previous if i.id in part.current),
        )

    async def _run_in_parts(
        self, project: Project, skill: Skill, groups: Sequence[tuple[str, ...]], feedback: Sequence[str], previous_body: str | None, run_id: str | None,
        overrides: Mapping[str, str] | None, on_call, extra_check,
    ) -> AgentResult:
        """Write an artefact one group of features at a time, join the parts into ONE document, and check the whole of it exactly as a single reply is checked.
        Each part is its own conversation with its own repairs; parts run one after another because each carries on the numbering of the one before.
        A part that cannot be accepted fails the artefact (nothing is saved), and the reason names the part and its features."""
        upstream = await asyncio.to_thread(self._upstream, project.id, skill)
        whole, _ = await asyncio.to_thread(self._item_context, project.id, skill, upstream, await asyncio.to_thread(self._own_artefact_id, project.id, skill))
        assert whole is not None
        section = skill.items[0].section
        previous_items = parse_items(previous_body, skill.items).items if previous_body else ()
        owned = B.assign_previous(previous_items, groups)
        calls: list[LLMResult] = []
        repair_log: list[dict] = []
        bodies: list[str] = []
        allowed: dict[str, None] = {}
        highest = 0                       # the highest number used by the parts written so far; _narrow takes the larger of this and the earlier versions' highest
        last_prompt: Prompt | None = None
        for k, features in enumerate(groups, start=1):
            part = B.Part(k, len(groups), features, owned[k - 1], highest, B.draft_for(previous_body, section, owned[k - 1]))
            prompt, item_ctx = await asyncio.to_thread(self._assemble, project, skill, feedback, previous_body, part, whole)
            allowed.update(dict.fromkeys(prompt.allowed))
            last_prompt = prompt
            try:
                part_calls, part_log, body = await self.converse(
                    project.id, skill, prompt, lambda result, prompt=prompt, ctx=item_ctx: self._check(result, skill, prompt, ctx, self.scanner, self.enforces_limits, previous_body),
                    run_id=run_id, overrides=overrides, on_call=on_call, extra_check=extra_check,
                    label=f"{skill.artefact.name}, part {k} of {len(groups)} ({features[0]} to {features[-1]})",
                )
            except OutputInvalid as exc:
                # The log of a failed artefact is every attempt of every part so far, each problem marked with its part (S32).
                raise OutputInvalid(
                    str(exc), list(exc.problems), exc.attempts, cut_off=exc.cut_off, limit=exc.limit,
                    attempt_log=[*repair_log, *_marked(exc.attempt_log, k, len(groups))], last_reply=exc.last_reply,
                ) from exc
            calls += part_calls
            repair_log += _marked(part_log, k, len(groups))
            bodies.append(body)
            new = [i for i in parse_items(body, skill.items).items if i.prefix == skill.items[0].prefix]
            highest = max([highest, *[split_id(i.id)[1] for i in new]])
        joined = B.merge_parts(
            bodies, skill.required_sections, section,
            stories=sum(len(parse_items(b, skill.items).items) for b in bodies),
            features=sum(len(g) for g in groups), size=getattr(self.settings, B.PART_SIZE_SETTING[skill.key]), nouns=B.PART_NOUNS[skill.key],
        )
        assert last_prompt is not None
        full_ctx = replace(whole, known=whole.known | frozenset(u.artefact_id for u in upstream) | frozenset(allowed))
        problems = check_body(joined, skill, tuple(allowed), full_ctx, None, self.scanner, limits=self.enforces_limits, previous_body=previous_body, waive_rules=REWRITE_WAIVED)
        if problems:
            raise OutputInvalid(
                f"'{skill.artefact.name}' could not be produced: the parts were each accepted, but the joined document failed its checks ({'; '.join(problems)}). Nothing was saved.",
                problems, len(calls), cut_off=False, limit=self.settings.output_tokens_for(skill.artefact.role),
                attempt_log=[*repair_log, {"attempt": len(calls), "call_id": None, "problems": [f"(joined document) {p}" for p in problems]}],
            )
        extra = {"parts": {"count": len(groups), "features": [list(g) for g in groups]}}
        return await asyncio.to_thread(
            self._save, project.id, skill, joined, calls, last_prompt, run_id, full_ctx, repair_log, extra,
        )

    async def _run_staged(
        self, project: Project, skill: Skill, groups: Sequence[tuple[str, ...]], feedback: Sequence[str], previous_body: str | None, run_id: str | None,
        overrides: Mapping[str, str] | None, on_call, extra_check,
    ) -> AgentResult:
        """Write an artefact in STAGES (S41) and join them into ONE document, checked exactly as a single reply is checked.

        Stage 1 writes the bounded, shared part once (for the Low-level design: the tables and how they relate). The later parts, one per group of covered items (requirements), write
        the part that grows without limit (endpoints and rules) and are SHOWN the finished stage 1, so no part can invent a second table. Each call is its own conversation with its own
        repairs, narrowed to its own sections and item kinds so the ordinary prompt and checks apply to it unchanged. A stage that cannot be accepted fails the artefact (nothing is
        saved), and the reason names the stage."""
        sections1, prefixes1, sections2, prefixes2 = B.staging_for(skill.key, project.kind)
        upstream = await asyncio.to_thread(self._upstream, project.id, skill)
        whole, _ = await asyncio.to_thread(self._item_context, project.id, skill, upstream, await asyncio.to_thread(self._own_artefact_id, project.id, skill))
        assert whole is not None
        first_skill = B.narrowed_skill(skill, sections1, prefixes1, covers=False)
        later_skill = B.narrowed_skill(skill, sections2, prefixes2, covers=True)
        later_sections = tuple(spec.section for spec in later_skill.items)
        previous_items = parse_items(previous_body, later_skill.items).items if previous_body else ()
        owned = B.assign_previous(previous_items, groups)
        total = len(groups) + 1
        calls: list[LLMResult] = []
        repair_log: list[dict] = []
        allowed: dict[str, None] = {}
        last_prompt: Prompt | None = None
        highest: dict[str, int] = {}                 # per kind: the highest number used by the stages written so far

        async def write(index: int, narrow_skill: Skill, stage: B.StageCall, label: str, others: Sequence[str]) -> str:
            nonlocal last_prompt
            prompt, item_ctx = await asyncio.to_thread(self._assemble, project, narrow_skill, feedback, previous_body, None, whole, stage)
            allowed.update(dict.fromkeys(prompt.allowed))
            last_prompt = prompt
            try:
                stage_calls, stage_log, body = await self.converse(
                    project.id, narrow_skill, prompt, lambda result, prompt=prompt, ctx=item_ctx: self._check_stage(result, narrow_skill, prompt, ctx, previous_body, others),
                    run_id=run_id, overrides=overrides, on_call=on_call, extra_check=extra_check, label=label,
                )
            except OutputInvalid as exc:
                raise OutputInvalid(
                    str(exc), list(exc.problems), exc.attempts, cut_off=exc.cut_off, limit=exc.limit,
                    attempt_log=[*repair_log, *_marked(exc.attempt_log, index, total)], last_reply=exc.last_reply,
                ) from exc
            calls.extend(stage_calls)
            repair_log.extend(_marked(stage_log, index, total))
            for item in parse_items(body, narrow_skill.items).items:
                highest[item.prefix] = max(highest.get(item.prefix, 0), split_id(item.id)[1])
            return body

        # stage 1: the tables, once
        stage_one = B.StageCall(
            text=B.stage_one_note(prefixes1, prefixes2), scope=None, draft=B.draft_of_sections(previous_body, sections1),
            current=frozenset(i.id for i in parse_items(previous_body, first_skill.items).items) if previous_body else frozenset(),
        )
        first_body = await write(1, first_skill, stage_one, f"{skill.artefact.name}, stage 1 of {total} ({B.STAGE_ONE_LABEL.get(prefixes1[0], 'the shared part')})", later_sections)
        finished_items = tuple(parse_items(first_body, first_skill.items).items)
        finished_text = B.draft_of_sections(first_body, [s for s in sections1 if s not in ("Overview", "Open questions")]) or ""
        # the parts: endpoints and rules, one group of requirements each
        bodies: list[str] = []
        for k, features in enumerate(groups, start=1):
            stage = B.StageCall(
                text=B.part_note_staged(k, len(groups), features, prefixes2) + "\n\n" + B.finished_stage_note(finished_text, prefixes1),
                scope=features, draft=B.draft_for_sections(previous_body, later_sections, owned[k - 1]),
                pools={prefix: tuple(i for i in features if split_id(i)[0] == prefix) for prefix in skill.covers},
                current=owned[k - 1], highest=dict(highest), finished_items=finished_items,
            )
            bodies.append(await write(k + 1, later_skill, stage, f"{skill.artefact.name}, part {k} of {len(groups)} ({features[0]} to {features[-1]})", [spec.section for spec in first_skill.items] + [s for s in sections1 if s not in sections2]))
        counts = {prefix: sum(1 for b in bodies for i in parse_items(b, later_skill.items).items if i.prefix == prefix) for prefix in prefixes2}
        counts[prefixes1[0]] = sum(1 for i in finished_items if i.prefix in prefixes1)
        requirements = sum(len(g) for g in groups)
        overview = B.stage_overview(prefixes1, prefixes2, counts, requirements, total, len(groups), self.settings.agent_batch_requirements)
        joined = B.merge_stages(first_body, bodies, skill.required_sections, sections1, sections2, later_sections, overview)
        assert last_prompt is not None
        full_ctx = replace(whole, known=whole.known | frozenset(u.artefact_id for u in upstream) | frozenset(allowed))
        problems = check_body(joined, skill, tuple(allowed), full_ctx, None, self.scanner, limits=self.enforces_limits, previous_body=previous_body, waive_rules=REWRITE_WAIVED)
        if problems:
            raise OutputInvalid(
                f"'{skill.artefact.name}' could not be produced: the stages were each accepted, but the joined document failed its checks ({'; '.join(problems)}). Nothing was saved.",
                problems, len(calls), cut_off=False, limit=self.settings.output_tokens_for(skill.artefact.role),
                attempt_log=[*repair_log, {"attempt": len(calls), "call_id": None, "problems": [f"(joined document) {p}" for p in problems]}],
            )
        extra = {"parts": {"count": total, "features": [list(g) for g in groups], "staged": True}}
        return await asyncio.to_thread(self._save, project.id, skill, joined, calls, last_prompt, run_id, full_ctx, repair_log, extra)

    def _check_stage(self, result: LLMResult, skill: Skill, prompt: Prompt, item_ctx: ItemContext | None, previous_body: str | None, others: Sequence[str]) -> tuple[list[str], str]:
        """The ordinary reply check for one stage, plus: it may write only its own sections (S41)."""
        problems, body = self._check(result, skill, prompt, item_ctx, self.scanner, self.enforces_limits, previous_body)
        if body:
            problems = (problems + B.foreign_sections(body, skill.required_sections, others))[:MAX_PROBLEMS]
        return problems, body

    def ensure_runnable(self, project_id: str, artefact_key: str) -> tuple[Project, Skill]:
        """Raise, in plain words, unless this artefact may be written now (known, has a skill, switched on, its phase running). Returns the project and skill."""
        return self._check_runnable(project_id, artefact_key)

    def save_composed(
        self, project_id: str, skill: Skill, body: str, calls: Sequence[LLMResult], upstream: Sequence[Upstream], run_id: str | None,
        item_ctx: ItemContext | None, repair_log: Sequence[dict], extra_generation: Mapping,
    ) -> AgentResult:
        """Save a body that was written in several conversations (the Build instruction set) as one version, through the same save path as every artefact."""
        prompt = Prompt(system="", user="", allowed=(), shown_chunks=(), built_on=tuple((u.artefact_id, u.version) for u in upstream))
        return self._save(project_id, skill, body, calls, prompt, run_id, item_ctx, repair_log, extra_generation)

    # ---- before any model call ------------------------------------------------------------------

    def _check_runnable(self, project_id: str, artefact_key: str) -> tuple[Project, Skill]:
        project = self.store.projects.get(project_id)  # NotFoundError for an unknown project
        known = any(a.key == artefact_key for phase in self.catalogue.phases for a in phase.artefacts)
        if not known:
            raise ArtefactNotInPlan(f"'{artefact_key}' is not an artefact in the workflow catalogue")
        skill = self.skills.get(artefact_key, project.kind)           # S40: the wording written for this kind of project, or the ordinary skill
        if skill is None:
            raise NoSkill(f"there is no skill for '{artefact_key}' yet, so an agent cannot write it")
        plan = self.engine.plan_for(project)
        if not plan.phase(skill.artefact.phase_key).has(artefact_key):
            raise ArtefactNotInPlan(f"'{skill.artefact.name}' was switched off for this project")
        self._require_running(self.store.phases, project_id, skill)
        return project, skill

    @staticmethod
    def _require_running(phases, project_id: str, skill: Skill) -> None:
        try:
            status = phases.get(project_id, skill.artefact.phase_key).status
        except NotFoundError:
            status = "not_started"
        if status != "running":
            raise PhaseNotRunning(
                f"'{skill.artefact.name}' can only be written while its phase is running; "
                f"{skill.artefact.phase_key} is '{status}'"
            )

    # ---- gathering what the agent may use ---------------------------------------------------------------

    def _assemble(
        self, project: Project, skill: Skill, feedback: Sequence[str], previous_draft: str | None, part: B.Part | None = None, base: ItemContext | None = None,
        stage: B.StageCall | None = None,
    ) -> tuple[Prompt, ItemContext | None]:
        """The prompt and the item rules for one reply. For one part of an artefact written in parts, `part` and `base` (the whole artefact's item context) narrow them to that part."""
        upstream = self._upstream(project.id, skill)
        self.check_upstream_size(skill, upstream)
        own = self._own_artefact_id(project.id, skill)
        hits = []
        queries = list(skill.searches) + brief_queries(project.brief, self.settings.agent_brief_queries)
        for query in queries:
            hits.extend(self.store.context.search(project.id, query, limit=10, kinds=["document", "artefact"]))
        whole = self._whole_documents(project.id)
        chosen = select_hits(
            hits if whole is None else [h for h in hits if h.kind != "document"],          # documents shown whole need no ranking; approved work is still searched
            max_chunks=self.settings.agent_context_max_chunks,
            max_chars=self.settings.agent_context_max_chars,
            min_coverage=self.settings.agent_context_min_coverage,
            exclude_artefacts=[u.artefact_id for u in upstream] + ([own] if own else []),
        )
        if whole is not None:
            chosen = [*whole, *chosen]
        notes = [Feedback(text) for text in feedback if text and text.strip()]
        notes += self._memory_feedback(project.id, skill, own)
        item_ctx, upstream_items = self._item_context(project.id, skill, upstream, own)
        if part is not None and item_ctx is not None:
            item_ctx = self._narrow(base or item_ctx, part, skill)
        if stage is not None:
            if item_ctx is not None:
                item_ctx = self._narrow_stage(item_ctx, stage)
            upstream_items = [*upstream_items, *stage.finished_items]            # the finished stage-1 items are sources a later part may name
        responses, responses_note = self._responses(project.id, skill)
        prompt = build_prompt(
            skill, project_name=project.name, brief=project.brief, upstream=upstream,
            feedback=notes, previous_draft=stage.draft if stage is not None else part.draft if part is not None else previous_draft, hits=chosen,
            item_context=item_ctx, upstream_items=upstream_items, responses=responses, responses_note=responses_note, settled=self._settled(project.id, skill),
            part_scope=stage.scope if stage is not None else part.features if part is not None else None,
            part_text=stage.text if stage is not None else B.part_note(part, B.PART_NOUNS.get(skill.key, B.PART_NOUNS["user_stories"])[1]) if part is not None else None,
            not_provided=self.skipped_inputs(project.id, skill), kind=project.kind,
        )
        if item_ctx is not None:
            # "Derives from" may name anything the model was shown as a source, as well as upstream items.
            item_ctx = ItemContext(
                item_ctx.specs, item_ctx.covers, item_ctx.known | frozenset(prompt.allowed), item_ctx.pools,
                item_ctx.current, item_ctx.retired, item_ctx.highest, item_ctx.previous,
            )
        return prompt, item_ctx

    def check_upstream_size(self, skill: Skill, upstream: Sequence[Upstream]) -> None:
        """Refuse, in plain words, when the material a skill builds on is over the limit. Nothing is ever cut silently."""
        total = sum(len(u.text) for u in upstream)
        if total > self.settings.agent_upstream_max_chars:
            biggest = sorted(upstream, key=lambda u: -len(u.text))
            names = ", ".join(f"{u.title} ({len(u.text)} characters)" for u in biggest[:3])
            raise InputTooLarge(
                f"the artefacts '{skill.artefact.name}' builds on total {total} characters, over the "
                f"{self.settings.agent_upstream_max_chars} limit; the largest are {names}. Nothing is cut silently"
            )

    def _item_context(
        self, project_id: str, skill: Skill, upstream: Sequence[Upstream], own: str | None
    ) -> tuple[ItemContext | None, list[Item]]:
        """What the item checks need: upstream items, this artefact's earlier IDs, and the pools to cover."""
        if not skill.items:
            return None, []
        shown: list[Item] = []
        pairs = []
        for u in upstream:
            upstream_skill = self.skills.get(u.key, self.store.projects.get(project_id).kind)          # S40: a design written as scripts and files holds SCR and FIL items
            if upstream_skill is not None and upstream_skill.items:
                shown.extend(parse_items(u.text, upstream_skill.items).items)
                pairs.append((u.text, upstream_skill.items))
        earlier = [v.body for v in self.store.artefacts.versions(project_id, own)] if own else []
        history = history_of(earlier, skill.items)
        ctx = ItemContext(
            specs=skill.items, covers=skill.covers, known=frozenset(i.id for i in shown),
            pools=upstream_pool(pairs), current=history.current, retired=history.retired,
            highest=history.highest, previous=history.previous,
        )
        return ctx, shown

    def _own_artefact_id(self, project_id: str, skill: Skill) -> str | None:
        found = self.store.artefacts.list(project_id, phase_key=skill.artefact.phase_key, artefact_type=skill.key)
        return found[0].id if found else None

    def _upstream(self, project_id: str, skill: Skill) -> list[Upstream]:
        out: list[Upstream] = []
        for key in skill.upstream:
            phase_key = next(p.key for p in self.catalogue.phases for a in p.artefacts if a.key == key)
            found = self.store.artefacts.list(project_id, phase_key=phase_key, artefact_type=key)
            if (not found or found[0].latest_version is None) and key in skill.optional_upstream:
                continue                                              # S40: the skill can run without it (a project with no screens has no user journey); the prompt says so
            if not found or found[0].latest_version is None:
                name = next(a.name for p in self.catalogue.phases for a in p.artefacts if a.key == key)
                raise MissingInput(
                    f"'{skill.artefact.name}' builds on '{name}', which has not been written yet"
                )
            version = self.store.artefacts.get_version(project_id, found[0].id)
            out.append(Upstream(found[0].id, found[0].title, key, strip_replaces(version.body), version.number))       # the 'Replaces:' history is for people, never evidence for a later phase
        return out

    def skipped_inputs(self, project_id: str, skill: Skill) -> list[str]:
        """The names of the optional inputs this project has no text for (S40), in the skill's order. Empty for a skill with none."""
        out: list[str] = []
        for key in skill.optional_upstream:
            phase_key = next(p.key for p in self.catalogue.phases for a in p.artefacts if a.key == key)
            found = self.store.artefacts.list(project_id, phase_key=phase_key, artefact_type=key)
            if not found or found[0].latest_version is None:
                out.append(next(a.name for p in self.catalogue.phases for a in p.artefacts if a.key == key))
        return out

    def inputs_for(self, project_id: str, skill: Skill) -> tuple[list[Upstream], ItemContext | None]:
        """The upstream artefacts a skill reads (in full, as the writer sees them) and the item rules for it.
        Used by the reviewer and by a person's edit, so both see exactly what the writer saw."""
        upstream = self._upstream(project_id, skill)
        item_ctx, _ = self._item_context(project_id, skill, upstream, self._own_artefact_id(project_id, skill))
        if item_ctx is not None:
            # The writer may derive from a whole upstream artefact as well as from its items (see _assemble).
            item_ctx = replace(item_ctx, known=item_ctx.known | frozenset(u.artefact_id for u in upstream))
        return upstream, item_ctx

    def _whole_documents(self, project_id: str) -> list | None:
        """Every passage of the project's reference documents, when they all fit in `agent_context_whole_chars`; otherwise None and the writer is shown what the search ranks first.
        Found live (2026-11-13): a rules document of 11 passages was found by the search, then cut to the 6 best-matching passages, and the loan rules were not among them, so the
        charter asked questions the document answers. A small document costs a few cents to show whole; a missed rule costs an answer from the person."""
        limit = self.settings.agent_context_whole_chars
        if limit <= 0:
            return None
        found = self.store.context.document_hits(project_id)
        if not found or sum(len(h.text) for h in found) > limit:
            return None
        return found

    def _responses(self, project_id: str, skill: Skill) -> tuple[list[tuple[str, str]], str | None]:
        """The person's responses to this stage's open questions (S28): (passage id, text) pairs, cited by the passage they were stored under, and a note when free-form text was cut.
        Nothing when none were uploaded for the stage, so the prompt is then exactly what it was."""
        from ..questions import service as answers

        order = [a.key for a in self.catalogue.phase(skill.artefact.phase_key).artefacts]                 # the plan's order, so the question numbers match the file the person downloaded
        return answers.responses_for_prompt(self.store, project_id, skill.artefact.phase_key, order)

    MAX_SETTLED = 40

    def _settled(self, project_id: str, skill: Skill) -> list[str]:
        """The open questions of this stage a person marked resolved (2026-11-19), as they read now, so a rewrite does not raise them again. Bounded; empty when there are none."""
        from ..questions import service as answers

        phase_key = skill.artefact.phase_key
        order = [a.key for a in self.catalogue.phase(phase_key).artefacts]
        st = answers.state(self.store, project_id, phase_key, order)
        out = [i.text[:300] for kind in ("questions", "constraints") for i in st.resolved_items(kind)]
        return out[: self.MAX_SETTLED]

    def _memory_feedback(self, project_id: str, skill: Skill, own: str | None) -> list[Feedback]:
        return [Feedback(e.text, f"MEM-{e.id:03d}") for e in unacted_notes(self.store, project_id, skill.artefact.phase_key, own)]

    @property
    def enforces_limits(self) -> bool:
        """The skill's stated numbers and formats (S35) apply to every reply, live or played back.

        S35 first switched them off in replay, on the reasoning that a recording cannot be repaired. That was wrong for a recording made WITH the limits on: it holds the
        repair conversation too (the rejected first reply, the request that sent the problems back, the corrected reply), and replay reproduces it only if it applies the
        same checks. With them off the app saved the rejected first reply, which the recorded reviews never saw (found by the S36 demo check, 2026-11-12)."""
        return True

    # ---- checking a reply -----------------------------------------------------------------------------------

    @staticmethod
    def _check(result: LLMResult, skill: Skill, prompt: Prompt, item_ctx: ItemContext | None = None, scanner: Scanner | None = None, limits: bool = True, previous_body: str | None = None) -> tuple[list[str], str]:
        try:
            body = parse_reply(result.text)
        except ReplyFormatError as exc:
            if result.finish_reason in TRUNCATED_REASONS:
                # The reply was cut off by the output limit BEFORE it could close its tag. "Never closed" is true but gives the model nothing to act
                # on; the cause is the length limit, and the fix is a shorter artefact. (Seen with Gemini: a reply that wrote 300 visible tokens was
                # cut off, because its hidden reasoning had used the rest of the limit.)
                return [CUT_OFF_BEFORE_CLOSING], ""
            return [str(exc)], ""
        return check_body(body, skill, prompt.allowed, item_ctx, result.finish_reason, scanner, limits=limits, previous_body=previous_body, waive_rules=REWRITE_WAIVED), body

    # ---- saving ---------------------------------------------------------------------------------------------------

    def _save(
        self, project_id: str, skill: Skill, body: str, calls: Sequence[LLMResult], prompt: Prompt, run_id: str | None,
        item_ctx: ItemContext | None = None, repair_log: Sequence[dict] = (), extra_generation: Mapping | None = None,
    ) -> AgentResult:
        final = calls[-1]
        author = f"agent:{skill.artefact.role}@{final.served_model}"
        citations = cited_ids(body)
        ceiling = self.settings.output_tokens_for(skill.artefact.role)
        cost = sum_costs([c.cost_usd for c in calls])
        parsed = parse_items(body, skill.items)
        links = [(item.id, target, "derives") for item in parsed.items for target in item.link_targets]
        flags = [f.as_dict() for f in self.scanner.scan(body)]
        generation = {
            "guardrails": flags,
            "built_on": {artefact_id: number for artefact_id, number in prompt.built_on},
            "items": list(parsed.ids),
            "skill": skill.key,
            "run_id": run_id,
            "attempts": len(calls),
            "repairs": len(calls) - 1,
            "repair_reasons": list(repair_log),
            "tokens_in": sum(c.tokens_in for c in calls),
            "tokens_out": sum(c.tokens_out for c in calls),
            "cost_usd": cost,
            "calls": [
                {"call_id": c.call_id, "model": c.served_model, "slot": c.slot, "fallback_used": c.fallback_used,
                 "tokens_in": c.tokens_in, "tokens_out": c.tokens_out, "cost_usd": c.cost_usd}
                for c in calls
            ],
            "output_limit": ceiling,
            "near_limit": max((p for p in (near_limit(c.tokens_out, ceiling) for c in calls) if p is not None), default=None),
            **(extra_generation or {}),
        }

        def still_running(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT status FROM phase_runs WHERE project_id = ? AND phase_key = ?",
                (project_id, skill.artefact.phase_key),
            ).fetchone()
            status = row["status"] if row is not None else "not_started"
            if status != "running":
                raise PhaseNotRunning(
                    f"'{skill.artefact.name}' was not saved: its phase became '{status}' while the model was writing"
                )

        artefact, version = self.store.artefacts.create_and_add_version(
            project_id, artefact_type=skill.key, phase_key=skill.artefact.phase_key, title=skill.artefact.name,
            prefix=skill.artefact.id_prefix, body=body, author=author, source_chunks=citations,
            generation=generation, guard=still_running, links=links,
        )
        return AgentResult(
            artefact_id=artefact.id, version=version.number, author=author, attempts=len(calls),
            tokens_in=generation["tokens_in"], tokens_out=generation["tokens_out"], cost_usd=cost,
            citations=tuple(citations), call_ids=tuple(c.call_id for c in calls),
            served_models=tuple(dict.fromkeys(c.served_model for c in calls)),
            created_artefact=version.number == 1, items=parsed.ids, links=len(links), flags=len(flags),
        )
