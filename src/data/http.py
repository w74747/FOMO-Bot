"""Shared HTTP session with rate limiting and retries."""
import asyncio
import time

import aiohttp
from loguru import logger


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


class RateLimiter:
    """Spaces calls evenly: at most `per_minute` calls per minute."""

    def __init__(self, per_minute: float):
        self.interval = 60.0 / max(per_minute, 0.1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(now, self._next) + self.interval

    def penalize(self, seconds: float):
        """Push the next slot back (used after a 429)."""
        self._next = max(self._next, time.monotonic() + seconds)


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
        limiter: RateLimiter | None = None,
        retries: int = 3,
    ):
        backoff = 2.0
        for attempt in range(retries + 1):
            if limiter:
                await limiter.acquire()
            try:
                s = await self.session()
                async with s.request(method, url, params=params, headers=headers, json=json_body) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    body = await resp.text()
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff * 15
                        if limiter and resp.status == 429:
                            limiter.penalize(delay)
                        logger.warning(f"{resp.status} from {url}, retry in {delay:.0f}s")
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
