import time
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr

from src.config import settings
from src.core.state import StateManager
from src.trading.autopilot import Autopilot

NOW = datetime.now(timezone.utc)


class DB:
    ready = True

    def __init__(self):
        self.rows = []

    async def closed_positions(self, since_hours=None):
        return self.rows


def paper(ret, mins_ago=10, tier=None):
    return {"mode": "paper", "return_pct": ret, "pnl_usd": ret / 5, "exit_reason": "وقف خسارة",
            "closed_at": NOW - timedelta(minutes=mins_ago), "tier": tier}


async def setup(health=None):
    st = StateManager(client=fr.FakeRedis(decode_responses=True))
    db, msgs = DB(), []

    async def notify(t):
        msgs.append(t)
    ap = Autopilot(st, db, settings, notify)
    if health is not None:
        await st.set_health({"value": health, "n": 40, "prev": 1.0, "ts": time.time()})
    await ap.enable()
    return st, db, ap, msgs


async def test_starts_only_when_health_and_paper_agree():
    st, db, ap, msgs = await setup(health=5.0)
    db.rows = [paper(3.0, i) for i in range(9)]
    assert "ينتظر" in await ap.evaluate() and not await st.live_enabled()        # 9 < 10 trades
    db.rows.append(paper(3.0, 30))
    db.rows += [paper(-50.0, 5, tier="low_liq")]                                  # experiment ignored
    assert "بدأ" in await ap.evaluate() and await st.live_enabled() and msgs


async def test_weak_health_blocks_start_and_stops_live():
    st, db, ap, msgs = await setup(health=1.5)
    db.rows = [paper(3.0, i) for i in range(10)]
    assert "الصحة" in await ap.evaluate() and not await st.live_enabled()        # 1.5 < 2
    await st.set_health({"value": 6.0, "n": 40, "prev": 1.0, "ts": time.time()})
    await ap.evaluate()
    assert await st.live_enabled()
    await st.set_health({"value": 0.4, "n": 40, "prev": 6.0, "ts": time.time()})
    assert "متوقف" in await ap.evaluate() and not await st.live_enabled()        # stops at once
    await st.set_health({"value": 6.0, "n": 40, "prev": 0.4, "ts": time.time()})
    assert "مهلة" in await ap.evaluate() and not await st.live_enabled()         # 12h hysteresis


async def test_losing_paper_streak_stops_live():
    st, db, ap, msgs = await setup(health=6.0)
    db.rows = [paper(4.0, 100 + i) for i in range(10)]
    await ap.evaluate()
    assert await st.live_enabled()
    db.rows += [paper(-8.0, i) for i in range(8)]
    assert "سالب" in await ap.evaluate() and not await st.live_enabled()


async def test_kill_switch_needs_the_user():
    st, db, ap, msgs = await setup(health=6.0)
    db.rows = [paper(4.0, 100 + i) for i in range(10)]
    await ap.evaluate()
    db.rows += [{"mode": "live", "pnl_usd": 5.0, "return_pct": 25, "exit_reason": "الهدف 1",
                 "closed_at": datetime.now(timezone.utc) + timedelta(seconds=1)},
                {"mode": "live", "pnl_usd": -16.0, "return_pct": -80, "exit_reason": "وقف خسارة",
                 "closed_at": datetime.now(timezone.utc) + timedelta(seconds=2)}]
    assert "طوارئ" in await ap.evaluate()
    assert not await st.live_enabled() and not await ap.is_auto()
    assert await ap.evaluate() == ""                                              # stays off


async def test_manual_mode_is_untouched():
    st = StateManager(client=fr.FakeRedis(decode_responses=True))
    ap = Autopilot(st, DB(), settings)
    await st.set_live_enabled(True)
    assert await ap.evaluate() == "" and await st.live_enabled()
