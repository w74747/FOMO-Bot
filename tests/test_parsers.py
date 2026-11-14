from src.data.geckoterminal import parse_ohlcv, parse_pools
from src.data.safety import TOKEN_2022, evaluate_mint_account

POOLS = {
    "data": [{
        "id": "solana_POOL1", "type": "pool",
        "attributes": {
            "address": "POOL1", "name": "WIF / SOL", "base_token_price_usd": "1.23",
            "reserve_in_usd": "250000.5", "fdv_usd": "1000000", "pool_created_at": "2024-01-01T00:00:00Z",
            "volume_usd": {"h24": "500000"}, "transactions": {"h24": {"buys": 100, "sells": 80}},
            "price_change_percentage": {"h24": "-12.5"},
        },
        "relationships": {
            "base_token": {"data": {"id": "solana_MINTWIF", "type": "token"}},
            "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}},
            "dex": {"data": {"id": "raydium", "type": "dex"}},
        },
    }],
    "included": [
        {"id": "solana_MINTWIF", "type": "token", "attributes": {"address": "MINTWIF", "symbol": "WIF", "name": "dogwifhat"}},
        {"id": "solana_So11111111111111111111111111111111111111112", "type": "token",
         "attributes": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL", "name": "Wrapped SOL"}},
    ],
}


def test_parse_pools():
    (p,) = parse_pools(POOLS)
    assert p.mint == "MINTWIF" and p.symbol == "WIF" and p.quote_symbol == "SOL"
    assert p.liquidity_usd == 250000.5 and p.volume_24h_usd == 500000
    assert p.tx_24h == 180 and p.dex == "raydium" and p.age_days > 300


def test_parse_ohlcv_sorted_ascending():
    payload = {"data": {"attributes": {"ohlcv_list": [[200, 1, 2, 0.5, 1.5, 10], [100, 1, 1.1, 0.9, 1, 5], [300, 0, 0, 0, 0, 0]]}}}
    candles = parse_ohlcv(payload)
    assert [c.ts for c in candles] == [100, 200]


def test_mint_safety():
    good = {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None, "decimals": 6}}}}
    assert evaluate_mint_account(good)["ok"]
    bad = {"owner": TOKEN_2022, "data": {"parsed": {"info": {
        "mintAuthority": "X", "freezeAuthority": None,
        "extensions": [{"extension": "permanentDelegate"},
                       {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 0}}}]}}}}
    v = evaluate_mint_account(bad)
    assert not v["ok"] and len(v["reasons"]) == 2
