from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import InvalidInput

SOURCE_KINDS = ("document", "artefact", "memory")
DOCUMENT_KINDS = ("txt", "md", "docx", "pdf")


class DocumentRefused(InvalidInput):
    """A document was refused for a reason the user can act on. The message never includes its content."""


@dataclass(frozen=True)
class Limits:
    """What the context store will accept and how it cuts text up. Defaults need no configuration."""

    max_file_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 200
    max_docs_per_project: int = 500
    max_chunks_per_document: int = 2000
    chunk_target_chars: int = 900
    chunk_cap_chars: int = 1500  # no chunk is ever longer than this
    min_tail_chars: int = 120  # a shorter last chunk is merged into the one before it
    max_uncompressed_factor: int = 6  # a .docx may expand to this many times the file limit


@dataclass(frozen=True)
class DocumentInfo:
    number: int
    filename: str
    kind: str
    sha256: str
    size_bytes: int
    chars: int
    chunk_count: int
    added_by: str
    created_at: str
    removed_at: str | None = None
    category: str = "reference"  # S28: 'reference', 'responses' (answers to a stage's open questions) or 'confirmations' (the charter's constraints, confirmed)
    stage: str | None = None  # S28: the phase a responses / confirmations document is for; None for a reference document
    already_added: bool = False  # True when the same bytes were already in the project
    findings: tuple = ()  # guardrail findings (rule, class, line, heading, length), flagged and never blocked; never the values

    @property
    def source_prefix(self) -> str:
        return f"DOC-{self.number:03d}"


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk. `citation` is the text an agent quotes; `source_id` is how it is looked up."""

    source_id: str
    kind: str
    title: str
    heading_path: str
    text: str
    score: float | None  # raw BM25, flipped so higher is better; None when looked up by ID
    relevance: float | None  # 0..1 against the best hit of THIS search; the best hit is always 1.0
    coverage: float | None  # 0..1: the share of the question's words this chunk contains (absolute, unlike relevance)
    phase_key: str | None
    artefact_id: str | None
    page: int | None
    citation: str


@dataclass(frozen=True)
class ReingestResult:
    indexed: int  # artefacts (re)indexed
    skipped: int  # artefacts already indexed at their current version
    chunks: int
    failed: tuple[tuple[str, str], ...] = ()  # (artefact id, plain reason)


# ---- S28: what a document is for -------------------------------------------------------------------------------------------------------------------

CATEGORIES = ("reference", "responses", "confirmations")
CONFIRMATION_STAGES = ("setup",)          # only the project charter states constraints and assumptions
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def check_category(category: str | None, stage: str | None) -> tuple[str, str | None]:
    """The category and stage to store, or a DocumentRefused with a plain sentence. Nothing given is a reference document, exactly as an upload always was.

    A reference document has no stage (one given is refused, not ignored: it would mean the person chose something that then did nothing). A responses document needs the
    stage whose questions it answers; a confirmations document is for Setup only. Whether the stage exists in this project's plan is checked by the route, which knows the plan."""
    category = (category or "reference").strip().lower()
    stage = (stage or "").strip().lower() or None
    if category not in CATEGORIES:
        raise DocumentRefused(f"a document is one of: {', '.join(CATEGORIES)}; '{category}' is not")
    if category == "reference":
        if stage is not None:
            raise DocumentRefused("a reference document is not tied to a stage; choose 'responses' to answer a stage's questions")
        return category, None
    if stage is None:
        raise DocumentRefused(f"say which stage these {category} are for")
    if not _STAGE.fullmatch(stage):
        raise DocumentRefused("the stage has unexpected characters")
    if category == "confirmations" and stage not in CONFIRMATION_STAGES:
        raise DocumentRefused("only the project charter, in Setup, states constraints and assumptions, so only Setup has confirmations")
    return category, stage

