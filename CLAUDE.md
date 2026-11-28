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
- Tests: `python -m pytest -q` and `python -m pyflakes src tests` (116 passing as of
  2026-11-28). Check for "FAILED" explicitly before committing.
- Commit to feature/wave-scanner, push it and push it to main (Railway deploys main).
  After deploy, read Railway logs (startup, "strategy health", "shadow:" lines).
- Telegram "Conflict: terminated by other getUpdates" right after a deploy is the old
  instance shutting down — harmless.

## Current state (2026-11-27)
- PAPER ONLY until Abdullah decides (his call 2026-11-27): the lower-lows gate was dropped and
  the bot runs paper only for 3 days (to 2026-11-30) to watch the results. One-off
  paper_only_1127 disabled the autopilot and set live off. The monitor must NOT re-enable
  /live auto or live; on 2026-11-30 review the paper trades since the deploy (avg, vs back-test
  via /reconcile) and give Abdullah a recommendation. Re-enabling /live auto resets the kill
  switch baseline (auto_since).
- Lower-lows gate REMOVED 2026-11-27 from score() in waves.py (no rejection; it still lowers the
  score). Downtrend gate kept. Entry desync (Jupiter price vs pool candles) is still open.

- ENTRY-MODE FINDING 2026-11-27 (entrymode_v1, 258 pools): the same strategy entered at the
  hourly CLOSE (ENTRY_ON_CLOSE, which live can reproduce exactly) loses: 511 trades, avg -2.53%,
  median -6.0%, A -675% / B -618% (bounce 1% and 3% also negative), vs the current intra-hour
  model +6.22% avg (A +2336 / B +2301). The current model only buys bounces that held until the
  candle close (lookahead), live at 2 s cannot know that -> its edge is likely overstated, which
  fits the real results (live ~-5%/trade, paper negative after the first 10). ENTRY_ON_CLOSE stays
  False (code kept: backtest close_entry, shadow signal, Monitor.enter_signals). The lower-lows
  gate result was measured on the same optimistic model. Proposed next: back-test entries/exits
  on 5-minute candles (stats from hourly) + jup_prices to measure the edge live can capture,
  before any return to live.
- 5-MINUTE TEST (approved by Abdullah 2026-11-27, research only): src/analysis/finetest.py
  backtest_fine (brain on hourly: stats/score/levels/refit 24 h; entries + exits on 5-min
  candles; modes "close" = decide at each 5-min close, no lookahead, and "intra" = current
  bounce model inside the 5-min bar). fine_models compares hourly intra / hourly close / 5-min
  intra / 5-min close on the same period per pool, A/B. Data: table candles_5m, filled by
  fine_loop (Scanner.collect_fine: active pools <= 150, backfill 4 x 1000 bars, hourly top-up,
  one Gecko call / 4 s). First full backfill sends the result once (finetest_v1); /finetest
  re-runs it. Abdullah: wait a few days, then judge the paper trades + this test together
  (review 2026-11-30). Live return only if "5-min close" is clearly positive on A and B.
  FIRST RESULT finetest_v1 (2026-11-27 12:44 UTC, 61 pools, ~19.8 days each): hourly intra 166
  trades avg +5.53% (A +3.73 / B +7.65); hourly close 116, avg -0.48% (A -1.34 / B +0.87);
  5-min intra 242, avg +3.05% (A +3.09 / B +3.01); 5-min close 221, avg +0.23%, median -1.9%
  (A -0.13 / B +0.65) -> NOT clearly positive; live-capturable edge ~0. Data still filling
  (62 of <=150 pools); re-run /finetest before the 11-30 review.
  Live (2 s) sits between 5-min close and 5-min intra -> thin edge. Paper 27/11: paws and
  Rings both hit TP1 (+2.77$, +3.51$).
- 5-MIN PARAMETER SEARCH (approved 2026-11-28, research only): finetest.fine_grid, 324 combos
  (bounce 1/2/3/5%, entry_drop 0.8/1.0/1.2, stop_drop 0.5/0.7/1.0, tp1 0.5/0.7/1.0, trail
  0.3/0.4/0.6) on the 5-min close model, ranked by the worse half (>= 25 trades per half).
  One-off finegrid_v1, /finegrid re-runs. Best combos on ~20 days are partly luck: adopt only
  what stays good on later data, with Abdullah's approval, in BOTH backtest + live.
  RESULT finegrid_v1 (2026-11-27 20:42 UTC, 63 pools): current settings 225 trades +0.27%
  (A -0.27 / B +0.90); only 2 of 324 combos positive in both halves, best bounce 3% + trail
  x0.6: +0.43% (A +0.47 / B +0.39), i.e. no parameter set gives a real edge. Conclusion: the
  edge is not in the parameters; the entry idea itself is ~breakeven under honest execution.
  Nothing changed. Live stays off; next steps (new entry signal, wallet tracking, or more data)
  are Abdullah's call.

