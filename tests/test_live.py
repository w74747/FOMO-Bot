"""Live broker against a simulated chain + Jupiter (real signing, fake network)."""
import base64
import json

import fakeredis.aioredis
import pytest
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

import src.trading.wallet as wallet_mod
from src.config import settings
from src.core.state import StateManager
from src.trading.broker import RiskBlocked
from src.trading.live import SOL_MINT, LiveBroker
from src.trading.wallet import SolanaWallet, load_keypair, sign_versioned

MINT = "MemeMint111111111111111111111111111111111111"
RENT = 2_039_280
FEE = 20_000


def unsigned_tx(payer: Pubkey, extra_signer: Keypair | None = None) -> str:
    accounts = [] if extra_signer is None else [extra_signer.pubkey()]
    ix = Instruction(Pubkey.default(), b"\x01", [])
    if extra_signer:
        from solders.instruction import AccountMeta
        ix = Instruction(Pubkey.default(), b"\x01", [AccountMeta(extra_signer.pubkey(), True, False)])
    msg = MessageV0.try_compile(payer, [ix], [], Hash.default())
    n = msg.header.num_required_signatures
    from solders.signature import Signature
    sigs = [Signature.default()] * n
    if extra_signer:
        from solders.message import to_bytes_versioned
        keys = list(msg.account_keys)[:n]
        sigs[keys.index(extra_signer.pubkey())] = extra_signer.sign_message(to_bytes_versioned(msg))
    _ = accounts
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()


