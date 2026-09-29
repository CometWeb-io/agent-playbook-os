from __future__ import annotations

import asyncio
from typing import Protocol


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...
    def reason(self) -> str | None: ...
    async def wait(self) -> None: ...


class MemoryCancellationToken:
    def __init__(self):
        self._event = asyncio.Event()
        self._reason: str | None = None

    def cancel(self, reason: str = "requested"):
        self._reason = reason
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def reason(self) -> str | None:
        return self._reason

    async def wait(self) -> None:
        await self._event.wait()


class StoreCancellationToken:
    def __init__(self, store, poll_seconds: float = 0.05):
        self.store = store
        self.poll_seconds = poll_seconds

    def is_cancelled(self) -> bool:
        return self.store.cancellation_request() is not None

    def reason(self) -> str | None:
        req = self.store.cancellation_request()
        return str(req.get("reason", "requested")) if req else None

    async def wait(self) -> None:
        while not self.is_cancelled():
            await asyncio.sleep(self.poll_seconds)
