"""Put the pieces together for one project and phase (S27): read the latest artefact versions and the project's documents, and say what is open. Read-only."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import collect as C


MAX_FREE_CHARS = 40000                # the most free-form response text one rewrite is shown, per stage (S28); a note in the prompt and in the panel says when it was cut. A real reply from a client runs to many thousands of characters; 8,000 cut one off before its last third
CATEGORY_OF_KIND = {C.KIND_QUESTIONS: "responses", C.KIND_CONSTRAINTS: "confirmations"}


@dataclass(frozen=True)
class Passage:
    source_id: str                    # the passage's own citation id, e.g. DOC-002#3: a bare document id is not a valid citation
    text: str
    item_id: str | None = None        # the question it answers, when it is an answered block of our file


@dataclass(frozen=True)
class AnswersFile:
    number: int                       # the document's number in the project
    source_id: str                    # DOC-nnn
    filename: str
    kind: str                         # 'questions' or 'constraints'; for a free-form file, what its category says
    answered: dict[str, str]          # item id -> answer, for OUR file; empty for a free-form one
    listed: tuple[str, ...]
    asked: dict[str, str] = field(default_factory=dict)   # item id -> the question the file printed under it (our file only)
    free_form: bool = False           # True when it is not our file (a client's own reply): its text goes to the rewrite as written
    passages: tuple[Passage, ...] = ()


@dataclass(frozen=True)
class State:
    phase_key: str
    collected: C.Collected
    files: tuple[AnswersFile, ...]    # the responses / confirmations documents uploaded for THIS stage and project, oldest first
    resolved_questions: frozenset[str] = frozenset()      # `text_key`s a person marked resolved (2026-11-19); the NEWEST mark of each wins, so 'open again' is already left out
    resolved_constraints: frozenset[str] = frozenset()

    def answered_ids(self, kind: str) -> set[str]:
        return {i for f in self.files if f.kind == kind for i in f.answered}

    def open_questions(self) -> list[C.Item]:
        done = self.answered_ids(C.KIND_QUESTIONS)
        return [i for i in self.collected.questions if i.id not in done and C.text_key(i.text) not in self.resolved_questions]

    def open_constraints(self) -> list[C.Item]:
        done = self.answered_ids(C.KIND_CONSTRAINTS)
        return [i for i in self.collected.constraints if i.id not in done and C.text_key(i.text) not in self.resolved_constraints]

    def resolved_items(self, kind: str) -> list[C.Item]:
        """The questions (or constraints) of the stage a person marked resolved, still shown in a small list so the mark can be undone. Answered ones are not here: an answer already settles them."""
        done = self.answered_ids(kind)
        marked = self.resolved_questions if kind == C.KIND_QUESTIONS else self.resolved_constraints
        items = self.collected.questions if kind == C.KIND_QUESTIONS else self.collected.constraints
        return [i for i in items if i.id not in done and C.text_key(i.text) in marked]

    def numbering_warnings(self) -> list[str]:
        """Plain sentences about an uploaded file of ours whose question numbers no longer mean what the stage now says. The file was made from an earlier version of the documents, or
        another file for the same stage numbered the same IDs differently, so an answer filed under an ID could be applied to a different question. Nothing is blocked: the person decides."""
        current = {i.id: i.text for i in (*self.collected.questions, *self.collected.constraints)}
        out: list[str] = []
        for f in self.files:
            if f.free_form:
                continue
            bad = [(i, f.asked[i]) for i in f.listed if f.asked.get(i, "").strip() and _same(f.asked[i]) != _same(current.get(i, ""))]          # a number the stage no longer has compares against nothing, so it differs
            if not bad:
                continue
            shown = "; ".join(f"{i} there asks \u201c{_cut(t)}\u201d, but {i} is now " + (f"\u201c{_cut(current[i])}\u201d" if i in current else "not in the stage") for i, t in bad[:3])
            more = f", and {len(bad) - 3} more" if len(bad) > 3 else ""
            out.append(f"{f.source_id} ({f.filename}) numbers its questions differently from the stage now: {shown}{more}. An answer filed under such a number could be applied to the wrong question. "
                       "Download the file again and answer that one, or remove this one from the documents.")
        return out

    def has_answers_file(self) -> bool:
        return bool(self.files)

    def has_usable_responses(self) -> bool:
        """Is there anything for a rewrite to use: a filled-in answer, or a free-form reply. A file with every answer left blank is present, but gives a rewrite nothing."""
        return any(f.answered or (f.free_form and any(p.text.strip() for p in f.passages)) for f in self.files)


def _same(text: str) -> str:
    return " ".join(C.plain(text or "").split()).casefold()                  # an answers file made before bold markers were dropped still matches the stage


def _cut(text: str, n: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


def _resolved_keys(current: dict) -> frozenset[str]:
    """The keys a person marked resolved. A mark saved before bold markers were dropped from the text carries them in its key, so every stored key is read again through the same rule as a
    question's own words; where two stored keys now mean the same question, the NEWEST mark of the two wins (an 'open again' still undoes an older 'resolved')."""
    newest: dict[str, Any] = {}
    for mark in sorted(current.values(), key=lambda m: m.id):
        newest[C.text_key(mark.text_key)] = mark
    return frozenset(k for k, m in newest.items() if m.state == "resolved")