class FakeChain:
    """Simulates Jupiter /order + /execute and the RPC calls the wallet uses."""

    def __init__(self, kp: Keypair, lamports: int, price_tokens_per_sol: float = 1e9 * 0.5):
        self.kp = kp
        self.lamports = lamports
        self.tokens = None          # None = no token account; int = account with balance
        self.ata = str(Pubkey.new_unique())
        self.rate = price_tokens_per_sol  # raw tokens per lamport-SOL... tokens per 1 SOL (raw)
        self.order = None
        self.fail_execute = False
        self.executed = []

    async def get_json(self, url, params=None, **kw):
        assert url.endswith("/order") and params["taker"] == str(self.kp.pubkey())
        self.order = dict(params)
        amt = int(params["amount"])
        out = int(amt / 1e9 * self.rate) if params["inputMint"] == SOL_MINT else int(amt / self.rate * 1e9)
        return {"transaction": unsigned_tx(self.kp.pubkey()), "requestId": "req1", "outAmount": str(out),
                "priceImpact": -0.3}

    async def post_json(self, url, body, **kw):
        if url.endswith("/execute"):
            tx = VersionedTransaction.from_bytes(base64.b64decode(body["signedTransaction"]))
            assert tx.verify_with_results() == [True]          # our signature is valid
            if self.fail_execute:
                return {"status": "Failed", "code": -1, "error": "slippage"}
            o, amt = self.order, int(self.order["amount"])
            if o["inputMint"] == SOL_MINT:
                out = int(amt / 1e9 * self.rate)
                self.lamports -= amt + FEE + (RENT if self.tokens is None else 0)
                self.tokens = (self.tokens or 0) + out
            else:
                out = int(amt / self.rate * 1e9)
                self.tokens -= amt
                self.lamports += out - FEE
            self.executed.append(o)
            return {"status": "Success", "signature": f"sig{len(self.executed)}",
                    "inputAmountResult": str(amt), "outputAmountResult": str(out)}
        # RPC
        m, p = body["method"], body["params"]
        if m == "getBalance":
            if p[0] != str(self.kp.pubkey()):       # a token account: holds its rent
                return {"result": {"value": RENT}}
            return {"result": {"value": self.lamports}}
        if m == "getTokenAccountsByOwner":
            if self.tokens is None:
                return {"result": {"value": []}}
            return {"result": {"value": [{"pubkey": self.ata, "account": {
                "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.tokens), "decimals": 6}}}}}}]}}
        if m == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": str(Hash.default())}}}
        if m == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
            assert tx.verify_with_results() == [True]
            assert self.tokens == 0
            self.tokens = None
            self.lamports += RENT - 5_000
            return {"result": "closesig"}
        if m == "getSignatureStatuses":
            return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}
        raise AssertionError(m)


class DB:
    def __init__(self):
        self.trades, self.closed = [], []

    async def log_trade(self, *a, **k):
        self.trades.append(a)

    async def save_closed_position(self, pos, reason):
        self.closed.append((pos, reason))


@pytest.fixture
async def live(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(wallet_mod.asyncio, "sleep", no_sleep)
    kp = Keypair()
    chain = FakeChain(kp, lamports=int(1.24563e9))
    w = SolanaWallet(chain, "http://rpc", str(kp))
    state = StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    b = LiveBroker(state, DB(), settings, chain, w, "key")
    b.sol_usd = 100.0
    return b, chain, state


ITEM = {"mint": MINT, "symbol": "MEME", "median_drop": 0.25, "median_rise": 0.35, "stop_pct": 0.12,
        "time_stop_hours": 20, "liquidity": 300_000, "bt_avg": 6.0}


def test_load_keypair_formats():
    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()


def test_partial_signing_keeps_other_signature():
    me, other = Keypair(), Keypair()
    signed = VersionedTransaction.from_bytes(base64.b64decode(sign_versioned(unsigned_tx(me.pubkey(), other), me)))
    assert signed.verify_with_results() == [True, True]
    with pytest.raises(ValueError):
        sign_versioned(unsigned_tx(other.pubkey()), me)


async def test_live_round_trip_accounts_fees_and_reclaims_rent(live):
    b, chain, state = live
    start = chain.lamports
    pos = await b.buy(ITEM, 0.002, 300_000)
    assert chain.order["slippageBps"] == str(settings.MAX_SLIPPAGE_BPS)
    # cost includes swap + fee + rent: 10$ at 100$/SOL = 0.1 SOL
    assert abs(pos["cost_sol"] - (0.1 + (FEE + RENT) / 1e9)) < 1e-9
    assert pos["decimals"] == 6 and pos["qty"] == chain.tokens
    assert abs(pos["rent_sol"] - RENT / 1e9) < 1e-12
    # entry price excludes refundable rent: (0.1 SOL + fee) * 100$ / tokens
    tokens_ui = pos["qty"] / 1e6
    assert abs(pos["entry_price"] - (0.1 + FEE / 1e9) * 100 / tokens_ui) < 1e-12
    p2, pnl1, closed = await b.sell(MINT, 0.5, 0.0, "الهدف 1")
    assert not closed and chain.tokens == pos["qty"] - pos["qty"] // 2
    assert chain.order["slippageBps"] == str(settings.MAX_SLIPPAGE_BPS)
    p3, pnl2, closed = await b.sell(MINT, 1.0, 0.0, "وقف متحرك")
    assert closed and chain.tokens is None                      # account closed
    assert chain.order["slippageBps"] == str(settings.LIVE_EXIT_SLIPPAGE_BPS)
    total_cost = (start - chain.lamports) / 1e9                 # 3 swap fees + close fee only
    assert abs(total_cost - (3 * FEE + 5_000) / 1e9) < 1e-9     # rent came back
    assert abs(p3["realized_sol"] + total_cost) < 2e-9          # books match the wallet exactly
    assert await state.get_position(MINT, "live") is None
    assert len(b.db.trades) == 3


async def test_live_respects_reserve_and_limits(live):
    b, chain, state = live
    chain.lamports = int(0.12e9)            # 0.12 SOL: 0.1 for the trade would breach the 0.05 reserve
    with pytest.raises(RiskBlocked):
        await b.buy(ITEM, 0.002, 300_000)
    chain.lamports = int(1e9)
    await state.add_daily_pnl(-settings.LIVE_DAILY_LOSS_USD, "live")
    with pytest.raises(RiskBlocked):
        await b.buy(ITEM, 0.002, 300_000)
    assert chain.executed == []
    assert await state.get_daily_pnl("paper") == 0     # books are separate


async def test_failed_execute_does_not_open_position(live):
    b, chain, state = live
    chain.fail_execute = True
    with pytest.raises(RiskBlocked):
        await b.buy(ITEM, 0.002, 300_000)
    assert await state.get_positions("live") == {}


async def test_self_test(live):
    b, chain, state = live
    msg = await b.self_test(2.0)
    assert "sig1" in msg and "sig2" in msg and chain.tokens is None


async def test_monitor_runs_paper_and_live_side_by_side(live):
    from src.trading.broker import PaperBroker
    from src.trading.monitor import Monitor
    from src.trading.strategy import StrategyParams, levels

    b, chain, state = live
    msgs = []
    import time
    await state.set_health({"value": 6.0, "n": 80, "prev": 5.0, "ts": time.time()})   # healthy phase

    async def notify(t):
        msgs.append(t)

    class Jup:
        price = 1.0

        async def prices(self, mints):
            return {m: {"price": (100.0 if m == SOL_MINT else self.price), "liquidity": 500_000} for m in mints}

    jup = Jup()
    paper = PaperBroker(state, DB(), settings)
    mon = Monitor(state, jup, paper, settings, notify, live_broker=b)
    lv = levels(1.0, 0.25, 0.35, StrategyParams.from_settings(settings))
    await state.set_watch_item(MINT, {**ITEM, "score": 80, "avg_cycle_hours": 36, "reward_risk": 2,
                                      "shadow": {"ok": True, "why": "", "ts": __import__("time").time()},
                                      "status": "waiting", "local_min": None, **lv})

    async def step(p):
        jup.price = p
        await mon.tick()

    # live OFF: only paper buys
    await step(0.78)
    await step(0.80)
    assert await state.get_position(MINT, "paper") and not await state.get_positions("live")
    assert chain.executed == []
    await mon.close_all(book="paper")

    # live ON: both buy on the next signal
    await state.set_live_enabled(True)
    await state.set_watch_item(MINT, {**ITEM, "score": 80, "avg_cycle_hours": 36, "reward_risk": 2,
                                      "shadow": {"ok": True, "why": "", "ts": __import__("time").time()},
                                      "status": "waiting", "local_min": None, **lv})
    await step(0.78)
    await step(0.80)
    assert await state.get_position(MINT, "paper") and await state.get_position(MINT, "live")
    assert any("حقيقي" in m for m in msgs)
    buys = [m for m in msgs if "شراء" in m and "🟢" in m]
    assert buys and all("حقيقي" in m for m in buys[-1:]) and not any("ورقي" in m for m in msgs[-3:])
    n_before = len(msgs)

    # a failing live sell keeps the position and warns once; paper still exits
    chain.fail_execute = True
    live_pos = await state.get_position(MINT, "live")
    await step(live_pos["stop"] * 0.5)
    await step(live_pos["stop"] * 0.5)
    assert await state.get_position(MINT, "live") is not None
    assert await state.get_position(MINT, "paper") is None
    assert sum("فشل البيع" in m for m in msgs) == 1
    # paper exit happened silently: no paper sell message while live is on
    assert not any("بيع" in m and "حقيقي" not in m and "فشل" not in m for m in msgs[n_before:])
    chain.fail_execute = False
    await step(live_pos["stop"] * 0.5)                  # retry succeeds
    assert await state.get_position(MINT, "live") is None and chain.tokens is None


async def test_sell_is_not_blocked_by_noisy_price_impact(live):
    b, chain, state = live
    await b.buy(ITEM, 0.002, 300_000)
    orig = chain.get_json

    async def noisy(url, params=None, **kw):
        res = await orig(url, params, **kw)
        res["priceImpact"] = -11.55
        return res
    chain.get_json = noisy
    _, _, closed = await b.sell(MINT, 1.0, 0.0, "وقف خسارة")
    assert closed                                   # exit goes through
    with pytest.raises(RiskBlocked):
        await b.buy(ITEM, 0.002, 300_000)           # but a new entry on such a quote is refused


async def test_live_levels_follow_the_signal_not_the_fill(live):
    from src.trading.broker import PaperBroker
    from src.trading.strategy import StrategyParams
    b, chain, state = live
    chain.rate *= 0.98                      # live fill ~2% worse than the signal price
    signal = 1e-4 * 0.5 / 0.5               # any price; paper uses the same formula
    p = StrategyParams.from_settings(settings)
    live_pos = await b.buy(ITEM, signal, 300_000, p)
    paper = PaperBroker(StateManager(client=fakeredis.aioredis.FakeRedis(decode_responses=True)), DB(), settings)
    paper_pos = await paper.buy(ITEM, signal, 300_000, p)
    for k in ("stop", "tp1", "tp2"):
        assert abs(live_pos[k] / paper_pos[k] - 1) < 0.01   # same exit levels (fees differ slightly)
    assert live_pos["entry_price"] != live_pos["signal_ref"]   # PnL basis is the real fill
