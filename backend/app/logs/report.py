"""The logs of a project, by stage (S32): what happened on each run and why anything failed, in words. Pure functions: no database, no clock, no model.

A row is a plain dict (the routes read them from the stores and pass them in). Nothing here carries a prompt or a model's answer: those are never kept. What it does carry has
already been cleaned when it was stored (one line, no URL, no configured secret, bounded), and a trace's error text is cleaned and cut again here, because it was stored by an
older version of the app and by a provider layer this module does not own.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

MAX_ERROR_CHARS = 300
NOT_KEPT = "The detail of this failure was not kept (the run was made before details were recorded, or it failed in a way that records none)."
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PART = re.compile(r"\(part (\d+) of (\d+)\)\s*")        # used with .match, which already anchors it at the start

# What the run's own status is called, in a sentence, and the words for an artefact's outcome.
RUN_WORDS = {"running": "running", "finished": "finished", "stopped": "stopped early", "failed": "failed", "interrupted": "interrupted (the app stopped while it ran)"}
OUTCOME_WORDS = {"done": "written", "failed": "failed", "skipped": "skipped", "not_started": "not started"}
STOP_WORDS = {"budget": "the cost cap was reached", "time": "the time cap was reached", "cancelled": "a person cancelled it"}


def clean_text(text: Any, limit: int = MAX_ERROR_CHARS, secrets: Iterable[str] = ()) -> str:
    """One line, no URL, no configured secret, cut to `limit`."""
    out = " ".join(str(text if text is not None else "").split())
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[removed]")
    return _URL.sub("[url removed]", out)[:limit]


MAX_REPLY_CHARS = 60_000


def _reply_text(text: Any, secrets: Iterable[str] = ()) -> str:
    """A kept reply as the log shows it: its own line breaks, every configured secret and web address taken out again (the keeper already did this once; a log is read by people, so it is done twice)."""
    out = str(text) if isinstance(text, str) else ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[removed]")
    return _URL.sub("[url removed]", out)[:MAX_REPLY_CHARS]


def part_of(problem: str) -> tuple[int, int] | None:
    """(part, parts) when a problem is marked '(part 2 of 4) ...', else None."""
    match = _PART.match(problem or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _seconds(started: str | None, finished: str | None) -> float | None:
    from datetime import datetime

    if not started or not finished:
        return None
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((b - a).total_seconds(), 1))


def _artefact_rows(run: Mapping, events: Sequence[Mapping], names: Mapping[str, str], secrets: Iterable[str]) -> list[dict]:
    """One row per artefact the run was asked to write, in the order the run listed them, from the events and the stored failure detail."""
    totals = run.get("totals") or {}
    failures = totals.get("failures") or {}
    order: list[str] = []
    state: dict[str, dict[str, Any]] = {}
    for event in events:
        data = event.get("data") or {}
        kind = event.get("type")
        if kind == "run.started":
            order = [k for k in data.get("artefacts", []) if isinstance(k, str)]
        key = data.get("key")
        if not isinstance(key, str):
            continue
        row = state.setdefault(key, {"outcome": "not_started", "attempts": 0, "version": None, "cost_usd": None, "reason": None})
        if kind == "artefact.started":
            row["attempts"] = max(row["attempts"], 1)
        elif kind == "artefact.attempt":
            row["attempts"] = max(row["attempts"], int(data.get("attempt", 0) or 0))
        elif kind == "artefact.finished":
            row.update(outcome="done", attempts=int(data.get("attempts", row["attempts"]) or 0), version=data.get("version"), cost_usd=data.get("cost_usd"))
        elif kind == "artefact.failed":
            row.update(outcome="failed", reason=clean_text(data.get("reason"), secrets=secrets))
        elif kind == "artefact.skipped":
            row.update(outcome="skipped", reason=clean_text(data.get("reason"), secrets=secrets))
    keys = order + [k for k in state if k not in order] + [k for k in failures if k not in order and k not in state]
    out: list[dict] = []
    for key in keys:
        row = state.get(key, {"outcome": "failed" if key in failures else "not_started", "attempts": 0, "version": None, "cost_usd": None, "reason": None})
        failure = failures.get(key)
        detail: dict[str, Any] | None = None
        if failure is not None:
            attempts = [
                {"attempt": a.get("attempt"), "call_id": a.get("call_id"), "problems": [clean_text(p, 400, secrets) for p in a.get("problems", [])]}
                for a in (failure.get("attempts") or []) if isinstance(a, Mapping)
            ]
            detail = {
                "reason": clean_text(failure.get("reason"), 2000, secrets), "cut_off": bool(failure.get("cut_off")),
                "problems": [clean_text(p, 400, secrets) for p in failure.get("problems", [])], "attempts": attempts,
                "kept": bool(attempts) or bool(failure.get("problems")),
                "last_reply": _reply_text(failure.get("last_reply"), secrets),
            }
        out.append({
            "key": key, "name": names.get(key, key), "outcome": row["outcome"], "outcome_words": OUTCOME_WORDS.get(row["outcome"], row["outcome"]),
            "attempts": row["attempts"], "version": row["version"], "cost_usd": row["cost_usd"], "reason": row["reason"], "failure": detail,
            "parts": _parts(detail),
        })
    return out


def _parts(detail: Mapping | None) -> list[dict]:
    """For an artefact written part by part: which parts had problems, in order."""
    if not detail:
        return []
    seen: dict[tuple[int, int], int] = {}
    for attempt in detail["attempts"]:
        for problem in attempt["problems"]:
            found = part_of(problem)
            if found:
                seen[found] = seen.get(found, 0) + 1
    return [{"part": p, "of": n, "problems": count} for (p, n), count in sorted(seen.items())]


def _attempts(run_id: str, traces: Sequence[Mapping], secrets: Iterable[str]) -> list[dict]:
    """Every model attempt of the run (a trace row), with the provider's error text cleaned and cut. No prompt, no answer."""
    out: list[dict] = []
    for t in traces:
        if t.get("run_id") != run_id:
            continue
        out.append({
            "agent": t.get("agent"), "phase_key": t.get("phase_key"), "model": t.get("served_model"), "requested_model": t.get("requested_model"),
            "fallback_used": bool(t.get("fallback_used")), "status": t.get("status"), "attempt": t.get("attempt"), "call_id": t.get("call_id"),
            "tokens_in": t.get("tokens_in", 0), "tokens_out": t.get("tokens_out", 0), "latency_ms": t.get("latency_ms", 0), "cost_usd": t.get("cost_usd"),
            "at": t.get("created_at"), "error": clean_text(t.get("error"), secrets=secrets) if t.get("error") else None,
        })
    return out


