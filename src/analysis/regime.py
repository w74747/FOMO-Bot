"""Does the wider small-cap market decide whether a wave trade works?

Builds a market index from every stored coin (median 24h / 6h return and the share of
coins that are up), back-tests the current strategy on all stored candles while logging
each trade's entry time, and groups trades by the market state just before entry
(only the previous, fully closed hour is used: no look-ahead).

Pure and CPU-heavy: run it in a separate process."""
import bisect
import statistics
from dataclasses import fields

from src.analysis.backtest import backtest
from src.analysis.optimizer import split_of
from src.analysis.waves import clean_candles
from src.data.models import Candle
from src.trading.strategy import StrategyParams

HOUR = 3600
MKT_BUCKETS = [(-1e9, -10), (-10, -5), (-5, 0), (0, 5), (5, 1e9)]
BREADTH_BUCKETS = [(0, 30), (30, 45), (45, 60), (60, 101)]
SKIP_LEVELS = [-15, -10, -7, -5, -3, 0]


def market_index(series: dict[str, list[Candle]], min_coins: int = 8) -> dict[int, dict]:
    closes = {p: {c.ts: c.c for c in cs if c.c > 0} for p, cs in series.items()}
    stamps = sorted({ts for d in closes.values() for ts in d})
    out = {}
    for ts in stamps:
        r24, r6 = [], []
        for d in closes.values():
            now = d.get(ts)
            if not now:
                continue
            a, b = d.get(ts - 24 * HOUR), d.get(ts - 6 * HOUR)
            if a:
                r24.append(now / a - 1)
            if b:
                r6.append(now / b - 1)
        if len(r24) >= min_coins:
            out[ts] = {"m24": statistics.median(r24) * 100,
                       "m6": statistics.median(r6) * 100 if r6 else 0.0,
                       "breadth": sum(1 for r in r24 if r > 0) / len(r24) * 100,
                       "coins": len(r24)}
    return out


_keys_cache: dict[int, list[int]] = {}


def regime_at(index: dict[int, dict], ts: float) -> dict | None:
    """State of the market at the last bar that had fully closed by `ts` (bar start + 1h <= ts),
    at most 3 hours old."""
    keys = _keys_cache.get(id(index))
    if keys is None or len(keys) != len(index):
        keys = sorted(index)
        _keys_cache.clear()
        _keys_cache[id(index)] = keys
    i = bisect.bisect_right(keys, ts - HOUR) - 1
    if i < 0 or ts - HOUR - keys[i] > 3 * HOUR:
        return None
    return index[keys[i]]


def _stats(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"n": 0}
    return {"n": n, "win": sum(1 for r in rets if r > 0) / n * 100,
            "avg": statistics.mean(rets), "total": sum(rets)}


def _bucket(trades, key, buckets):
    return [((lo, hi), _stats([t["ret"] for t in trades if t[key] is not None and lo <= t[key] < hi]))
            for lo, hi in buckets]


