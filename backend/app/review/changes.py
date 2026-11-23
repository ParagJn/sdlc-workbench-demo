"""What changed since the last version, for the reviewer (S33). Pure: no database, no model.

The reviewer is shown the artefact and what it was built on, but not its earlier versions. A rewrite (version 2 or later; 90 of the 122 stored reviews are of one) then invites a wrong finding such as
"the previous version cannot be checked" about a sentence that is true. This gives the reviewer the facts it needs in a few lines, not a second copy of the document: which items were added, kept, changed
and withdrawn, and for each changed item its previous text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..agents.items import parse_items
from ..agents.skills import ItemSpec

MAX_PREVIOUS_CHARS = 300        # an item's previous text, cut to this
MAX_LISTED = 40                 # IDs named per group; the rest are counted, never silently dropped
MAX_CHANGED_SHOWN = 12          # changed items shown with their previous text; the rest are counted


@dataclass(frozen=True)
class Changes:
    version: int                        # the version being reviewed
    previous_version: int
    added: tuple[str, ...]
    kept: tuple[str, ...]               # in both versions with the same text
    changed: tuple[tuple[str, str], ...]    # (item ID, its text in the previous version), in the new version's order
    withdrawn: tuple[str, ...]          # in the previous version, not in this one

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.withdrawn)


def changes_between(previous_body: str, body: str, specs: Sequence[ItemSpec], *, version: int, previous_version: int) -> Changes | None:
    """The changes from `previous_body` to `body`, or None when the artefact holds no numbered items (there is then nothing to list by ID)."""
    if not specs:
        return None
    before = {i.id: i.text for i in parse_items(previous_body, specs).items}
    now = parse_items(body, specs).items
    added, kept, changed = [], [], []
    for item in now:
        if item.id not in before:
            added.append(item.id)
        elif " ".join(before[item.id].split()) == " ".join(item.text.split()):
            kept.append(item.id)
        else:
            changed.append((item.id, before[item.id]))
    present = {i.id for i in now}
    withdrawn = [i for i in before if i not in present]
    return Changes(version, previous_version, tuple(added), tuple(kept), tuple(changed), tuple(withdrawn))


def _ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_LISTED])
    return shown + (f" and {len(ids) - MAX_LISTED} more" if len(ids) > MAX_LISTED else "")


def describe(changes: Changes, neutralise) -> str:
    """The block of text for the reviewer's prompt. `neutralise` is the prompt module's own cleaner for foreign text, so a previous version cannot forge a fence or a tag."""
    c = changes
    lines = [f"This is version {c.version}. The version before it was {c.previous_version}. Use this to check any statement the artefact makes about what stayed the same or changed."]
    lines.append(f"- Items added: {_ids(c.added) if c.added else 'none'}.")
    lines.append(f"- Items kept with the same text: {_ids(c.kept) if c.kept else 'none'}.")
    lines.append(f"- Items whose text changed: {_ids([i for i, _ in c.changed]) if c.changed else 'none'}.")
    lines.append(f"- Items withdrawn: {_ids(c.withdrawn) if c.withdrawn else 'none'}.")
    shown = c.changed[:MAX_CHANGED_SHOWN]
    if shown:
        lines.append("")
        lines.append("What the changed items said in the previous version:")
        for item_id, old in shown:
            text = neutralise(" ".join(old.split()))[:MAX_PREVIOUS_CHARS]
            lines.append(f"- {item_id}: {text}")
        if len(c.changed) > len(shown):
            lines.append(f"- ({len(c.changed) - len(shown)} more changed items are not shown.)")
    return "\n".join(lines)
