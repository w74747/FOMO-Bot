"""Parameter optimiser.

Runs the walk-forward back-test for every parameter combination in GRID over every token
with stored candle history. Tokens are split into two fixed halves:
  * set A is used to pick the best parameters,
  * set B is never used for picking: it shows whether the winner also works on coins it
    has not "seen" (out-of-sample). A config that only shines on A is over-fitted.

CPU-heavy, pure functions: run it in a separate process so the bot keeps trading."""
import hashlib
import itertools
import math
import statistics
from dataclasses import asdict, dataclass, replace

from src.analysis.backtest import backtest
from src.data.models import Candle
from src.trading.strategy import StrategyParams

GRID = {
    "zigzag_pct": [0.08, 0.10, 0.14],
    "entry_drop_factor": [0.70, 0.85, 1.00],
    "tp1_factor": [0.50, 0.70],
    "tp2_factor": [0.85, 1.10],
    "stop_drop_factor": [0.35, 0.50, 0.70],
}
MIN_TRADES_A = 12
MIN_TOKENS_A = 5     # a config must work on several coins, not one lucky chart
MIN_TOKENS_B = 3


@dataclass
class SetStats:
    trades: int
    tokens_traded: int
    win_rate: float
    avg_pct: float        # expectancy per trade, %
    total_pct: float      # sum of trade returns, % (fixed size)
    worst_pct: float
    lcb_pct: float        # pessimistic expectancy (mean - std error), used for ranking


@dataclass
class ConfigResult:
    params: dict
    a: SetStats
    b: SetStats


def _set_stats(per_token: list[list[float]]) -> SetStats:
    rets = [r for tok in per_token for r in tok]
    n = len(rets)
    if n == 0:
        return SetStats(0, 0, 0.0, 0.0, 0.0, 0.0, -999.0)
    mean = sum(rets) / n
    sd = statistics.pstdev(rets) if n > 1 else abs(mean)
    return SetStats(
        trades=n,
        tokens_traded=sum(1 for t in per_token if t),
        win_rate=round(sum(1 for r in rets if r > 0) / n, 3),
        avg_pct=round(mean * 100, 2),
        total_pct=round(sum(rets) * 100, 1),
        worst_pct=round(min(rets) * 100, 1),
        lcb_pct=round((mean - sd / math.sqrt(n)) * 100, 2),
    )


def split_of(pool: str) -> str:
    return "A" if int(hashlib.sha1(pool.encode()).hexdigest(), 16) % 2 == 0 else "B"


def optimize(
    datasets: list[tuple[str, list[tuple], float]],
    base: dict,
    position_usd: float,
    grid: dict | None = None,
) -> dict:
    """datasets: [(pool, [candle tuples], liquidity_usd)]. base: current StrategyParams as dict.
    Returns a JSON-able report with the current config and the top configs."""
    grid = grid or GRID
    base_params = StrategyParams(**base)
    data = [(pool, [Candle(*c) for c in cs], liq, split_of(pool)) for pool, cs, liq in datasets]
    caches: dict[tuple[str, float], dict] = {}

    def evaluate(params: StrategyParams) -> ConfigResult:
        per = {"A": [], "B": []}
        for pool, candles, liq, side in data:
            cache = caches.setdefault((pool, params.zigzag_pct), {})
            r = backtest(candles, params.zigzag_pct, params, liq, position_usd, stats_cache=cache)
            per[side].append(r.returns)
        return ConfigResult(asdict(params), _set_stats(per["A"]), _set_stats(per["B"]))

    current = evaluate(base_params)
    keys = list(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        results.append(evaluate(replace(base_params, **dict(zip(keys, combo)))))

    eligible = [r for r in results if r.a.trades >= MIN_TRADES_A and r.a.tokens_traded >= MIN_TOKENS_A]
    eligible.sort(key=lambda r: r.a.lcb_pct, reverse=True)
    top = eligible[:5]
    # recommend the best-ranked config whose out-of-sample (B) evidence is broad enough
    recommend_idx = None
    for i, r in enumerate(top, 1):
        if (r.b.trades >= 15 and r.b.tokens_traded >= MIN_TOKENS_B and r.b.avg_pct > 0
                and r.b.avg_pct >= current.b.avg_pct):
            recommend_idx = i
            break
    return {
        "tokens": len(data),
        "tokens_a": sum(1 for d in data if d[3] == "A"),
        "tokens_b": sum(1 for d in data if d[3] == "B"),
        "configs_tested": len(results),
        "current": asdict(current),
        "top": [asdict(r) for r in top],
        "recommend": recommend_idx,
    }
