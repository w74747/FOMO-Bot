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


def test_parse_jupiter_token():
    from src.data.jupiter import parse_token
    t = parse_token({
        "id": "MINTX", "symbol": "PEPE2", "name": "Pepe Two", "usdPrice": 0.0012, "liquidity": 250000,
        "fdv": 1200000, "holderCount": 4200, "organicScore": 61.5, "isVerified": False,
        "firstPool": {"id": "POOLX", "createdAt": "2025-01-01T00:00:00Z"},
        "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 22.5},
        "stats24h": {"priceChange": -14.2, "buyVolume": 300000, "sellVolume": 280000},
    })
    assert t["mint"] == "MINTX" and t["age_days"] > 300 and t["volume_24h"] == 580000
    assert abs(t["top_holders_pct"] - 0.225) < 1e-9 and t["holders"] == 4200


def test_prefilter_tokens():
    from src.analysis.scanner import prefilter_tokens
    from src.config import settings
    base = {"symbol": "MEME", "name": "", "price": 1, "liquidity": 200000, "fdv": 2e6, "age_days": 40,
            "volume_24h": 500000, "holders": 3000, "organic_score": 50, "top_holders_pct": 0.2,
            "mint_disabled": True, "freeze_disabled": True, "is_sus": False}
    toks = [
        {**base, "mint": "A"},
        {**base, "mint": "B", "age_days": 3},
        {**base, "mint": "C", "freeze_disabled": False},
        {**base, "mint": "D", "top_holders_pct": 0.8},
        {**base, "mint": "E", "symbol": "USDC"},
        {**base, "mint": "A"},  # duplicate across categories
    ]
    kept, rej = prefilter_tokens(toks, settings)
    assert [t["mint"] for t in kept] == ["A"]
    assert rej == {"too young": 1, "freeze authority active": 1, "concentrated holders": 1, "major/stable": 1}


def test_parse_multi_top_pools():
    from src.data.geckoterminal import parse_multi_top_pools
    payload = {"data": [], "included": [
        {"id": "solana_P1", "type": "pool", "attributes": {"address": "P1", "name": "MEME / SOL", "reserve_in_usd": "90000"},
         "relationships": {"base_token": {"data": {"id": "solana_MEMEMINT"}},
                           "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112"}}}},
        {"id": "solana_P2", "type": "pool", "attributes": {"address": "P2", "name": "MEME / USDC", "reserve_in_usd": "150000"},
         "relationships": {"base_token": {"data": {"id": "solana_MEMEMINT"}},
                           "quote_token": {"data": {"id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}}}},
    ]}
    out = parse_multi_top_pools(payload)
    assert {p.pool_address for p in out["MEMEMINT"]} == {"P1", "P2"}
    assert {p.quote_symbol for p in out["MEMEMINT"]} == {"SOL", "USDC"}
