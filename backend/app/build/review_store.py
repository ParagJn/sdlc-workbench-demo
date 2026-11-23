"""Saving the two code reviews as one artefact, and reading them back (S18b).

`code_security_review` is ONE artefact. Each time reviewers run it gets a NEW version whose text is a plain-words summary (so it reads like any artefact)
and whose `generation` holds the two reviewer records keyed by reviewer, plus WHICH versions of the four code artefacts were read. Because a version is
immutable and a re-run adds a new one, one reviewer's result can never overwrite the other's, and the history stays together.

A re-run of ONE reviewer carries the other's latest record forward unchanged into the new version, so the newest version always holds the current state of both.
Whether a review is out of date is a plain comparison of the versions it read with the versions the code artefacts have now.
"""
from __future__ import annotations

from typing import Mapping

from ..agents.skills import SkillLibrary
from .code_review import AGENT_NAMES, CODE_ARTEFACTS, GREEN, GREY, RED, SLOTS, CodeReview, agent_name

REVIEW_KEY = "code_security_review"
PHASE = "build"
REVIEW_AUTHOR = "agent:security_reviewer@system"        # the text is assembled by the system from what the two reviewers said


def versions_read(store, project_id: str) -> dict[str, int]:
    """{code artefact key: its latest saved version number} for the code that exists now."""
    out: dict[str, int] = {}
    for key in CODE_ARTEFACTS:
        found = store.artefacts.list(project_id, phase_key=PHASE, artefact_type=key)
        if found and found[0].latest_version is not None:
            out[key] = found[0].latest_version
    return out


def latest_record(store, project_id: str) -> dict | None:
    """The reviewers' records from the newest version that holds them, or None if no review has ever been saved. A person may edit the review text; that
    new version carries no records, and the reviewers' last results must not vanish because of it."""
    found = store.artefacts.list(project_id, phase_key=PHASE, artefact_type=REVIEW_KEY)
    if not found or found[0].latest_version is None:
        return None
    for version in reversed(store.artefacts.versions(project_id, found[0].id)):
        generation = version.generation if isinstance(version.generation, dict) else None
        if generation and "reviews" in generation:
            return {**generation, "version": version.number, "artefact_id": found[0].id}
    return None


def body_for(reviews: Mapping[str, dict]) -> str:
    """The artefact's own text: what each reviewer said, in plain words. The findings are listed; the code itself is not copied in."""
    out = ["## Result", "Two reviewers read the generated code. This is PROTOTYPE code: a clean result means they found no problems in it, not that it is ready for production.", ""]
    for slot in SLOTS:
        rec = reviews.get(slot)
        out += [f"## Reviewed by the {agent_name(slot)} agent", (rec or {}).get("message") or f"Code not reviewed by the {agent_name(slot)} agent yet."]
        if rec and rec.get("summary"):
            out.append(f"Summary: {rec['summary']}")
        for f in (rec or {}).get("findings", []):
            out.append(f"- [{f['severity'].upper()}] {f['target']}: {f['problem']}" + (f" Fix: {f['fix']}" if f.get("fix") else ""))
        if rec and rec.get("label"):
            out.append(f"Independence: {rec['label'].replace('_', ' ')}.")
        if rec and rec.get("partial"):
            out.append("This review is partial. Not shown to it: " + ", ".join(rec.get("left_out", [])) + ".")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def save(store, library: SkillLibrary, project_id: str, results: Mapping[str, CodeReview], *, run_id: str | None = None) -> int:
    """Save the reviewers' results as a new version of the review artefact. Any reviewer not in `results` keeps its latest record. Returns the version number."""
    previous = (latest_record(store, project_id) or {}).get("reviews", {})
    reviews: dict[str, dict] = {slot: rec for slot, rec in previous.items() if slot in SLOTS}
    for slot, result in results.items():
        reviews[slot] = result.as_record()
    skill = library.get(REVIEW_KEY)
    generation = {
        "skill": REVIEW_KEY, "written_by": "system", "run_id": run_id, "reviews": reviews, "read_versions": versions_read(store, project_id),
        "ran": sorted(results),
    }
    _, version = store.artefacts.create_and_add_version(
        project_id, artefact_type=REVIEW_KEY, phase_key=PHASE, title=skill.artefact.name if skill else "Code and security review", prefix="BLD-REV",
        body=body_for(reviews), author=REVIEW_AUTHOR, generation=generation,
    )
    return version.number


def is_current(store, project_id: str, record: Mapping | None) -> bool:
    """True if the code has not changed since the review: every code artefact's version is the one that was read."""
    if not record:
        return False
    return dict(record.get("read_versions", {})) == versions_read(store, project_id)
