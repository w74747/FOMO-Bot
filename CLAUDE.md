# FOMO-Bot — CLAUDE.md

## What this is
Solana memecoin "wave" trading bot. Finds coins older than 7 days that repeat ~20-30% drops
and rebounds, learns each coin's median drop/rise from hourly candles, and trades the waves:
buy after the typical drop + a 2% bounce, sell half at TP1, trail the rest. Paper and live
books side by side; live is controlled by an autopilot. Owner: Abdullah (Oman, Asia/Muscat).
Always reply to him in Arabic unless he writes in English. Telegram texts are Arabic.

## Stack
- Python 3.11, asyncio. python-telegram-bot 21.6 (polling, HTML, admin chat filter),
  aiohttp, redis.asyncio (state, prefix "fb:"), asyncpg (Postgres), pydantic-settings,
  loguru, solders 0.23.0.
- Railway: service FOMO-Bot + Postgres + Redis. `main` auto-deploys. Health server :8080.
- Data: Jupiter Tokens V2 (discovery), Price V3 (live prices), Swap API V2 (/order ->
  sign -> /execute); GeckoTerminal OHLCV (CoinGecko Demo key); Helius RPC (mint/freeze
  checks, balances). Birdeye disabled.

## Layout
- src/config.py — all settings (env overrides on Railway)
- src/core/state.py (Redis: positions per book "paper"/"live", watchlist, cooldowns,
  strikes, params overrides, health, live flags), src/core/database.py (Postgres tables:
  token_analyses, paper_trades, candles, tracked_pools, closed_positions, jup_prices)
- src/data/: http.py (RateLimiter, PriorityLimiter, retries, key redaction), jupiter.py,
  geckoterminal.py, safety.py, models.py
- src/analysis/: waves.py (zigzag, wave_stats, score), backtest.py (walk-forward,
  realistic options), optimizer.py (grid, A/B split by pool hash), scanner.py (discovery,
  prefilter, candles, watchlist, shadow_refresh, update_health), regime.py (market regime,
  entry/exit models, edge decay, strategy health), reconcile.py, shadow.py
- src/trading/: strategy.py (params, levels, plan, trailing), broker.py (PaperBroker,
  check_risk), live.py (LiveBroker), wallet.py, monitor.py (2 s fast / 15 s full loop),
  autopilot.py (/live auto)
- src/bot/: controller.py (Telegram commands), report.py (daily report), reasons.py
- src/main.py — wires everything; background loops: scanner, monitor, daily report,
  shadow (hourly), autopilot (10 min), Jupiter price recorder (1 min), one-off jobs
  guarded by Redis meta flags.

## Key rules (do not break)
- Live signal = back-test signal (shadow gate). Any change to entry/exit logic must be made
  in BOTH backtest.py and monitor.py, then checked with /reconcile.
- Realistic back-test is the default (BT_REALISTIC): bounce entry + 0.7% delay, candle
  wicks only count against us. Never judge a strategy on the old wick model.
- Optimiser recommends only if >= 10% better on half B and not worse on half A.
- Live safety: health <= +0.5% blocks live entries; autopilot stop rules; kill switch 15 $
  drawdown since /live auto; daily loss limit 15 $; SOL reserve 0.05; exits use 10% slippage
  and never check price impact.
- Jupiter budget: 55 req/min shared (free plan 60); prices/orders high priority.
- Never print, log or commit WALLET_PRIVATE_KEY or API keys (http.redact hides keys).
- Ask Abdullah before destructive Railway actions or before turning live trading on.
- No rest/ban after losses (REST_AFTER_LOSS=False, 2026-11-24): bans cost ~19% of back-test
  profit in both halves and made live diverge from the back-test. A broken wave waits for a
  new high (shadow bt_blocked). Liquidity-collapse bans (72 h) stay.

## Workflow
- Tests: `python -m pytest -q` and `python -m pyflakes src tests` (95 passing as of
  2026-11-26). Check for "FAILED" explicitly before committing.