def latest_texts(store: Any, project_id: str, phase_key: str, order: Sequence[str] = ()) -> list[tuple[str, str, str]]:
    """(id, title, body of the LATEST version) for every written artefact of the phase. An older version is never read.

    The ORDER decides the question numbers (REQ-Q1, REQ-Q2...), so it must not depend on timing: a phase's documents are written in parallel, and which one finishes first
    would otherwise change the numbers between two identical runs, and an answers file from earlier would answer the wrong question. Documents are therefore ordered by their
    position in the phase's plan (`order` is the plan's artefact types), then by artefact ID."""
    position = {key: n for n, key in enumerate(order)}
    written = [a for a in store.artefacts.list(project_id, phase_key=phase_key) if a.latest_version is not None]
    written.sort(key=lambda a: (position.get(a.type, len(position)), a.id))
    return [(a.id, a.title, store.artefacts.get_version(project_id, a.id).body) for a in written]


def state(store: Any, project_id: str, phase_key: str, order: Sequence[str] = ()) -> State:
    """What a stage left open and what has been uploaded for it. The CATEGORY chosen at upload decides which documents count (S28); the first line of our own file is read only to
    take its answers. A reference document is never an answers file, whatever it says."""
    project = store.projects.get(project_id)
    collected = C.collect(phase_key, latest_texts(store, project_id, phase_key, order))
    files: list[AnswersFile] = []
    for category in ("responses", "confirmations"):
        for info, passages in store.context.document_passages(project_id, category=category, stage=phase_key):
            whole = "\n".join(text for _, text in passages)
            parsed = C.parse_answers(whole)
            ours = parsed is not None and parsed.project_short == project.id.lower() and parsed.phase_key == phase_key
            kind = parsed.kind if ours else (C.KIND_CONSTRAINTS if category == "confirmations" else C.KIND_QUESTIONS)
            if ours and CATEGORY_OF_KIND[parsed.kind] != category:
                continue                                                 # a questions file filed as confirmations (or the reverse): the upload refuses it; if one got in, it counts for neither
            cited: list[Passage] = []
            for source_id, text in passages:
                block = C.block_answer(text) if ours else None
                cited.append(Passage(source_id, text, block[0] if block else None))
            files.append(AnswersFile(
                info.number, info.source_prefix, info.filename, kind,
                dict(parsed.answered) if ours else {}, parsed.listed if ours else (), dict(parsed.asked) if ours else {}, free_form=not ours, passages=tuple(cited),
            ))
    marks = {kind: _resolved_keys(store.question_marks.current(project_id, phase_key, kind)) for kind in (C.KIND_QUESTIONS, C.KIND_CONSTRAINTS)}
    return State(phase_key, collected, tuple(files), marks[C.KIND_QUESTIONS], marks[C.KIND_CONSTRAINTS])


