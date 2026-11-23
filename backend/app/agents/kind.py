"""S40 part 2: the kind of project, said to every writer.

A `software` project is a product with users, so the writers' skills are written for that and nothing is added. An `automation` project is a technical
migration or scripted automation: scripts, playbooks and files that people run, with approvals made by people. Left alone, a writer turns "the system shall record ..."
into services, a database and endpoints that no source asks for (measured 2026-11-13: 12 components, 12 tables, 31 endpoints for a runtime upgrade).
So for `automation` ONE section is added to the writer's rules; for `software` nothing is, and the prompt is byte for byte what it was.
"""
from __future__ import annotations

from ..models import KINDS

AUTOMATION_NOTE = (
    "## Kind of project\n"
    "This project is a technical migration or automation. What it delivers is scripts, playbooks, configuration and files that people run, "
    "and records kept in the organisation's own repository or change process. It is NOT a software product.\n"
    "- Write about the automation: what each script or playbook does, what it reads, what it writes, and how it stops safely. "
    "Write about people only as the roles who approve, decide or sign off; an approval is a gate the automation checks or a record in the change process, not a feature.\n"
    "- There is no user interface, no web service, no endpoint, no database, no queue and no application server in this project unless a source names one. "
    "Do not introduce any of them, and do not propose a store, a service, a screen or a product that no source names. "
    "If something would be needed and no source says what, put it in Open questions and do not decide it.\n"
    "- Take every rule, number, date, role and tolerance from the sources, and name the source."
)


def kind_note(kind: str) -> str:
    """The section added for this kind of project, or an empty string (a software project gets nothing)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return AUTOMATION_NOTE if kind == "automation" else ""
