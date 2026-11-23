from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from typing import Awaitable, Callable

from ...config import Settings
from ..errors import ReplayMiss
from ..recordings import (
    MAX_REPLAY_LATENCY_MS,
    Recording,
    RecordingStore,
    leaks_secret,
    request_key,
)
from ..types import LLMRequest, ProviderResponse
from .base import Provider

log = logging.getLogger(__name__)


_MEMORY_NOTE = re.compile(r"MEM-(\d{3,})")


def relative_notes(request: LLMRequest) -> tuple[LLMRequest, dict[str, str]]:
    """The same request with its feedback-note IDs (MEM-nnn) renumbered 001, 002... in the order they first appear, and the map from the new IDs back to the real ones.

    A note's number is the database's own row counter, so the first note of a project is MEM-001 only in an empty database. A recording made in a fresh database says
    MEM-001..MEM-006; the same step in a database that already holds notes asks for MEM-004..MEM-009, and an exact-text lookup would miss for no reason a person could see.
    The renumbering is done at the lookup only: nothing stored changes, and a recording made with MEM-001, 002... renumbers to itself."""
    order: dict[str, str] = {}

    def renumber(match: re.Match) -> str:
        return order.setdefault(match.group(0), f"MEM-{len(order) + 1:03d}")

    system = _MEMORY_NOTE.sub(renumber, request.system)
    messages = tuple(replace(m, content=_MEMORY_NOTE.sub(renumber, m.content)) for m in request.messages)
    if not order:
        return request, {}
    return replace(request, system=system, messages=messages), {new: old for old, new in order.items()}


class ReplayProvider:
    """Answers from saved replies and never touches the network."""

    label = "replay"

    def __init__(
        self,
        store: RecordingStore,
        *,
        pace: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.store = store
        self.pace = pace
        self._sleep = sleep

    async def complete(self, model_id: str, request: LLMRequest) -> ProviderResponse:
        key = request_key(model_id, request)
        recording = self.store.get(key)
        back: dict[str, str] = {}
        if recording is None:
            # Not under its own numbers: try again with the feedback notes numbered from 001 (see relative_notes), and give the reply the real numbers back.
            renumbered, back = relative_notes(request)
            if back:
                recording = self.store.get(request_key(model_id, renumbered))
        if recording is None:
            raise ReplayMiss(
                f"no recording for role '{request.role}' on model '{model_id}' "
                f"(key {key[:12]}; {self.store.count_for_model(model_id)} recording(s) exist for this model). "
                "Record it by running in live mode with RECORD_LIVE_CALLS=true; any change to a prompt, "
                "a limit or a model ID needs a fresh recording."
            )
        delay = min(recording.latency_ms, MAX_REPLAY_LATENCY_MS) / 1000 * self.pace
        if delay > 0:
            await self._sleep(delay)
        text = _MEMORY_NOTE.sub(lambda m: back.get(m.group(0), m.group(0)), recording.text) if back else recording.text
        return ProviderResponse(text, recording.usage, recording.finish_reason)


class RecordingProvider:
    """Calls the real provider and saves every successful reply. The reply is returned unchanged."""

    def __init__(self, inner: Provider, store: RecordingStore, settings: Settings):
        self.inner = inner
        self.store = store
        self._settings = settings

    @property
    def label(self) -> str:
        return f"{self.inner.label}+recording"

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()

    async def complete(self, model_id: str, request: LLMRequest) -> ProviderResponse:
        started = time.perf_counter()
        response = await self.inner.complete(model_id, request)  # errors pass through, unrecorded
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        try:
            await asyncio.to_thread(self._save, model_id, request, response, latency_ms)
        except Exception:
            # A recording is a convenience. It must never cost a reply that has already been paid for.
            log.warning("could not save a recording for role %s", request.role, exc_info=True)
        return response

    def _save(self, model_id: str, request: LLMRequest, response: ProviderResponse, latency_ms: int) -> None:
        if not response.text.strip():
            return  # only successful replies are recorded
        recording = Recording.from_call(model_id, request, response, latency_ms)
        if leaks_secret(recording, self._settings.secret_values()):
            # Fail closed, and say so without repeating the value.
            log.warning("recording skipped for role %s: it contains a configured credential", request.role)
            return
        self.store.save(recording)