def responses_for_prompt(store: Any, project_id: str, phase_key: str, order: Sequence[str] = ()) -> tuple[list[tuple[str, str]], str | None]:
    """What a rewrite is shown (S28): (passage id, text) for each filled-in answer, in the person's words, and for a free-form reply its passages as written, bounded.
    The second value is a plain note when free-form text was cut, else None. Empty when nothing usable was uploaded for the stage."""
    s = state(store, project_id, phase_key, order)
    items = {i.id: i for i in (*s.collected.questions, *s.collected.constraints)}
    out: list[tuple[str, str]] = []
    used = 0
    cut = False
    for f in s.files:
        for passage in f.passages:
            if not f.free_form:
                if passage.item_id is None or passage.item_id not in f.answered:
                    continue                                              # the frame, an unanswered block: nothing to say
                asked = items.get(passage.item_id)
                question = f" (question: {asked.text[:300]})" if asked else ""
                out.append((passage.source_id, f"{passage.item_id}{question}. Answer: {f.answered[passage.item_id]}"))
            else:
                text = " ".join(passage.text.split())
                if not text:
                    continue
                room = MAX_FREE_CHARS - used
                if room <= 0:
                    cut = True
                    break
                if len(text) > room:
                    text, cut = text[:room], True
                used += len(text)
                out.append((passage.source_id, text))
    note = f"The free-form text was cut after {MAX_FREE_CHARS} characters; the rest was not shown." if cut else None
    return out, note


# ---- the one-button rewrite (S28) -----------------------------------------------------------------------------------------------------------------

REWRITE_RUNNING, REWRITE_AFTER_SEND_BACK, REWRITE_REFUSED = "run", "send_back_then_run", "refused"


def documents_to_rewrite(s: State) -> list[str]:
    """The artefact IDs that a rewrite with the responses should touch.

    For OUR file: exactly the documents that had an ANSWERED question (a question knows the document it came from). For a free-form reply nobody can say which document it bears
    on, so every document of the stage that has an Open questions section is rewritten. Documents with no open questions are left alone, so no money is spent on them."""
    answered = {i for f in s.files for i in f.answered}
    chosen = [i.artefact_id for i in (*s.collected.questions, *s.collected.constraints) if i.id in answered]
    if any(f.free_form and any(p.text.strip() for p in f.passages) for f in s.files):
        chosen += [i.artefact_id for i in s.collected.questions]
    seen: dict[str, None] = {}
    for artefact_id in chosen:
        seen.setdefault(artefact_id, None)
    return list(seen)


def rewrite_plan(phase_status: str, s: State) -> tuple[str, str | None, list[str]]:
    """(what to do, a sentence when it cannot be done, the artefact IDs to rewrite) for the phase in this state.

    - nothing usable uploaded: refused (there is nothing for a rewrite to use);
    - running or sent back: run the rewrite;
    - awaiting review: send it back with an automatic note, then run (an audited decision, so the approval rule applies);
    - approved: refused with the existing rule's sentence (reopen first); not started: refused (there is nothing to rewrite yet)."""
    targets = documents_to_rewrite(s)
    if not s.has_usable_responses():
        return REWRITE_REFUSED, "no usable responses have been uploaded for this stage: upload the answered file, choosing 'Responses', first", []
    if not targets:
        return REWRITE_REFUSED, "none of the documents of this stage has an open question that the responses bear on, so there is nothing to rewrite", []
    if phase_status == "not_started":
        return REWRITE_REFUSED, "this stage has not been written yet; run it first, then answer its questions", []
    if phase_status == "approved":
        return REWRITE_REFUSED, "this stage is approved; reopen it before rewriting it", []
    if phase_status == "awaiting_review":
        return REWRITE_AFTER_SEND_BACK, None, targets
    return REWRITE_RUNNING, None, targets                                  # running, or rejected (sent back)


def send_back_note(s: State) -> str:
    """The automatic note recorded when the button sends a phase back: which response documents it was for."""
    names = ", ".join(f"{f.source_id}" for f in s.files)
    return f"Responses to the open questions were uploaded ({names}); rewrite to use them."