def analyze(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float,
            real_trades: list[dict] | None = None) -> dict:
    """datasets: [(pool, [candle tuples], liquidity)]. real_trades: [{opened_at, ret, mode}]
    with ret in %. Returns plain dicts (picklable)."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = {pool: clean_candles([Candle(*c) for c in cs]) for pool, cs, _ in datasets}
    index = market_index(series)

    trades = []
    for pool, _, liq in datasets:
        log: list = []
        backtest(series[pool], p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd, trade_log=log)
        for ts, ret in log:
            rg = regime_at(index, ts)
            trades.append({"pool": pool, "side": split_of(pool), "ts": ts, "ret": ret * 100,
                           "m24": rg["m24"] if rg else None, "m6": rg["m6"] if rg else None,
                           "breadth": rg["breadth"] if rg else None})
    with_rg = [t for t in trades if t["m24"] is not None]

    def skip_table(rows):
        base = _stats([t["ret"] for t in rows])
        out = []
        for lvl in SKIP_LEVELS:
            kept = [t["ret"] for t in rows if t["m24"] >= lvl]
            out.append((lvl, _stats(kept)))
        return base, out

    real = []
    for r in real_trades or []:
        rg = regime_at(index, r["opened_at"])
        if rg:
            real.append({**r, "m24": rg["m24"], "m6": rg["m6"], "breadth": rg["breadth"]})

    last_ts = max(index) if index else None
    return {
        "coins": len(series), "hours": len(index),
        "trades": len(trades), "with_regime": len(with_rg),
        "by_m24": _bucket(with_rg, "m24", MKT_BUCKETS),
        "by_m6": _bucket(with_rg, "m6", MKT_BUCKETS),
        "by_breadth": _bucket(with_rg, "breadth", BREADTH_BUCKETS),
        "skip_all": skip_table(with_rg),
        "skip_a": skip_table([t for t in with_rg if t["side"] == "A"]),
        "skip_b": skip_table([t for t in with_rg if t["side"] == "B"]),
        "real_by_m24": _bucket(real, "m24", MKT_BUCKETS),
        "real": [{k: r.get(k) for k in ("symbol", "mode", "ret", "m24", "breadth")} for r in real],
        "now": index[last_ts] if last_ts else None,
    }


def _fmt_range(lo, hi, unit="%"):
    if lo <= -1e8:
        return f"< {hi:+.0f}{unit}"
    if hi >= 100 and unit == "%" and lo >= 60:
        return f"≥ {lo:.0f}{unit}"
    if hi >= 1e8:
        return f"≥ {lo:+.0f}{unit}"
    return f"{lo:+.0f}..{hi:+.0f}{unit}"


def _row(label, st):
    if not st["n"]:
        return f"{label}: —"
    return f"{label}: {st['n']} صفقة · فوز {st['win']:.0f}% · متوسط {st['avg']:+.2f}% · مجموع {st['total']:+.0f}%"


def format_report(r: dict) -> str:
    L = ["🌡 <b>أثر حالة السوق على الاستراتيجية</b>",
         f"{r['coins']} عملة · {r['hours']} ساعة من المؤشر · {r['trades']} صفقة اختبار ({r['with_regime']} بمؤشر)"]
    if r.get("now"):
        n = r["now"]
        L.append(f"السوق الآن: وسيط 24س {n['m24']:+.1f}% · 6س {n['m6']:+.1f}% · صاعدة {n['breadth']:.0f}% من {n['coins']} عملة")
    L += ["", "<b>حسب وسيط تغيّر السوق خلال 24 ساعة قبل الدخول:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_m24"]]
    L += ["", "<b>حسب نسبة العملات الصاعدة:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_breadth"]]
    L += ["", "<b>حسب وسيط 6 ساعات:</b>"]
    L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["by_m6"]]
    for key, name in (("skip_all", "الكل"), ("skip_a", "النصف A"), ("skip_b", "النصف B")):
        base, rows = r[key]
        if not base["n"]:
            continue
        L += ["", f"<b>لو أوقفنا الشراء حين السوق أضعف من حد ({name}):</b>", _row("• بدون فلتر", base)]
        L += [_row(f"• حد {lvl:+d}%", st) for lvl, st in rows]
    if r["real"]:
        L += ["", "<b>صفقاتنا الفعلية (ورقي + حقيقي):</b>"]
        L += [_row(f"• {_fmt_range(lo, hi)}", st) for (lo, hi), st in r["real_by_m24"]]
    return "\n".join(L)


ENTRY_MODELS = [
    ("الاختبار الحالي (شراء عند نقطة الدخول)", None, 0.0, False),
    ("ارتداد 2% بلا تأخير", 0.02, 0.0, False),
    ("ارتداد 2% + تأخير 0.7% (المقاس في GTA)", 0.02, 0.007, False),
    ("ارتداد 2% + تأخير 2%", 0.02, 0.02, False),
    ("ارتداد 1% + تأخير 0.7%", 0.01, 0.007, False),
    ("ارتداد 3% + تأخير 0.7%", 0.03, 0.007, False),
    ("الحالي بلا ذيول (جسم الشمعة فقط لصالحنا)", None, 0.0, True),
    ("ارتداد 2% + تأخير 0.7% بلا ذيول", 0.02, 0.007, True),
    ("ارتداد 1% + تأخير 0.7% بلا ذيول", 0.01, 0.007, True),
]


def entry_models(datasets: list[tuple], params: dict, position_usd: float, fee_usd: float) -> list[dict]:
    """Same coins, same params; only the entry model changes. Shows how much of the back-test's
    edge survives the real 'wait for the bounce' entry."""
    known = {f.name for f in fields(StrategyParams)}
    p = StrategyParams(**{k: v for k, v in params.items() if k in known})
    series = [(pool, clean_candles([Candle(*c) for c in cs]), liq) for pool, cs, liq in datasets]
    out = []
    for name, bounce, slip, body in ENTRY_MODELS:
        per = {"A": [], "B": []}
        for pool, cs, liq in series:
            r = backtest(cs, p.zigzag_pct, p, liq, position_usd, tx_fee_usd=fee_usd,
                         bounce_pct=bounce, entry_slip=slip, body_only=body)
            per[split_of(pool)] += [x * 100 for x in r.returns]
        out.append({"name": name, "all": _stats(per["A"] + per["B"]),
                    "a": _stats(per["A"]), "b": _stats(per["B"])})
    return out


def format_entry_models(rows: list[dict], real: list[dict] | None = None) -> str:
    L = ["🎯 <b>واقعية الاختبار: نموذج الدخول</b>"]
    for r in rows:
        L += [f"<b>{r['name']}</b>", _row("• الكل", r["all"]),
              _row("• A", r["a"]) + " | " + _row("B", r["b"]).replace("B: ", "B ")]
    if real:
        L += ["", _row("📍 صفقاتنا الفعلية", _stats([t["ret"] for t in real]))]
    return "\n".join(L)
