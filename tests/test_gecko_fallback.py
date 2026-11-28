import asyncio

from src.data.geckoterminal import GeckoClient
from src.data.http import HttpError, quota_exhausted

CAP = '{"status":{"error_code":10006,"error_message":"You\'ve reached 10,000 calls limit."}}'


class FakeHttp:
    def __init__(self):
        self.urls = []

    async def get_json(self, url, **kw):
        self.urls.append((url, dict(kw.get("headers") or {})))
        if "api.coingecko.com" in url:
            raise HttpError(429, url, CAP)
        return {"data": []}


def test_quota_detection():
    assert quota_exhausted(429, CAP)
    assert not quota_exhausted(429, "rate limited")
    assert not quota_exhausted(500, CAP)


def test_demo_cap_switches_to_keyless():
    http = FakeHttp()
    g = GeckoClient(http, 8, "demo-key")
    assert asyncio.run(g.trades("POOL")) == []
    assert g.fallback and g.base == GeckoClient.KEYLESS_BASE
    assert "x-cg-demo-api-key" not in http.urls[-1][1]
    asyncio.run(g.trades("POOL2"))
    assert len(http.urls) == 3  # no more demo calls after the switch


def test_other_429_still_raises():
    class H(FakeHttp):
        async def get_json(self, url, **kw):
            raise HttpError(429, url, "slow down")
    g = GeckoClient(H(), 8, "demo-key")
    try:
        asyncio.run(g.trades("P"))
        assert False
    except HttpError:
        pass
    assert not g.fallback


def test_back_to_the_key_next_month():
    from src.data.geckoterminal import GeckoClient
    g = GeckoClient(http=None, demo_key="k")
    keyed_base = g.base
    g._use_keyless()
    assert g.fallback and g.base == GeckoClient.KEYLESS_BASE
    g._maybe_restore()
    assert g.fallback                                  # same month: stay keyless
    g._fallback_month = "2000-01"
    g._maybe_restore()
    assert not g.fallback and g.base == keyed_base


async def test_flow_only_watchlist_when_keyless(monkeypatch):
    import fakeredis.aioredis
    import src.analysis.flow as fl
    from src.analysis.flow import FlowCollector
    from src.config import settings
    from src.core.state import StateManager
    from tests.test_flow import _DB, _Gecko

    async def nosleep(_):
        return None
    monkeypatch.setattr(fl.asyncio, "sleep", nosleep)
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    await state.set_watch_item("M1", {"pool": "P1", "symbol": "A"})
    g = _Gecko()
    g.fallback = True
    fc = FlowCollector(_DB(), g, None, None, state, settings)
    n, _ = await fc.collect_trades()
    assert n == 1


class CountingHttp:
    """multi call fails (429) or returns nothing; counts single-token calls."""
    def __init__(self, multi_fails: bool):
        self.multi_fails = multi_fails
        self.single = 0

    async def get_json(self, url, **kw):
        if "/tokens/multi/" in url:
            if self.multi_fails:
                raise HttpError(429, url, "rate limited")
            return {"data": [], "included": []}
        if "/tokens/" in url and url.endswith("/pools"):
            self.single += 1
        return {"data": [], "included": []}


def test_best_pools_no_per_token_storm_after_failed_multi():
    http = CountingHttp(multi_fails=True)
    g = GeckoClient(http, 8, None)
    assert asyncio.run(g.best_pools([f"M{i}" for i in range(30)])) == {}
    assert http.single == 0


def test_best_pools_no_per_token_calls_on_keyless():
    http = CountingHttp(multi_fails=False)
    g = GeckoClient(http, 8, "demo-key")
    g._use_keyless()
    asyncio.run(g.best_pools(["A", "B", "C"]))
    assert http.single == 0


def test_best_pools_per_token_on_keyed_api():
    http = CountingHttp(multi_fails=False)
    g = GeckoClient(http, 8, "demo-key")
    asyncio.run(g.best_pools(["A", "B"]))
    assert http.single == 2
