"""Build the usage report for one project from its trace rows. A pure function: no database, no clock, no model.

A row is a plain dict with: served_model, phase_key, agent, status ('ok' or 'error'), fallback_used, tokens_in, tokens_out, latency_ms, cost_usd (None when unpriced), created_at.

Practice runs are the calls served by the built-in practice models, whose names start with PRACTICE_PREFIX. Their cost is mock money, so they are reported on their own and never added to the live figures.
A report never carries a prompt, an answer or the text of an error: errors are counted only.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import narrative

PRACTICE_PREFIX = "demo-"


def is_practice(served_model: str) -> bool:
    return (served_model or "").lower().startswith(PRACTICE_PREFIX)


def _totals(rows: list[dict]) -> dict[str, Any]:
    ok = [r for r in rows if r["status"] == "ok"]
    priced = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return {
        "calls": len(rows),
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "fallbacks": sum(1 for r in rows if r["fallback_used"]),
        "tokens_in": sum(r["tokens_in"] for r in rows),
        "tokens_out": sum(r["tokens_out"] for r in rows),
        "cost_usd": round(sum(priced), 6) if priced else (0.0 if not rows else None),
        "unpriced_calls": sum(1 for r in ok if r["cost_usd"] is None),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / len(rows)) if rows else 0,
        "first": min((r["created_at"] for r in rows), default=""),
        "last": max((r["created_at"] for r in rows), default=""),
    }


def _group(rows: list[dict], field: str, label) -> list[dict]:
    """Totals per value of `field`, biggest spend first (then most calls, then name, so the order never wobbles)."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[field] or "", []).append(r)
    out = []
    for key, items in groups.items():
        t = _totals(items)
        out.append({"key": key, "label": label(key), **{k: t[k] for k in ("calls", "errors", "fallbacks", "tokens_in", "tokens_out", "cost_usd", "unpriced_calls")}})
    out.sort(key=lambda g: (-(g["cost_usd"] or 0), -g["calls"], g["key"]))
    top = max((g["cost_usd"] or 0 for g in out), default=0)
    for g in out:
        g["share_of_top"] = round((g["cost_usd"] or 0) / top, 4) if top else 0.0     # the bar length: 1.0 for the biggest
    return out


def build_report(project: dict[str, Any], rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    live_rows = [r for r in rows if not is_practice(r["served_model"])]
    practice_rows = [r for r in rows if is_practice(r["served_model"])]
    report: dict[str, Any] = {
        "project": {"id": project["id"], "name": project["name"], "short_id": project["id"][:6], "started": project["created_at"]},
        "live": _totals(live_rows),
        "practice": _totals(practice_rows),
        "by_phase": _group(live_rows, "phase_key", narrative.phase_name),
        "by_model": _group(live_rows, "served_model", narrative.model_name),
        "by_agent": _group(live_rows, "agent", lambda k: k.replace("_", " ").title() if k else "Unknown"),
        "practice_by_model": _group(practice_rows, "served_model", lambda k: k),
        "practice_note": f"Practice calls are the ones served by the built-in practice models (names starting '{PRACTICE_PREFIX}'). They use no real model.",
        "footnote": "This report is written from the recorded model calls by fixed wording. No AI model is used to write it, so producing it costs nothing.",
    }
    report["narrative"] = narrative.sentences(report)
    return report
