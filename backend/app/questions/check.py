"""The one check an upload of ours gets (S28): a file this application wrote, uploaded for the wrong stage or project, is refused with a sentence.

Only OUR file is checked, recognised by its first line. Any other file (a client's own reply in Word, say) is accepted as it is: the person chose the stage, and what the file says is
theirs. Reference documents are never checked. Reading is by text, so a PDF or Word file is read the way the document store reads it.
"""
from __future__ import annotations

from typing import Any

from ..context.extract import extract
from . import collect as C


def wrong_stage_problem(request: Any, project_id: str, data: bytes, filename: str, category: str, stage: str | None) -> str | None:
    """A sentence if this is one of our files and it names a different stage or project than the one chosen; otherwise None. Never raises: a file that cannot be read is simply not ours."""
    if category == "reference" or stage is None:
        return None
    try:
        extracted = extract(filename, data)
        text = "\n".join(section.text for section in extracted.sections)
    except Exception:                                              # noqa: BLE001 - an unreadable file is refused later, by the upload itself, with its own sentence
        return None
    parsed = C.parse_answers(text)
    if parsed is None:
        return None
    if parsed.project_short != project_id:
        return "this file was downloaded from a different project; download the file again from this one and answer that"
    if parsed.phase_key != stage:
        names = request.app.state.engine.catalogue
        chosen = names.phase(stage).name if names.has_phase(stage) else stage
        theirs = names.phase(parsed.phase_key).name if parsed.phase_key and names.has_phase(parsed.phase_key) else "another stage"
        return f"this file is for {theirs}, but you chose {chosen}; choose {theirs}, or download the file for {chosen}"
    if (parsed.kind == C.KIND_CONSTRAINTS) != (category == "confirmations"):
        return "this is a file of " + ("constraints and assumptions" if parsed.kind == C.KIND_CONSTRAINTS else "open questions") + ", so choose " + (
            "'Confirmations of constraints and assumptions'" if parsed.kind == C.KIND_CONSTRAINTS else "'Responses to open questions'")
    return None
