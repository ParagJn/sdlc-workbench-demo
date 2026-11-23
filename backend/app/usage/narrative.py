"""The words of the usage report. Fixed sentence templates with the numbers filled in, so the same data always reads the same and a test can check every sentence.

Nothing here calls a model. A sentence is only written when it is true: no sentence about fallbacks when none happened, none about practice runs when there are none.
"""
from __future__ import annotations

from datetime import datetime


def money(value: float | None) -> str:
    """Dollars and cents. None (a price nobody knew) is never shown as $0.00."""
    return "unknown" if value is None else f"${value:,.2f}"


def count(n: int) -> str:
    return f"{n:,}"


def tokens(n: int) -> str:
    """12,300 -> '12.3 thousand'; 3,687,514 -> '3.7 million'; under a thousand as is."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} million"
    if n >= 1_000:
        return f"{n / 1_000:.1f} thousand"
    return str(n)


def percent(part: float, whole: float) -> int:
    return 0 if whole <= 0 else round(100 * part / whole)


def day(iso: str) -> str:
    """'2026-10-30T13:50:26.126Z' -> '30 Oct'."""
    return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d %b").lstrip("0")


def span(first: str, last: str) -> str:
    a, b = day(first), day(last)
    return f"on {a}" if a == b else f"between {a} and {b}"


def _up(text: str) -> str:
    """Capitalise only the first letter ('between 30 Oct and 4 Nov' -> 'Between 30 Oct and 4 Nov'); str.capitalize() would lower-case the month names."""
    return text[:1].upper() + text[1:]


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{count(n)} {one if n == 1 else (many or one + 's')}"


# Human names for the phases and for the model families, so the sentences read like a person wrote them.
PHASE_NAMES = {
    "setup": "Setup", "requirements": "Requirements", "design": "Design", "architecture": "Architecture",
    "build": "Build", "deploy": "Deploy", "handover": "Handover",
}


def phase_name(key: str | None) -> str:
    return "Outside any phase" if not key else PHASE_NAMES.get(key, key.replace("_", " ").title())


def model_name(served: str) -> str:
    """'claude-sonnet-5-5' -> 'Claude Sonnet', 'gpt-6.1-sol' -> 'OpenAI', 'gemini-3.7-flash' -> 'Gemini'. Anything else is shown as it is."""
    s = served.lower()
    if s.startswith("claude-sonnet"):
        return "Claude Sonnet"
    if s.startswith("claude-haiku"):
        return "Claude Haiku"
    if s.startswith("claude-opus"):
        return "Claude Opus"
    if s.startswith("claude"):
        return "Claude"
    if s.startswith(("gpt", "o1", "o3", "o4")):
        return "OpenAI"
    if s.startswith("gemini"):
        return "Gemini"
    return served


def sentences(r: dict) -> list[str]:
    """The story, in a fixed order. `r` is the report built by report.build_report; each sentence is added only when the numbers support it."""
    out: list[str] = []
    live, practice = r["live"], r["practice"]
    if live["calls"] == 0 and practice["calls"] == 0:
        return ["No model calls have been recorded for this project yet. The report fills in as soon as a phase runs."]

    if live["calls"] > 0:
        out.append(
            f"{_up(span(live['first'], live['last']))} this project made {plural(live['calls'], 'model call')} "
            f"and spent {money(live['cost_usd'])}, using {tokens(live['tokens_in'])} tokens in and {tokens(live['tokens_out'])} out."
        )
        phases = [g for g in r["by_phase"] if g["cost_usd"]]
        if phases and live["cost_usd"]:
            top = phases[0]
            line = f"{phase_name(top['key'])} was the largest part, at {percent(top['cost_usd'], live['cost_usd'])}% of the spend ({money(top['cost_usd'])})"
            if len(phases) > 1:
                nxt = phases[1]
                line += f", then {phase_name(nxt['key'])} at {percent(nxt['cost_usd'], live['cost_usd'])}%"
            out.append(line + ".")
        models = [g for g in r["by_model"] if g["cost_usd"]]
        if models and live["cost_usd"]:
            top = models[0]
            line = f"{model_name(top['key'])} did most of the paid work ({percent(top['cost_usd'], live['cost_usd'])}% of the spend)"
            others = [model_name(g["key"]) for g in models[1:3]]
            if others:
                line += "; " + " and ".join(others) + " handled the rest"
            out.append(line + ".")
        if live["unpriced_calls"]:
            out.append(f"{plural(live['unpriced_calls'], 'call')} had no known price, so the total may be higher than shown.")
        if live["errors"] == 0:
            out.append("Every call succeeded." + (f" {plural(live['fallbacks'], 'call')} used a fallback model." if live["fallbacks"] else ""))
        else:
            out.append(
                f"{plural(live['errors'], 'call')} failed" + (f" and {plural(live['fallbacks'], 'call')} used a fallback model." if live["fallbacks"] else ".")
            )
        if live["calls"] and live["cost_usd"] is not None:
            out.append(f"On average a call cost {money(live['cost_usd'] / live['calls'])}.")
    else:
        out.append("This project has no paid model calls; everything recorded was a practice run.")

    if practice["calls"]:
        out.append(
            f"It also has {plural(practice['calls'], 'practice call')} on the built-in practice models. "
            "These use no real model, so they are kept out of the cost above."
        )
    return out
