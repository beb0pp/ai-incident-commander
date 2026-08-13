"""Investigation checkpointing.

State is written after every graph level. Two things fall out of that:

* An investigation that dies halfway is still inspectable — you can see exactly
  which agent produced what before the failure.
* The API can serve a live view of a long-running investigation without holding
  the run in memory in the request handler.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import structlog

from aic.orchestration.state import InvestigationState

log = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 24 * 60 * 60


class CheckpointStore(Protocol):
    async def save(self, state: InvestigationState) -> None: ...

    async def load(self, run_id: str) -> InvestigationState | None: ...


class InMemoryCheckpointStore:
    """Process-local checkpoints. Used by tests and by single-process runs."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def save(self, state: InvestigationState) -> None:
        self._data[state.run_id] = state.snapshot()

    async def load(self, run_id: str) -> InvestigationState | None:
        payload = self._data.get(run_id)
        return InvestigationState.restore(payload) if payload else None

    def __len__(self) -> int:
        return len(self._data)


class RedisCheckpointStore:
    """Redis-backed checkpoints, so any API replica can read any run's state."""

    def __init__(
        self,
        redis: object,
        *,
        prefix: str = "aic:run:",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        # Typed as ``object`` so importing this module never requires redis to be
        # installed; the client is injected by the composition root.
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl_seconds

    def _key(self, run_id: str) -> str:
        return f"{self._prefix}{run_id}"

    async def save(self, state: InvestigationState) -> None:
        payload = json.dumps(state.snapshot(), default=str)
        await self._redis.set(self._key(state.run_id), payload, ex=self._ttl)  # type: ignore[attr-defined]

    async def load(self, run_id: str) -> InvestigationState | None:
        raw = await self._redis.get(self._key(run_id))  # type: ignore[attr-defined]
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return InvestigationState.restore(json.loads(raw))
        except (ValueError, TypeError) as exc:
            log.warning("checkpoint.corrupt", run_id=run_id, error=str(exc))
            return None
