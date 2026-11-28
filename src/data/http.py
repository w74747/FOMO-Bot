"""Shared HTTP session with rate limiting and retries."""
import asyncio
import re
import time
from collections import deque

import aiohttp
from loguru import logger


_SECRET_RE = re.compile(r"((?:api[-_]?key|x-api-key|apikey)=)[^&\s\"']+", re.I)


def redact(text: str) -> str:
    """Hide API keys in URLs before anything is logged or sent to Telegram."""
    return _SECRET_RE.sub(r"\1***", text)


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(redact(f"HTTP {status} for {url}: {body[:200]}"))
        self.status = status
        self.body = body or ""


def quota_exhausted(status: int, body: str) -> bool:
    """CoinGecko monthly credit cap (error_code 10006): retrying cannot help."""
    return status == 429 and ("10006" in (body or "") or "calls limit" in (body or ""))


class RateLimiter:
    """Spaces calls evenly: at most `per_minute` calls per minute."""

    def __init__(self, per_minute: float):
        self.interval = 60.0 / max(per_minute, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, high: bool = False):
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(now, self._next) + self.interval

    def penalize(self, seconds: float):
        """Push the next slot back (used after a 429)."""
        self._next = max(self._next, time.monotonic() + seconds)


class PriorityLimiter:
    """Sliding 60-second budget shared by everything that uses one API account.

    High-priority calls (prices for open positions, swap orders) may use the whole budget;
    low-priority calls (discovery scans) only what is left above `reserve`, so a scan can
    never starve the price monitor. A 429 blocks low priority for the full back-off but
    high priority for only a couple of seconds."""

    def __init__(self, per_minute: int, reserve: int, window: float = 60.0):
        self.cap = max(1, per_minute)
        self.reserve = min(max(0, reserve), self.cap - 1)
        self.window = window
        self._hits: deque[float] = deque()
        self._block = {True: 0.0, False: 0.0}
        self._lock = asyncio.Lock()

    def _room(self, high: bool, now: float) -> float:
        """Seconds to wait before a call of this priority may go (0 = now)."""
        while self._hits and now - self._hits[0] >= self.window:
            self._hits.popleft()
        if now < self._block[high]:
            return self._block[high] - now
        limit = self.cap if high else self.cap - self.reserve
        if len(self._hits) < limit:
            return 0.0
        # wait until enough old calls leave the window
        idx = len(self._hits) - limit
        return max(0.05, self._hits[idx] + self.window - now)

    async def acquire(self, high: bool = False):
        while True:
            async with self._lock:
                now = time.monotonic()
                wait = self._room(high, now)
                if wait <= 0:
                    self._hits.append(now)
                    return
            await asyncio.sleep(min(wait, 1.0) if high else wait)

    def penalize(self, seconds: float):
        now = time.monotonic()
        self._block[False] = max(self._block[False], now + seconds)
        self._block[True] = max(self._block[True], now + min(seconds, 2.0))

    def used(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._hits if now - t < self.window)


class Http:
    def __init__(self, timeout: float = 15):
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body=None,
        limiter: RateLimiter | PriorityLimiter | None = None,
        retries: int = 3,
        high: bool = False,
        max_delay: float | None = None,
    ):
        backoff = 2.0
        for attempt in range(retries + 1):
            if limiter:
                await limiter.acquire(high=high)
            try:
                s = await self.session()
                async with s.request(method, url, params=params, headers=headers, json=json_body) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    body = await resp.text()
                    if quota_exhausted(resp.status, body):
                        raise HttpError(resp.status, url, body)  # monthly cap: no retry
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        hinted = float(retry_after) if retry_after and retry_after.isdigit() else 0.0
                        delay = min(max(hinted, backoff * 15), 180.0)  # never retry instantly
                        if max_delay is not None:
                            delay = min(delay, max_delay)  # time-critical calls retry fast
                        if limiter and resp.status == 429:
                            limiter.penalize(delay)
                        logger.warning(f"{resp.status} from {redact(url)}, retry in {delay:.0f}s")
                        if attempt < retries:
                            await asyncio.sleep(delay)
                            backoff *= 2
                            continue
                    raise HttpError(resp.status, url, body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt >= retries:
                    raise HttpError(0, url, str(e)) from e
                await asyncio.sleep(backoff)
                backoff *= 2
        raise HttpError(0, url, "retries exhausted")

    async def get_json(self, url: str, **kw):
        return await self.request_json("GET", url, **kw)

    async def post_json(self, url: str, body, **kw):
        return await self.request_json("POST", url, json_body=body, **kw)
