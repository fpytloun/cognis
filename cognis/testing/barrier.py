"""Single-request test barrier; never imported by production controllers."""

import asyncio


class RequestBarrier:
    def __init__(self, *, timeout: float = 30) -> None:
        self.timeout = timeout
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = False
        self.claimed = False
        self.timed_out = False

    @property
    def held(self) -> bool:
        """Whether the claimed request is still blocked by this barrier."""
        return self.claimed and not self.cancelled and not self.released.is_set()

    async def hold(self) -> None:
        if self.claimed:
            raise RuntimeError("Barrier already claimed")
        self.claimed = True
        self.entered.set()
        try:
            async with asyncio.timeout(self.timeout):
                await self.released.wait()
        except TimeoutError:
            self.timed_out = True
            self.cancel()
            raise
        except BaseException:
            self.cancel()
            raise
        if self.cancelled:
            raise RuntimeError("Barrier cancelled")

    def release(self) -> None:
        if self.cancelled:
            raise RuntimeError("Barrier cancelled")
        self.released.set()

    def cancel(self) -> None:
        self.cancelled = True
        self.released.set()
