"""Download every artefact of a project in one zip (the user's request, 2026-11-17; for an Administrator only).

The zip holds one folder for each phase that has something written, in the order of the phases ('1 Setup', '2 Requirements', '3 Design', '4 Architecture', '5 Build'). The documents of
Setup, Requirements, Design and Architecture are Word files made from the saved text by the writer Deploy already uses; the files of the Build brief are Markdown, exactly as saved.
Only the latest version of each artefact is written. A wireframe's drawing (an `svg` block) is not a thing Word can show, so it is replaced by one line saying where the drawing is.

Pure functions: no model, no network, no database and no disk; the zip's bytes are returned. Deploy and Handover are listed in `NOT_YET` and are left out until the user adds them.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from .architecture.word import body_for_word, pictures_for
from .build import brief as B
from .deploy.docx_writer import DocInfo, markdown_to_docx

PHASES = ("setup", "requirements", "design", "architecture", "build")
NOT_YET = ("deploy", "handover")                       # later, by the user's decision: their artefacts are not in the zip yet
PHASE_NAMES = {"setup": "Setup", "requirements": "Requirements", "design": "Design", "architecture": "Architecture", "build": "Build"}
WORD, MARKDOWN = "docx", "md"

# the order of the documents inside a phase: the order a person would read them in, then by name
READING_ORDER = {
    "setup": ("project_charter",),
    "requirements": ("problem_statement", "vision_objectives", "personas", "functional_requirements", "non_functional_requirements", "epics_features", "user_stories"),
    "design": ("user_journey", "lowfi_wireframes", "hifi_wireframes"),
    "architecture": ("usecase_diagram", "component_overview", "high_level_design", "low_level_design", "flow_diagram"),
    "build": ("build_brief",),
}
_OPEN_FENCE = re.compile(r"^(\s{0,3})(`{3,}|~{3,})[ \t]*([A-Za-z0-9_+-]*)[ \t]*$")
_BAD_NAME = re.compile(r"[^A-Za-z0-9 ._()\-]+")
MAX_NAME = 80


@dataclass(frozen=True)
class Item:
    """One saved artefact, as the export reads it: where it belongs, what it is called and its latest text."""
    phase: str
    type: str
    id: str
    title: str
    version: int
    body: str
    created_at: str | None = None


@dataclass(frozen=True)
class Entry:
    path: str                  # the path inside the zip
    data: bytes
    kind: str                  # docx | md
    artefact_id: str
    version: int


def safe_name(text: str, fallback: str = "file") -> str:
    """A file or folder name that is safe on every system: letters, digits, spaces and . _ ( ) -, no path parts, bounded."""
    cleaned = _BAD_NAME.sub(" ", (text or "").replace("/", " ").replace("\\", " "))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    return (cleaned[:MAX_NAME].strip(" .")) or fallback


def folder_of(phase: str) -> str:
    return f"{PHASES.index(phase) + 1} {PHASE_NAMES[phase]}"


def drawings_replaced(markdown: str) -> str:
    """The text with every `svg` block replaced by one line, because Word cannot show a drawing and its markup is not readable. The line names the drawing's item where one is near.
    Blocks of any other kind are kept as they are (they are code, and Word writes them in a code style)."""
    lines = (markdown or "").split("\n")
    out: list[str] = []
    last_item = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        item = re.match(r"^\s*(?:[-*]\s+)?([A-Z]{2,5}-\d{3})\b", line)
        if item:
            last_item = item.group(1)
        opening = _OPEN_FENCE.match(line)
        if opening and opening.group(3).lower() == "svg":
            fence = opening.group(2)
            j = i + 1
            while j < len(lines) and not re.match(rf"^\s{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", lines[j]):
                j += 1
            where = f" for {last_item}" if last_item else ""
            out.append(f"*[The drawing{where} is shown in the app; it is not part of this document.]*")
            i = j + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _when(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def ordered(items: Iterable[Item]) -> list[Item]:
    """The items of the phases that are exported, by phase and then in reading order; anything else (Deploy, Handover, an unknown type) is left out."""
    keep = [i for i in items if i.phase in PHASES]
    def key(i: Item) -> tuple[int, int, str]:
        order = READING_ORDER.get(i.phase, ())
        return (PHASES.index(i.phase), order.index(i.type) if i.type in order else len(order), i.title.lower())
    return sorted(keep, key=key)


def document(item: Item, project_name: str) -> Entry:
    """One artefact as a Word file, in its phase's folder."""
    data = markdown_to_docx(body_for_word(item.type, drawings_replaced(item.body)), DocInfo(title=f"{item.title} - {project_name}", subject=f"{item.id} version {item.version}", created=_when(item.created_at)), pictures_for(item.type, item.body))
    return Entry(f"{folder_of(item.phase)}/{safe_name(item.title)}.docx", data, WORD, item.id, item.version)


def brief_entries(item: Item) -> list[Entry]:
    """The files of the Build brief, each exactly as saved, in the Build folder."""
    files = B.split_set(item.body)
    return [Entry(f"{folder_of(item.phase)}/{safe_name(name, 'file.md')}", text.encode("utf-8"), MARKDOWN, item.id, item.version) for name, text in sorted(files.items(), key=lambda kv: B.order_key(kv[0]))]


def entries_for(items: Iterable[Item], project_name: str) -> list[Entry]:
    """Every file of the export, in order. A name used twice in one folder gets '(2)', so nothing is overwritten."""
    out: list[Entry] = []
    seen: set[str] = set()
    for item in ordered(items):
        made = brief_entries(item) if item.type == "build_brief" else [document(item, project_name)]
        for e in made:
            path, n = e.path, 2
            while path.lower() in seen:
                stem, dot, ext = e.path.rpartition(".")
                path = f"{stem} ({n}){dot}{ext}"
                n += 1
            seen.add(path.lower())
            out.append(Entry(path, e.data, e.kind, e.artefact_id, e.version))
    return out


def folders_of(entries: Sequence[Entry]) -> list[tuple[str, list[Entry]]]:
    """The entries grouped by folder, in order: what the screen shows as a directory."""
    groups: dict[str, list[Entry]] = {}
    for e in entries:
        groups.setdefault(e.path.split("/", 1)[0], []).append(e)
    return list(groups.items())


def zip_bytes(entries: Sequence[Entry]) -> bytes:
    """The zip. Every entry is stored with a fixed time so the same project gives the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for e in entries:
            info = zipfile.ZipInfo(e.path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, e.data)
    return buffer.getvalue()


def zip_name(project_name: str) -> str:
    return f"{safe_name(project_name, 'project')}-artefacts.zip"


def listing(entries: Sequence[Entry]) -> list[dict]:
    """What the screen shows: the folders and, in each, the files with their kind and size. No bytes."""
    return [{"folder": folder, "files": [{"path": e.path, "name": e.path.split("/", 1)[1], "kind": e.kind, "artefact_id": e.artefact_id, "version": e.version, "bytes": len(e.data)} for e in files]}
            for folder, files in folders_of(entries)]
