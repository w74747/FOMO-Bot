"""Walk-forward back-test of the wave strategy on hourly candles.

At every bar the wave statistics are computed ONLY from past candles (no look-ahead),
re-estimated every `refit_every` bars. Fills are conservative: if a bar touches both
stop and target, the stop is assumed to hit first."""
from dataclasses import asdict, dataclass, field

from src.analysis.waves import score, wave_stats, zigzag
from src.data.models import Candle
from src.trading.strategy import (StrategyParams, build_plan, levels, side_cost, time_stop_hours, trail_pct,
                                  trailing_stop)


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


def bt_kwargs(settings) -> dict:
    """Back-test options matching live execution (empty = the old optimistic model)."""
    if not getattr(settings, "BT_REALISTIC", False):
        return {}
    kw = {"bounce_pct": settings.BOUNCE_CONFIRM_PCT, "entry_slip": settings.BT_ENTRY_SLIP, "body_only": True}
    if getattr(settings, "ENTRY_ON_CLOSE", False):
        kw["close_entry"] = True
    return kw


def _bounce_entry(bar, lv, bounce, slip, armed_low):
    """Live entry on hourly bars. Returns (fill, low_after_fill) when bought, ("cancel", None)
    when the stop broke first, or (None, lowest_price_so_far) while still waiting.
    The order of high and low inside a bar is unknown, so ambiguous bars go against us."""
    if armed_low is None:
        if bar.l > lv["entry"]:
            return None, None
        if bar.l <= lv["stop"]:
            return "cancel", None
        low = bar.l
        if bar.c >= low * (1 + bounce):                  # low first, then the bounce
            return low * (1 + bounce) * (1 + slip), None
        return None, low
    trigger = armed_low * (1 + bounce)
    if bar.l < armed_low:
        if bar.h >= trigger:                             # bounce off the old low, then a new low
            return trigger * (1 + slip), bar.l
        if bar.l <= lv["stop"]:
            return "cancel", None
        if bar.c >= bar.l * (1 + bounce):
            return bar.l * (1 + bounce) * (1 + slip), None
        return None, bar.l
    if bar.h >= trigger:
        return max(trigger, bar.o) * (1 + slip), None    # a gap above the trigger fills at the open
    return None, armed_low


