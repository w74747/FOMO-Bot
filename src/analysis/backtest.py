"""Walk-forward back-test of the wave strategy on hourly candles.

At every bar the wave statistics are computed ONLY from past candles (no look-ahead),
re-estimated every `refit_every` bars. Fills are conservative: if a bar touches both
stop and target, the stop is assumed to hit first."""
from dataclasses import asdict, dataclass, field

from src.analysis.waves import score, wave_stats, zigzag
from src.data.models import Candle
from src.trading.strategy import StrategyParams, build_plan, levels, side_cost, trail_pct, trailing_stop


@dataclass
class BacktestResult:
    trades: int
    wins: int
    win_rate: float
    total_return_pct: float     # sum of per-trade returns (fixed size per trade)
    avg_return_pct: float
    worst_trade_pct: float
    test_days: float
    returns: list[float] = field(default_factory=list)  # per-trade returns (fractions)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("returns", None)
        return d


def backtest(
    candles: list[Candle],
    zz_pct: float,
    params: StrategyParams,
    liquidity_usd: float,
    position_usd: float,
    warmup: int = 240,
    refit_every: int = 24,
    stats_cache: dict | None = None,
    tx_fee_usd: float = 0.0,
) -> BacktestResult:
    """`stats_cache` (optional) memoises wave stats per refit bar; it is only valid for one
    candle series and one zigzag pct, and lets the optimiser reuse work across parameter sets."""
    empty = BacktestResult(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if len(candles) < warmup + 48:
        return empty

    cost_side = side_cost(position_usd, liquidity_usd)
    fee = tx_fee_usd / max(position_usd, 1e-9)   # fixed network cost per transaction, as a fraction
    returns: list[float] = []
    stats = None
    pos = None  # dict with entry, stop, tp1, tp2, half_done, opened_idx, time_stop
    blocked_until_new_high = False
    ref_high = None
    tradeable = False

    for i in range(warmup, len(candles)):
        bar = candles[i]
        if (i - warmup) % refit_every == 0 or stats is None:
            if stats_cache is not None and i in stats_cache:
                stats = stats_cache[i]
            else:
                stats = wave_stats(candles[:i], zz_pct)
                if stats_cache is not None:
                    stats_cache[i] = stats
            # same gate as live: the coin must still pass the wave score filters at this point in time
            tradeable = bool(stats) and not score(stats, liquidity_usd)[1] and build_plan(
                stats.last_high, stats.median_drop, stats.median_rise, stats.avg_cycle_hours,
                liquidity_usd, position_usd, params, tx_fee_usd) is not None
            if stats and pos is None:
                ref_high = stats.last_high
                blocked_until_new_high = False
        if not stats or stats.n_cycles < 3 or stats.median_drop <= 0 or stats.median_rise <= 0:
            continue

        if pos is None:
            if ref_high is None:
                ref_high = stats.last_high
            if bar.h > ref_high:
                ref_high = bar.h
                blocked_until_new_high = False
            if blocked_until_new_high or not tradeable:
                continue
            lv = levels(ref_high, stats.median_drop, stats.median_rise, params)
            if bar.l <= lv["entry"]:
                fill = min(lv["entry"], bar.o)
                if fill <= lv["stop"]:
                    blocked_until_new_high = True
                    continue
                scale = fill / lv["entry"]
                pos = {
                    "entry": fill,
                    "stop": lv["stop"] * scale,
                    "tp1": lv["tp1"] * scale,
                    "tp2": lv["tp2"] * scale,
                    "half": False,
                    "realized": -fee,          # buy transaction
                    "peak": fill,
                    "trail": trail_pct(stats.median_drop, params),
                    "opened": i,
                    "time_stop": max(6, int(1.5 * stats.avg_cycle_hours)) if stats.avg_cycle_hours else 24,
                }
            continue

        # manage open position (stop is checked BEFORE using this bar's high: conservative)
        e = pos["entry"]
        closed = False
        if bar.l <= pos["stop"]:
            px = min(pos["stop"], bar.o)
            part = 0.5 if pos["half"] else 1.0
            pos["realized"] += part * ((px - e) / e - 2 * cost_side) - fee
            closed = True
        elif not pos["half"]:
            if bar.h >= pos["tp1"]:
                pos["realized"] += 0.5 * ((pos["tp1"] - e) / e - 2 * cost_side) - fee
                pos["half"] = True
                pos["peak"] = pos["tp1"]
                pos["stop"] = trailing_stop(max(pos["stop"], e), e, pos["peak"], pos["tp2"], pos["trail"])
            elif i - pos["opened"] >= pos["time_stop"]:
                pos["realized"] += (bar.c - e) / e - 2 * cost_side - fee
                closed = True
        else:
            pos["peak"] = max(pos["peak"], bar.h)
            pos["stop"] = trailing_stop(pos["stop"], e, pos["peak"], pos["tp2"], pos["trail"])
        if closed:
            returns.append(pos["realized"])
            pos = None
            ref_high = bar.h
            blocked_until_new_high = False

    if not returns:
        return BacktestResult(0, 0, 0.0, 0.0, 0.0, 0.0, round((candles[-1].ts - candles[warmup].ts) / 86400, 1))
    wins = sum(1 for r in returns if r > 0)
    return BacktestResult(
        trades=len(returns),
        wins=wins,
        win_rate=round(wins / len(returns), 3),
        total_return_pct=round(sum(returns) * 100, 2),
        avg_return_pct=round(sum(returns) / len(returns) * 100, 2),
        worst_trade_pct=round(min(returns) * 100, 2),
        test_days=round((candles[-1].ts - candles[warmup].ts) / 86400, 1),
        returns=returns,
    )


__all__ = ["backtest", "BacktestResult", "zigzag"]