- Commit to feature/wave-scanner, push it and push it to main (Railway deploys main).
  After deploy, read Railway logs (startup, "strategy health", "shadow:" lines).
- Telegram "Conflict: terminated by other getUpdates" right after a deploy is the old
  instance shutting down — harmless.

## Current state (2026-11-24)
- Live trading OFF: the autopilot (/live auto, still enabled) switched it off on 2026-11-24
  (last 8 paper trades averaged -2.18%). It restarts by itself only when health > +2%, the
  last 10 paper trades average > 0% and 12 h passed. Kill switch: drawdown since /live auto is
  14.06 $ of 15 $ -> one more live loss stops live for good until Abdullah re-enables it.
- Real results: 19 live trades, about -19.4 $ total (6 under the autopilot: -11.42 $).
- Diagnosis 2026-11-24: data (pool candles vs Jupiter 1-min prices) and execution speed are
  fine; the bot was not taking the back-test's trades (16 missed profitable ones), mainly
  because of rests/bans after losses -> REST_AFTER_LOSS=False. Next check: 2026-11-27 (does
  the bot now take the same trades as the back-test?).
- Hourly monitor: scheduled task "FOMO-Bot: مراقبة كل ساعة" (fresh session each hour) reads
  this file; it may fix bugs and redeploy, never turns live on or raises risk.
- Railway overrides: MIN_LIQUIDITY_USD=50000, MAX_OPEN_POSITIONS=12 (paper),
  MAX_ANALYZE_PER_SCAN=80, LIVE_POSITION_USD=20, LIVE_MAX_OPEN_POSITIONS=6.
- Param overrides (Redis): zigzag_pct=0.14, entry_drop_factor=1.0, tp1_factor=0.7,
  tp2_factor=1.1, stop_drop_factor=0.7, trail_factor=0.4, min_reward_risk=1.3,
  min_stop_pct=0.07, min_trail_pct=0.05.
- Wallet ~4.5 SOL (Abdullah added 2.569 SOL on 2026-11-24).

## Open questions / next
- /whatif [mint] (2026-11-26, research only): realistic back-test with vs without the downtrend
  and lower-lows score gates (backtest skip_gates; live never uses it). Without a mint: all stored
  pools, A/B by pool hash. Abdullah asked why steady-waving but falling coins (CYBERLEEK, VEIL,
  swordcat) are rejected; relaxing the gate needs the usual >= 10% on B / not worse on A.
  Result 2026-11-26 (258 pools): no lower-lows gate +4627% (A +2326 / B +2302) vs current
  +2467% (A +1306 / B +1161), median 2.5% vs 0.6%; no downtrend gate fails (A worse). The three
  coins themselves still lose without the gates. Robustness (gate_v1): lower-lows gain is broad
  (48 pools better / 19 worse, +1625% without the top 3; pools with stale data gain too, worst
  trade unchanged -29.4%); downtrend gain is 3 pools only (rest -87%) -> keep that gate.
  Proposal to Abdullah: drop only the lower-lows gate (score() in waves.py, so scanner, back-test
  and shadow change together), ideally after the 2026-11-27 no-rest check. NOT applied.
- Shadow gate: first 10 paper trades +5.5%/trade, next ones negative; reconcile again after
  the no-rest change.
- Re-test single trailing exit on 1-minute Jupiter prices (jup_prices) after ~1 week.
- Low-liquidity experiment ENDED 2026-11-21 (3 paper trades, -8.38$: dumps jump through
  the stop). PAPER_ONLY_MIN_LIQUIDITY_USD=0; coins under 50k$ liquidity are not traded.
- Per-coin acceptance (2026-11-24): >= 2 back-test trades + positive median (bt_reject_reason);
  tested out of sample. Discovery also uses GeckoTerminal trending/top-volume pools; 24h
  volume floor 50k$.
