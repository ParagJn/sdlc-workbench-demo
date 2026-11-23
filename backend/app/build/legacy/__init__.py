"""The catalogue and skills as they were while the Build phase WROTE CODE (S17 and S18), frozen.

The live Build phase now writes instruction files (S19). The code generator is switched off but kept, because it is the tool that will check code that comes
back (D19). Its tests and demo scripts load these copies, so they exercise the real generator without the live catalogue having to carry four code artefacts.
Nothing here is read by the running application."""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFINITIONS = HERE / "definitions"
SKILLS = HERE / "skills"


def catalogue():
    from ...llm.routing import Routing
    from ...workflow.definitions import load_catalogue
    return load_catalogue(Routing.load().roles.keys(), DEFINITIONS)


def library(cat=None):
    from ...agents.skills import SkillLibrary
    return SkillLibrary.load(cat if cat is not None else catalogue(), SKILLS)