def run_summary(run: Mapping, artefacts: Sequence[Mapping], requested_by: str | None = None) -> dict:
    totals = run.get("totals") or {}
    failed = [a for a in artefacts if a["outcome"] == "failed"]
    return {
        "run_id": run["id"], "phase": run["phase_key"], "status": run["status"], "status_words": RUN_WORDS.get(run["status"], run["status"]),
        "stop_reason": run.get("stop_reason"), "stop_words": STOP_WORDS.get(run.get("stop_reason") or ""), "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"), "seconds": totals.get("elapsed_seconds", _seconds(run.get("started_at"), run.get("finished_at"))),
        "requested_by": requested_by, "calls": totals.get("calls"), "cost_usd": totals.get("cost_usd"),
        "artefacts": len(artefacts), "failed": len(failed), "failed_names": [a["name"] for a in failed],
        # A failed artefact with no stored record at all (an older run) has nothing to be "kept"; the note on the run says so instead.
        "detail_kept": all(a["failure"] is not None and a["failure"]["kept"] for a in failed) if failed else True,
    }


def run_detail(
    run: Mapping, events: Sequence[Mapping], traces: Sequence[Mapping], names: Mapping[str, str], requested_by: str | None = None, secrets: Iterable[str] = (),
) -> dict:
    """One run in full: its summary, each artefact with the reasons it failed, and every model attempt."""
    artefacts = _artefact_rows(run, events, names, list(secrets))
    summary = run_summary(run, artefacts, requested_by)
    notes = [NOT_KEPT] if any(a["outcome"] == "failed" and (a["failure"] is None or not a["failure"]["kept"]) for a in artefacts) else []
    if run["status"] == "failed" and not artefacts:
        notes.append("The run failed before it started any artefact.")
    return {"run": summary, "artefacts": artefacts, "attempts": _attempts(run["id"], traces, list(secrets)), "notes": notes}


