"""S40: a plan whose writers could not run. The wizard let a project be created with Build ticked and the use-case diagram not, and Build then failed with "builds on 'Use-case diagram',
which has not been written yet". The catalogue's `after` lists only the order WITHIN a phase; what a writer reads is in its skill. So the plan is checked against the skills:
every artefact in the plan whose skill reads another artefact as a REQUIRED input needs that artefact in the plan too, or the person is told, in plain words, before anything is created."""
from __future__ import annotations

from typing import Iterable

from ..workflow.plan import Plan
from .skills import SkillLibrary


def input_problems(plan: Plan, skills: SkillLibrary, kind: str) -> list[str]:
    """One plain sentence per artefact in the plan that needs an artefact the plan leaves out. [] when every writer in the plan can run."""
    names = {a.key: a.name for p in plan.phases for a in p.phase.artefacts}
    in_plan = {a.key for p in plan.phases for a in p.artefacts}
    out: list[str] = []
    for phase in plan.phases:
        for artefact in phase.artefacts:
            skill = skills.get(artefact.key, kind)
            if skill is None:
                continue
            missing = [up for up in skill.upstream if up not in skill.optional_upstream and up in names and up not in in_plan]
            if missing:
                listed = ", ".join(f"'{names[m]}'" for m in missing)
                out.append(f"'{artefact.name}' is ticked but it is written from {listed}, which {'is' if len(missing) == 1 else 'are'} not ticked. Tick {'it' if len(missing) == 1 else 'them'}, or untick '{artefact.name}'")
    return out


def check_inputs(plan: Plan, skills: SkillLibrary, kind: str) -> None:
    from ..workflow.plan import PlanError

    problems = input_problems(plan, skills, kind)
    if problems:
        raise PlanError("; ".join(problems))
