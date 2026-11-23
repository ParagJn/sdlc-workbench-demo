"""The run's money and clock, and the pure scheduling decisions. No I/O, no asyncio: everything here is plain logic.

A cap is checked at the same two moments everywhere: before an artefact starts, and after each model call returns.
Money is counted only when known. A call with no configured price is counted as unpriced, never as zero, so a
budget cannot be quietly defeated by a missing price.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

STOP_BUDGET = "budget"
STOP_TIME = "time"
STOP_CANCELLED = "cancelled"


@dataclass
class Meter:
    """Tallies one run. Thread-safe: calls complete on different tasks and the totals are read from anywhere."""

    max_cost_usd: float  # 0 switches the cap off
    max_seconds: float  # 0 switches the cap off
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cost: float = field(default=0.0, init=False)
    _calls: int = field(default=0, init=False)
    _unpriced: int = field(default=0, init=False)
    _tokens_in: int = field(default=0, init=False)
    _tokens_out: int = field(default=0, init=False)
    _cancelled: bool = field(default=False, init=False)
    _stop: str | None = field(default=None, init=False)
    _cost_when_stopped: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    # ---- recording ----------------------------------------------------------------------------------

    def add_call(self, cost_usd: float | None, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self._calls += 1
            self._tokens_in += tokens_in
            self._tokens_out += tokens_out
            if cost_usd is None:
                self._unpriced += 1
            else:
                self._cost += cost_usd

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    # ---- the question every check asks -----------------------------------------------------------------

    def should_stop(self) -> str | None:
        """Why no more work should start, or None. Once a reason is given it stays: a run does not un-stop."""
        with self._lock:
            if self._stop is not None:
                return self._stop
            reason = None
            if self._cancelled:
                reason = STOP_CANCELLED
            elif self.max_cost_usd > 0 and self._cost >= self.max_cost_usd:
                reason = STOP_BUDGET
            elif self.max_seconds > 0 and self.clock() - self.started >= self.max_seconds:
                reason = STOP_TIME
            if reason is not None:
                self._stop = reason
                self._cost_when_stopped = self._cost
            return reason

    # ---- reading --------------------------------------------------------------------------------------------

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return self._cost

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    def snapshot(self) -> dict:
        with self._lock:
            over = None
            if self._stop == STOP_BUDGET and self.max_cost_usd > 0:
                over = round(max(0.0, self._cost - self.max_cost_usd), 6)
            return {
                "calls": self._calls,
                "unpriced_calls": self._unpriced,
                "tokens_in": self._tokens_in,
                "tokens_out": self._tokens_out,
                "cost_usd": round(self._cost, 6),
                "cost_is_partial": self._unpriced > 0,  # known costs only: some calls had no price
                "over_budget_by_usd": over,
                "elapsed_seconds": round(self.clock() - self.started, 3),
            }


# ---- pure scheduling ------------------------------------------------------------------------------------------

def ready(order: Sequence[str], after: Mapping[str, Sequence[str]], done: set[str], started: set[str]) -> list[str]:
    """Artefacts that may start now: not started, and everything they wait for has finished. Catalogue order."""
    return [k for k in order if k not in started and all(d in done for d in after.get(k, ()) if d in order)]


def blocked_by(
    order: Sequence[str], after: Mapping[str, Sequence[str]], failed: Mapping[str, str], skipped: Mapping[str, str]
) -> dict[str, str]:
    """For every not-yet-decided artefact that (directly or not) needs a failed or skipped one: which one blocks it.

    `failed` and `skipped` map an artefact key to a reason. The answer names the nearest cause that is itself a
    failure, so a chain of skips all point back at the real fault.
    """
    out: dict[str, str] = {}
    cause: dict[str, str] = {k: k for k in failed}
    for key in order:
        if key in failed or key in skipped:
            cause.setdefault(key, cause.get(key, key))
            continue
        for dep in after.get(key, ()):
            if dep in order and dep in cause:
                out[key] = cause[dep]
                cause[key] = cause[dep]
                break
    return out


def waits_with_reads(after: Mapping[str, Sequence[str]], reads: Mapping[str, Sequence[str]], wanted: Sequence[str], order: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """The plan's ordering plus what each chosen artefact READS from the same run (2026-11-16). The plan orders the epics after the requirements, but they READ the personas too; when the
    personas and the epics are written in one run, the epics must wait for the personas or they would be built on the old ones. A read counts only for an artefact that is part of this run and
    that the plan puts EARLIER, so it can never make a loop."""
    place = {k: n for n, k in enumerate(order)}
    chosen = set(wanted)
    out: dict[str, tuple[str, ...]] = {}
    for key in order:
        extra = [d for d in reads.get(key, ()) if d in chosen and key in chosen and d != key and place.get(d, len(order)) < place[key] and d not in after.get(key, ())]
        out[key] = (*after.get(key, ()), *extra)
    return out


def group_problems(
    after: Mapping[str, Sequence[str]], reads: Mapping[str, Sequence[str]], written: Iterable[str], only: Iterable[str], names: Mapping[str, str] | None = None,
) -> list[str]:
    """Plain sentences about a chosen GROUP of artefacts to write together (2026-11-14). [] means the group can run.

    `after` is the plan's ordering (what each artefact waits for); `reads` is what the artefact's skill needs as REQUIRED input, which is the real list (the epics read the personas,
    though the plan only orders them after the requirements). Two things must hold for each chosen artefact:
      1. everything it reads is already written, or is in the group; otherwise the run would start and the artefact would fail ('has no content and is not part of this run');
      2. everything it reads that IS in the group comes before it in the plan, so the run writes it first. Otherwise the two could be written at the same time and the reader
         would be built on the old version.
    An artefact already written is rewritten from what is written now, so only a MISSING input counts for the first."""
    chosen = set(only)
    have = set(written)
    name = (lambda k: names.get(k, k)) if names else (lambda k: k)
    out: list[str] = []
    for key in [k for k in reads if k in chosen]:
        missing = [dep for dep in reads[key] if dep not in chosen and dep not in have]
        if missing:
            listed = ", ".join(f"'{name(m)}'" for m in missing)
            out.append(f"'{name(key)}' is written from {listed}, which {'is' if len(missing) == 1 else 'are'} not written yet and not in this group. Add {'it' if len(missing) == 1 else 'them'} to the group, or write {'it' if len(missing) == 1 else 'them'} first")
    return out


def wanted_keys(
    plan_keys: Sequence[str], has_version: Mapping[str, bool], *, only: Iterable[str] | None, rewrite_all: bool
) -> list[str]:
    """Which artefacts a run will produce. Default: those with no version yet. `only` names some; `rewrite_all` takes all."""
    if only is not None:
        chosen = set(only)
        return [k for k in plan_keys if k in chosen]
    if rewrite_all:
        return list(plan_keys)
    return [k for k in plan_keys if not has_version.get(k, False)]
