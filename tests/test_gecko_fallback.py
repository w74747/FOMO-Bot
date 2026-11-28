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
