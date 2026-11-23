"""The pictures a design document's Word file carries (2026-11-22, the user's request: the high-level design's CONCEPTUAL ER and the low-level design's LOGICAL ER go in the Word file).

One function, used by every route that makes a Word file, so the zip and a single download can never disagree. The pictures are drawn from the document's own text (nothing is stored), in the
draw.io look; a document that has no readable data section, or whose picture cannot be drawn, simply gets none. Nothing here raises.
"""
from __future__ import annotations

from ..deploy.docx_writer import Picture
from .data import CONCEPTUAL, LOGICAL
from .diagram import render_diagram
from .png import svg_to_png
from .readable import DOCUMENT_SECTIONS, reshape

TITLE = {CONCEPTUAL: "Conceptual ER diagram", LOGICAL: "Logical ER diagram"}
AFTER = {CONCEPTUAL: "Data overview", LOGICAL: "Database design"}            # the picture sits right after the section it draws, not on the last page
NOTE = {
    CONCEPTUAL: "The main things the system keeps and how they relate. Drawn from the Data overview of this document. If it is too large for the page, resize it in Word.",
    LOGICAL: "Every table with its columns and key, and how the tables relate. Drawn from the Tables and Database design sections of this document. If it is too large for the page, resize it in Word.",
}


def body_for_word(artefact_type: str, body: str) -> str:   # also the `readable` text the screen shows
    """The design documents' tables, endpoints, rules, decisions, interfaces and things kept written as blocks a person can read. Every other document, and every entry
    that is not in a readable shape, is returned exactly as it was. The stored text is never changed: this is only how the Word file is laid out."""
    if artefact_type not in DOCUMENT_SECTIONS:
        return body
    try:
        return reshape(body, DOCUMENT_SECTIONS[artefact_type])
    except Exception:
        return body


def pictures_for(artefact_type: str, body: str) -> list[Picture]:
    """The ER picture for a high- or low-level design, as a list of zero or one."""
    if artefact_type not in TITLE:
        return []
    try:
        svg = render_diagram(artefact_type, body, {})
        png = svg_to_png(svg) if svg else None
    except Exception:
        return []
    return [Picture(TITLE[artefact_type], png, NOTE[artefact_type], AFTER[artefact_type])] if png else []
