"""Turns a coin's wave statistics into concrete buy / target / stop levels."""
from dataclasses import asdict, dataclass

JUPITER_FEE = 0.001     # ~5-10 bps Jupiter + rounding
POOL_FEE = 0.0025       # typical DEX pool fee per side


@dataclass
class StrategyParams:
    entry_drop_factor: float = 0.85
    tp1_factor: float = 0.60
    tp2_factor: float = 0.85
    min_reward_risk: float = 1.3
    min_stop_pct: float = 0.07
    stop_drop_factor: float = 0.45

    @classmethod
    def from_settings(cls, s) -> "StrategyParams":
        return cls(
            entry_drop_factor=s.ENTRY_DROP_FACTOR,
            tp1_factor=s.TP1_FACTOR,
            tp2_factor=s.TP2_FACTOR,
            min_reward_risk=s.MIN_REWARD_RISK,
        )


@dataclass
class TradePlan:
    ref_high: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    stop_pct: float
    median_drop: float
    median_rise: float
    time_stop_hours: float
    cost_pct: float          # estimated round-trip cost (fees + price impact)
    reward_risk: float

    def to_dict(self) -> dict:
        return asdict(self)


def side_cost(position_usd: float, liquidity_usd: float) -> float:
    """Estimated cost of one swap as a fraction: fees + price impact.
    Constant-product impact of buying x into a pool with half-reserve R is ~ x / R."""
    impact = position_usd / max(liquidity_usd / 2, 1.0)
    return JUPITER_FEE + POOL_FEE + impact


def levels(ref_high: float, median_drop: float, median_rise: float, p: StrategyParams) -> dict:
    entry = ref_high * (1 - median_drop * p.entry_drop_factor)
    stop_pct = max(p.min_stop_pct, median_drop * p.stop_drop_factor)
    return {
        "ref_high": ref_high,
        "entry": entry,
        "stop": entry * (1 - stop_pct),
        "tp1": entry * (1 + median_rise * p.tp1_factor),
        "tp2": entry * (1 + median_rise * p.tp2_factor),
        "stop_pct": stop_pct,
    }


def build_plan(
    ref_high: float,
    median_drop: float,
    median_rise: float,
    avg_cycle_hours: float,
    liquidity_usd: float,
    position_usd: float,
    p: StrategyParams,
) -> TradePlan | None:
    if ref_high <= 0 or median_drop <= 0 or median_rise <= 0:
        return None
    lv = levels(ref_high, median_drop, median_rise, p)
    cost = 2 * side_cost(position_usd, liquidity_usd)
    e = lv["entry"]
    reward = 0.5 * (lv["tp1"] - e) / e + 0.5 * (lv["tp2"] - e) / e - cost
    risk = lv["stop_pct"] + cost
    rr = reward / risk if risk > 0 else 0.0
    if rr < p.min_reward_risk:
        return None
    return TradePlan(
        ref_high=ref_high,
        entry=e,
        stop=lv["stop"],
        tp1=lv["tp1"],
        tp2=lv["tp2"],
        stop_pct=lv["stop_pct"],
        median_drop=median_drop,
        median_rise=median_rise,
        time_stop_hours=max(6.0, 1.5 * avg_cycle_hours) if avg_cycle_hours else 24.0,
        cost_pct=cost,
        reward_risk=round(rr, 2),
    )
