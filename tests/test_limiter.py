import asyncio
import time

from src.data.http import PriorityLimiter


async def test_scans_cannot_starve_prices():
    lim = PriorityLimiter(per_minute=10, reserve=4, window=1.0)
    for _ in range(6):                       # low priority fills its share (10 - 4)
        await lim.acquire(high=False)
    t = time.monotonic()
    for _ in range(4):                       # reserved slots still free for prices
        await lim.acquire(high=True)
    assert time.monotonic() - t < 0.1
    t = time.monotonic()
    await lim.acquire(high=False)            # scan must wait for the window to roll
    assert time.monotonic() - t > 0.5


async def test_429_blocks_scans_long_but_prices_briefly():
    lim = PriorityLimiter(per_minute=100, reserve=10, window=60)
    lim.penalize(30)
    t = time.monotonic()
    await lim.acquire(high=True)
    assert time.monotonic() - t < 2.5         # prices wait ~2s, not 30s
    done = asyncio.ensure_future(lim.acquire(high=False))
    await asyncio.sleep(0.2)
    assert not done.done()                    # scans still blocked
    done.cancel()