def stage_list(
    phases: Sequence[Mapping], runs: Sequence[Mapping], decisions: Sequence[Mapping], events_by_run: Mapping[str, Sequence[Mapping]],
    names: Mapping[str, str], requesters: Mapping[str, str] | None = None, secrets: Iterable[str] = (),
) -> dict:
    """Every stage (phase) of the project, with its runs newest first and the decisions people made on it. `phases` are {key, name, status}, in order."""
    requesters = requesters or {}
    stages: list[dict] = []
    for phase in phases:
        mine = [r for r in runs if r["phase_key"] == phase["key"]]
        items = []
        for run in sorted(mine, key=lambda r: (r.get("started_at") or "", r["id"]), reverse=True):
            rows = _artefact_rows(run, events_by_run.get(run["id"], ()), names, list(secrets))
            items.append(run_summary(run, rows, requesters.get(run["id"])))
        stages.append({
            "key": phase["key"], "name": phase["name"], "status": phase["status"], "runs": items, "run_count": len(items),
            "failed_runs": sum(1 for r in items if r["status"] in ("failed", "interrupted")),
            "last_run_at": items[0]["started_at"] if items else None,
            "decisions": [d for d in decisions if d.get("phase") == phase["key"]],
        })
    return {"stages": stages, "run_count": sum(s["run_count"] for s in stages), "failed_runs": sum(s["failed_runs"] for s in stages)}


def decision_rows(audit: Iterable[Mapping]) -> list[dict]:
    """The decisions people made on a stage, from the audit trail, oldest first: approved, sent back, reopened."""
    out: list[dict] = []
    for a in audit:
        action, detail = a.get("action"), a.get("detail") or {}
        if action == "phase.decided":
            what = "approved" if detail.get("decision") == "approve" else "sent back for changes"
            out.append({"phase": a.get("target_id"), "at": a.get("at"), "by": a.get("actor"), "what": what, "note": clean_text(detail.get("feedback"), 300) or None})
        elif action == "phase.reopened":
            out.append({"phase": a.get("target_id"), "at": a.get("at"), "by": a.get("actor"), "what": "reopened", "note": clean_text(detail.get("reason"), 300) or None})
    return out


def markdown(detail: Mapping, project_name: str) -> str:
    """A run's log as a plain Markdown file for sharing. Text only; every sentence has already been cleaned."""
    run = detail["run"]
    lines = [f"# Log of run {run['run_id']} ({project_name})", "",
             f"- Stage: {run['phase']}", f"- Result: {run['status_words']}" + (f" ({run['stop_words']})" if run.get("stop_words") else ""),
             f"- Started: {run['started_at']}" + (f" by {run['requested_by']}" if run.get("requested_by") else ""),
             f"- Finished: {run['finished_at'] or 'not yet'}", f"- Model calls: {run['calls'] if run['calls'] is not None else 'unknown'}",
             f"- Cost: {'unknown' if run['cost_usd'] is None else '$%.4f' % run['cost_usd']}", ""]
    for note in detail["notes"]:
        lines += [f"> {note}", ""]
    lines += ["## Artefacts", ""]
    for a in detail["artefacts"]:
        lines.append(f"### {a['name']}: {a['outcome_words']}" + (f" (version {a['version']})" if a["version"] else ""))
        if a["reason"]:
            lines.append(f"Reason: {a['reason']}")
        failure = a["failure"]
        if failure:
            lines.append(f"Why it failed: {failure['reason']}")
            if failure["cut_off"]:
                lines.append("The reply was cut off at its output limit.")
            for attempt in failure["attempts"]:
                lines.append(f"- Attempt {attempt['attempt']}:")
                lines += [f"  - {p}" for p in attempt["problems"]] or ["  - (no problems recorded)"]
            if not failure["attempts"] and failure["problems"]:
                lines += [f"- {p}" for p in failure["problems"]]
        lines.append("")
    lines += ["## Model attempts", ""]
    for t in detail["attempts"]:
        lines.append(f"- {t['at']} {t['agent']} on {t['model']}{' (fallback)' if t['fallback_used'] else ''}: {t['status']}, {t['tokens_in']} in, {t['tokens_out']} out, {t['latency_ms']} ms"
                     + (f". Error: {t['error']}" if t["error"] else ""))
    return "\n".join(lines).rstrip() + "\n"