- FLOW / HOLDERS / SMART WALLETS (approved by Abdullah 2026-11-28, research only, no effect on
  trading): src/analysis/flow.py FlowCollector. flow_loop: GeckoTerminal pool trades (<= 300
  latest, table pool_trades) for watchlist + held + most liquid active pools (<= 20) every 5
  min; Jupiter holderCount (table holders) every 15 min. smart_loop: once a day Birdeye
  top_traders (30 d, total_pnl) of <= 25 of our coins -> table smart_wallets (bots excluded in
  db.smart_wallets). src/data/birdeye.py; needs BIRDEYE_API_KEY on Railway (free Standard plan
  30k CU/month, 1 rps; top_traders may need Starter 99$/month: on 401/403 it stops and
  /wallets says so). BIRDEYE_SECURITY=False keeps the free budget off token_security.
  Commands /flow, /wallets. Abdullah prefers Birdeye over hand-picked FOMO wallets. Plan:
  after ~1 week test flow-gated entries (net buying / smart buys before the bounce) on the
  honest 5-min-close model, A/B, before any use. Watch GeckoTerminal 429s (CoinGecko Demo
  plan is listed as 10k calls/month; no 429 from it seen so far).

- GECKO QUOTA 2026-11-28 08:45 UTC: the CoinGecko Demo key hit its 10,000-call MONTHLY cap
  (error 10006; flow_loop trades + fine_loop used it up). Fix 3fc1a57: a 10006 429 is not retried and
  GeckoClient switches to the keyless api.geckoterminal.com (20/min) until restart; keyless gives
  some 429s at that rate but shadow/candles keep working. Demo quota should reset 2026-12-01.
  Open for Abdullah: slow flow/fine loops until then, or a paid CoinGecko plan.
  2026-11-28 follow-up: keyless still drew constant 429s (core candles/shadow competing with
  research loops) -> while gecko.fallback: KEYLESS_PER_MIN 15, flow trades only watchlist +
  held (<= 8) every 15 min, 5-min candle top-up paused; GeckoClient returns to the demo key by
  itself in a new UTC month (no restart needed). From December the research loops use the
  10k/month quota again and will run out again: budget them (or a paid plan) before Dec 1.
  2026-11-28 14:25 (6249dbc): scans had not finished since 07:41 UTC: on keyless, best_pools made one
  call per token after a failed (429) multi call, each retried 30-180 s. Now no per-token calls on
  keyless or after a failed multi call. Keyless still 429s almost every call from Railway's IP.

## State before 2026-11-27 (history)
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
  APPROVED by Abdullah 2026-11-26: drop only the lower-lows gate (score() in waves.py, so
  scanner, back-test and shadow change together; keep the downtrend gate). APPLIED 2026-11-27.
  11-27 check result (one-off check1127): since the no-rest deploy only 1 paper trade (CBUCKS
  +9.1%, time stop, NOT a back-test trade: Jupiter price touched a level 2.3% above the pool
  candles' entry level); the back-test took 2 trades on watchlist coins that paper missed
  (FLOAT +17%, CBUCKS +15%; shadow showed bt_in_trade 26/11 01:26-10:41). Rests were not the
  cause; the gap is entry desync (Jupiter price vs pool candles; overall reconcile: 18 "level
  reached but bt saw no bounce", 15 "pool candle never reached the level"). Condition "trades
  match" NOT met -> lower-lows change held, asked Abdullah (apply anyway vs fix desync first).
- Shadow gate: first 10 paper trades +5.5%/trade, next ones negative; reconcile again after
  the no-rest change.
- Re-test single trailing exit on 1-minute Jupiter prices (jup_prices) after ~1 week.
- Low-liquidity experiment ENDED 2026-11-21 (3 paper trades, -8.38$: dumps jump through
  the stop). PAPER_ONLY_MIN_LIQUIDITY_USD=0; coins under 50k$ liquidity are not traded.
- Per-coin acceptance (2026-11-24): >= 2 back-test trades + positive median (bt_reject_reason);
  tested out of sample. Discovery also uses GeckoTerminal trending/top-volume pools; 24h
  volume floor 50k$.