def _close_entry(bar, lv, bounce, slip, armed_low, max_above=1.05):
    """Entry decided only at the hourly close, from the same candle live uses (ENTRY_ON_CLOSE).
    Returns (fill, armed_low): fill is a price (buy now at the close + slip), "cancel" (a wick
    broke the stop: the wave is broken), "missed" (closed too far above the entry zone) or
    None (armed_low = lowest close since the zone was touched, None = not armed)."""
    if armed_low is None:
        if bar.c > lv["entry"]:
            return None, None
        if bar.l <= lv["stop"]:
            return "cancel", None
        return None, bar.c
    if bar.l <= lv["stop"]:
        return "cancel", None
    if bar.c >= armed_low * (1 + bounce):
        if bar.c > lv["entry"] * max_above:
            return "missed", None
        return bar.c * (1 + slip), None
    return None, min(armed_low, bar.c)


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
    trade_log: list | None = None,
    bounce_pct: float | None = None,
    entry_slip: float = 0.0,
    body_only: bool = False,
    trade_detail: list | None = None,
    level_log: dict | None = None,
    trail_only: float | None = None,
    cooldowns: dict | None = None,
    skip_gates: tuple[str, ...] = (),
    close_entry: bool = False,
    open_log: dict | None = None,
) -> BacktestResult:
    """`bounce_pct` (optional) models the live entry: after the price touches the entry level the
    bot waits for a bounce of `bounce_pct` off the lowest price since, then buys `entry_slip`
    higher (polling delay). Hourly bars hide the order of high and low, so ambiguous bars are
    resolved against us. None = the original "buy at the entry level" model.

    `body_only`: events in our favour (touching the entry zone, the bounce low, target 1, the
    trailing peak) only count inside the candle body (open..close); wicks still hit stops.
    A wick is often one trade on one pool that the aggregated live price never shows.

    `skip_gates` (research only): wave-score rejection reasons starting with these prefixes
    (e.g. "downtrend", "lower lows") are ignored, to measure what a filter costs or saves.
    Live never uses it; the default () is the live gate.

    `close_entry` (with bounce_pct): the entry is decided only at each hourly close (see
    _close_entry), exactly what live does with ENTRY_ON_CLOSE, so live == back-test by
    construction. `open_log` receives the position still open at the end (entry_ts, fill...).

    `stats_cache` (optional) memoises wave stats per refit bar; it is only valid for one
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
    armed_low = None   # bounce model: lowest price since the entry zone was touched
    cool_until = 0     # live-style rest after a losing exit (cooldowns option)
    stop_times: list[float] = []

    def rest_after(ts: float, ret: float, reason: str) -> float:
        """Live rules: a stop-loss or losing time stop rests the coin; repeated stop-losses ban it."""
        if not cooldowns or ret >= 0 or not reason.startswith(("stop", "time")):
            return 0
        hours = cooldowns.get("loss_h", 12)
        if reason.startswith("stop"):
            stop_times.append(ts)
            recent = sum(1 for t in stop_times if ts - t <= 7 * 86400)
            if recent >= cooldowns.get("long_limit", 99):
                hours = cooldowns.get("long_h", hours)
            elif recent >= cooldowns.get("strike_limit", 99):
                hours = cooldowns.get("strike_h", hours)
        return ts + hours * 3600

    for i in range(warmup, len(candles)):
        bar = candles[i]
        fb = Candle(bar.ts, bar.o, max(bar.o, bar.c), min(bar.o, bar.c), bar.c, bar.v) if body_only else bar
        if (i - warmup) % refit_every == 0 or stats is None:
            if stats_cache is not None and i in stats_cache:
                stats = stats_cache[i]
            else:
                stats = wave_stats(candles[:i], zz_pct)
                if stats_cache is not None:
                    stats_cache[i] = stats
            # same gate as live: the coin must still pass the wave score filters at this point in time
            gate = [r for r in score(stats, liquidity_usd, params.max_cycle_hours)[1]
                    if not r.startswith(skip_gates)] if stats else ["no stats"]
            tradeable = bool(stats) and not gate and build_plan(
                stats.last_high, stats.median_drop, stats.median_rise, stats.avg_cycle_hours,
                liquidity_usd, position_usd, params, tx_fee_usd) is not None
            if stats and pos is None:
                ref_high = stats.last_high
                blocked_until_new_high = False
        if not stats or stats.n_cycles < 3 or stats.median_drop <= 0 or stats.median_rise <= 0:
            continue

        if pos is None:
            if bar.ts < cool_until:
                armed_low = None
                continue
            if ref_high is None:
                ref_high = stats.last_high
            if bar.h > ref_high:
                ref_high = bar.h
                blocked_until_new_high = False
            if level_log is not None:
                lv0 = levels(ref_high, stats.median_drop, stats.median_rise, params)
                level_log[bar.ts] = {"entry": lv0["entry"], "stop": lv0["stop"], "ref_high": ref_high,
                                     "tradeable": tradeable, "blocked": blocked_until_new_high,
                                     "armed": armed_low is not None, "low": bar.l, "body_low": fb.l,
                                     "median_drop": stats.median_drop, "median_rise": stats.median_rise,
                                     "avg_cycle_hours": stats.avg_cycle_hours}
            if blocked_until_new_high or not tradeable:
                continue
            lv = levels(ref_high, stats.median_drop, stats.median_rise, params)
            fill = None
            same_bar_low = None          # a low that happened AFTER the fill inside this bar
            if bounce_pct is None:
                if fb.l <= lv["entry"]:
                    fill = min(lv["entry"], bar.o)
                    if fill <= lv["stop"]:
                        blocked_until_new_high = True
                        continue
                    if body_only and bar.l < fb.l:
                        same_bar_low = bar.l         # the wick below came after the fill (against us)
            elif close_entry:
                if armed_low is None and (bar.c <= lv["stop"] or bar.c < ref_high * (1 - 2 * stats.median_drop)):
                    continue      # closed below the stop / far below an old high: not a wave (live: stale_level)
                fill, armed_low = _close_entry(bar, lv, bounce_pct, entry_slip, armed_low)
                if fill == "cancel":
                    blocked_until_new_high = True
                    continue
                if fill is None or fill == "missed":
                    continue
            else:
                was_armed = armed_low is not None
                if not was_armed and (bar.o <= lv["stop"] or bar.o < ref_high * (1 - 2 * stats.median_drop)):
                    continue      # already below the stop / far below an old high: not a wave (live: stale_level)
                fill, same_bar_low = _bounce_entry(fb, lv, bounce_pct, entry_slip, armed_low)
                if body_only and fill != "cancel" and (was_armed or fill is not None or same_bar_low is not None) \
                        and bar.l <= lv["stop"]:
                    if fill is None:
                        fill = "cancel"              # the wick broke the stop while we waited
                    else:
                        same_bar_low = bar.l         # bought, then the wick hit the stop
                if fill == "cancel":                     # stop broken before any bounce
                    armed_low, blocked_until_new_high = None, True
                    continue
                if fill is None:
                    armed_low = same_bar_low             # still armed; lowest price so far
                    continue
                armed_low = None
            if fill is None:
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
                "lv_entry": lv["entry"], "ref_high": ref_high,
                "time_stop": int(time_stop_hours(stats.avg_cycle_hours, params.max_cycle_hours,
                                                 params.time_stop_factor)),
            }
            if same_bar_low is not None and same_bar_low <= pos["stop"]:
                # bought on the bounce, then the same hour fell through the stop
                returns.append(-fee + (pos["stop"] - fill) / fill - 2 * cost_side - fee)
                if trade_log is not None:
                    trade_log.append((bar.ts, returns[-1]))
                if trade_detail is not None:
                    trade_detail.append({"entry_ts": bar.ts, "exit_ts": bar.ts, "fill": fill, "ret": returns[-1],
                                         "reason": "stop (same hour)", "lv_entry": lv["entry"], "ref_high": ref_high,
                                         "stop": pos["stop"], "tp1": pos["tp1"]})
                cool_until = max(cool_until, rest_after(bar.ts, returns[-1], "stop"))
                pos, ref_high, blocked_until_new_high = None, bar.h, False
            continue

        # manage open position (stop is checked BEFORE using this bar's high: conservative)
        e = pos["entry"]
        closed = False
        reason = None
        if trail_only is not None:
            # single moving exit: no target; the stop follows the highest price by `trail_only`
            # (never below the initial stop); whole position sold when it is hit
            if bar.l <= pos["stop"]:
                px = min(pos["stop"], bar.o)
                pos["realized"] += (px - e) / e - 2 * cost_side - fee
                closed, reason = True, ("trailing" if pos["stop"] > e else "stop")
            elif pos["stop"] < e and i - pos["opened"] >= pos["time_stop"]:
                pos["realized"] += (bar.c - e) / e - 2 * cost_side - fee
                closed, reason = True, "time"
            else:
                pos["peak"] = max(pos["peak"], fb.h)
                pos["stop"] = max(pos["stop"], pos["peak"] * (1 - trail_only))
        elif bar.l <= pos["stop"]:
            reason = "trailing" if pos["half"] else "stop"
            px = min(pos["stop"], bar.o)
            part = 0.5 if pos["half"] else 1.0
            pos["realized"] += part * ((px - e) / e - 2 * cost_side) - fee
            closed = True
        elif not pos["half"]:
            if fb.h >= pos["tp1"]:
                pos["realized"] += 0.5 * ((pos["tp1"] - e) / e - 2 * cost_side) - fee
                pos["half"] = True
                pos["peak"] = pos["tp1"]
                pos["stop"] = trailing_stop(max(pos["stop"], e), e, pos["peak"], pos["tp2"], pos["trail"])
            elif i - pos["opened"] >= pos["time_stop"]:
                pos["realized"] += (bar.c - e) / e - 2 * cost_side - fee
                closed, reason = True, "time"
        else:
            pos["peak"] = max(pos["peak"], fb.h)
            pos["stop"] = trailing_stop(pos["stop"], e, pos["peak"], pos["tp2"], pos["trail"])
        if closed:
            returns.append(pos["realized"])
            if trade_log is not None:
                trade_log.append((candles[pos["opened"]].ts, pos["realized"]))
            if trade_detail is not None:
                trade_detail.append({"entry_ts": candles[pos["opened"]].ts, "exit_ts": bar.ts, "fill": e,
                                     "ret": pos["realized"], "reason": reason + ("+tp1" if pos["half"] and reason != "trailing" else ""),
                                     "lv_entry": pos["lv_entry"], "ref_high": pos["ref_high"],
                                     "stop": pos["stop"], "tp1": pos["tp1"]})
            cool_until = max(cool_until, rest_after(bar.ts, pos["realized"], reason))
            pos = None
            ref_high = bar.h
            blocked_until_new_high = False

    if open_log is not None and pos is not None:
        open_log.update({"entry_ts": candles[pos["opened"]].ts, "fill": pos["entry"], "stop": pos["stop"],
                         "tp1": pos["tp1"], "tp2": pos["tp2"], "lv_entry": pos["lv_entry"],
                         "ref_high": pos["ref_high"], "half": pos["half"]})
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
