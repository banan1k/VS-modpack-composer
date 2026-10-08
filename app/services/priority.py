from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class PriorityCoordinator:
    """Fair priority gate for background prefetch vs user work.

    Background work may run concurrently up to PREFETCH_CONCURRENCY. Once a user job arrives,
    no new background work starts and the user waits only for currently active background I/O to
    finish. Background work remains paused while user work is active.
    """

    def __init__(self):
        self._condition = asyncio.Condition()
        self._background_running = 0
        self._user_waiting = 0
        self._user_running = False

    @asynccontextmanager
    async def user_priority(self):
        async with self._condition:
            self._user_waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: self._background_running == 0 and not self._user_running
                )
                self._user_running = True
            finally:
                self._user_waiting -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._user_running = False
                self._condition.notify_all()

    @asynccontextmanager
    async def background_slot(self):
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._user_running and self._user_waiting == 0
            )
            self._background_running += 1
        try:
            yield
        finally:
            async with self._condition:
                self._background_running -= 1
                self._condition.notify_all()
