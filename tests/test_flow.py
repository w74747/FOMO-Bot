import fakeredis.aioredis

from src.analysis.flow import FlowCollector, flow_summary, format_flow, format_wallets, holders_change
from src.config import settings
from src.core.state import StateManager
from src.data.birdeye import BirdeyeClient, parse_top_traders
from src.data.geckoterminal import parse_trades


def test_parse_trades():
    payload = {"data": [
        {"attributes": {"block_timestamp": "2026-11-27T10:00:05Z", "kind": "sell", "tx_hash": "b",
                        "volume_in_usd": "120.5", "tx_from_address": "W2", "price_from_in_usd": "0.01"}},
        {"attributes": {"block_timestamp": "2026-11-27T10:00:00Z", "kind": "buy", "tx_hash": "a",
                        "volume_in_usd": "1500", "tx_from_address": "W1", "price_to_in_usd": "0.0099"}},
        {"attributes": {"block_timestamp": "bad", "kind": "buy", "tx_hash": "c"}},
    ]}
    tr = parse_trades(payload)
    assert [t["tx"] for t in tr] == ["a", "b"] and tr[0]["usd"] == 1500 and tr[0]["trader"] == "W1"


def test_flow_summary_and_holders():
    now = 10_000
    tr = [{"ts": now - 100, "kind": "buy", "usd": 1500, "trader": "S"},
          {"ts": now - 200, "kind": "buy", "usd": 100, "trader": "X"},
          {"ts": now - 300, "kind": "sell", "usd": 400, "trader": "Y"},
          {"ts": now - 5000, "kind": "sell", "usd": 9999, "trader": "Y"}]
    f = flow_summary(tr, now, 3600, smart={"S"})
    assert f["buy_usd"] == 1600 and f["sell_usd"] == 400 and f["big_buys"] == 1 and f["smart_buys"] == 1
    assert abs(f["buy_share"] - 0.8) < 1e-9
    assert abs(holders_change([(0, 100), (90_000, 110)], 90_000, 86_400) - 0.10) < 1e-9
    assert holders_change([(90_000, 110)], 90_000, 86_400) is None
    txt = format_flow([{"symbol": "AB<", "h1": f, "m15": f, "holders_24h": 0.1}], 3, "note")
    assert "AB&lt;" in txt and "+10.0%" in txt


def test_parse_top_traders_and_wallet_format():
    p = {"data": {"items": [{"owner": "WALLET1234567890", "total_pnl": 1200, "volume_usd": 5000, "trade": 12,
                             "tags": ["sniper-bot"]}, {"no": "owner"}]}}
    t = parse_top_traders(p)
    assert t == [{"wallet": "WALLET1234567890", "pnl": 1200.0, "realized": 0.0, "volume": 5000.0, "trades": 12,
                  "tags": ["sniper-bot"]}]
    txt = format_wallets([{"wallet": "WALLET1234567890", "coins": 2, "pnl": 1200, "symbols": "A,B,A"}], "n")
    assert "WALL…7890" in txt and "2 عملة" in txt
    assert "لا توجد" in format_wallets([], "n")


class _Http:
    def __init__(self, exc=None):
        self.exc = exc

    async def get_json(self, *a, **k):
        if self.exc:
            raise self.exc
        return {"data": {"items": [{"owner": "W", "total_pnl": 5}]}}


async def test_birdeye_denied_plan_stops_calls():
    from src.data.http import HttpError
    b = BirdeyeClient(_Http(HttpError(403, "u")), "key")
    assert await b.top_traders("M") == [] and b.denied and not b.ready
    assert BirdeyeClient(_Http(), None).ready is False
    assert (await BirdeyeClient(_Http(), "k").top_traders("M"))[0]["wallet"] == "W"


class _DB:
    ready = True

    def __init__(self):
        self.saved = {}

    async def active_pools(self, within_hours=6):
        return [{"pool": "P2", "mint": "M2", "symbol": "B", "liquidity_usd": 9e5},
                {"pool": "P1", "mint": "M1", "symbol": "A", "liquidity_usd": 1e5}]

    async def last_trade_ts(self, pool):
        return 100

    async def save_trades(self, pool, tr):
        self.saved[pool] = tr
        return len(tr)


class _Gecko:
    async def trades(self, pool):
        return [{"tx": "t", "ts": 1000, "kind": "buy", "usd": 1, "trader": "x", "price": 1}]


async def test_targets_put_watchlist_first_and_count_gaps(monkeypatch):
    import src.analysis.flow as fl

    async def nosleep(_):
        return None
    monkeypatch.setattr(fl.asyncio, "sleep", nosleep)
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    await state.set_watch_item("M1", {"pool": "P1", "symbol": "A"})
    db = _DB()
    fc = FlowCollector(db, _Gecko(), None, BirdeyeClient(_Http(), None), state, settings)
    assert [t["pool"] for t in await fc.targets()] == ["P1", "P2"]
    n, new = await fc.collect_trades()
    assert n == 2 and new == 2 and fc.gaps == 2          # oldest returned trade is after the last stored + 60 s
    assert "BIRDEYE_API_KEY" in fc.birdeye_note()
