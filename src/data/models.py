from dataclasses import asdict, dataclass
from typing import NamedTuple


class Candle(NamedTuple):
    ts: int      # unix seconds, start of interval
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class PoolInfo:
    pool_address: str
    mint: str
    symbol: str
    name: str
    quote_symbol: str
    dex: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    fdv_usd: float
    age_days: float
    tx_24h: int
    price_change_24h: float

    def to_dict(self) -> dict:
        return asdict(self)
