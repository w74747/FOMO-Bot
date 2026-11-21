from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Values come from Railway variables (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- Infrastructure ---
    PORT: int = 8080
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str
    DATABASE_URL: str | None = None

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str
    ALLOWED_ADMIN_CHAT_ID: int

    # --- Data providers ---
    SOLANA_RPC_URL: str | None = None          # Helius mainnet RPC URL
    HELIUS_API_KEY: str | None = None          # used for whale webhooks (next phase)
    BIRDEYE_API_KEY: str | None = None         # optional: holder concentration
    JUPITER_API_KEY: str | None = None         # live prices (and execution later)
    COINGECKO_DEMO_API_KEY: str | None = None  # optional: higher GeckoTerminal limits
    GECKO_CALLS_PER_MIN: int = 8

    # --- Execution (live mode is NOT implemented yet; kept for the next phase) ---
    WALLET_PRIVATE_KEY: str | None = None
    TRADING_MODE: Literal["paper", "live"] = "paper"   # informational; live is switched with /live on

    # --- Live trading safety (runs side by side with paper) ---
    LIVE_POSITION_USD: float = 10.0
    LIVE_MAX_OPEN_POSITIONS: int = 5
    LIVE_DAILY_LOSS_USD: float = 15.0
    LIVE_SOL_RESERVE: float = 0.05           # never spend below this (fees + temporary rent)
    LIVE_EXIT_SLIPPAGE_BPS: int = 1000       # stops must get out even in a fast market
    LIVE_MIN_PAPER_TRADES: int = 15          # gate before /live on (override: /live force)

    # --- Optional AI ---
    XAI_API_KEY: str | None = None
    ENABLE_GROK_AUDIT: bool = False

    # --- Risk ---
    MAX_POSITION_USD: float = 20.0
    MAX_OPEN_POSITIONS: int = 5
    DAILY_LOSS_LIMIT_USD: float = 50.0
    MAX_SLIPPAGE_BPS: int = 300
    PAPER_START_BALANCE_USD: float = 1000.0
    PAPER_NOTIFY: bool = False              # paper trade messages while live is on (False = silent)
    STOP_COOLDOWN_HOURS: float = 12.0
    LIQUIDITY_BAN_HOURS: float = 72.0
    STRIKE_LIMIT: int = 2               # stop-losses on one coin within 7 days…
    STRIKE_BAN_HOURS: float = 72.0      # …ban it this long (its waves are breaking)   # coins whose liquidity collapsed are ignored this long
    MIN_BT_TRADES: int = 3              # no watchlist entry without back-test evidence

    # --- Network costs (Solana) ---
    # Conservative per-transaction cost: base fee (5,000 lamports/signature) + priority fee.
    # A normal swap's priority fee is ~0.00001-0.00002 SOL; 0.0001 leaves room for congestion.
    # A round trip is 3 transactions (buy, sell half, sell rest). The token-account rent
    # (~0.002 SOL) is refunded when the account is closed after the final sell.
    TX_FEE_SOL: float = 0.0001
    SOL_USD_FALLBACK: float = 150.0

    # --- Scanner (which coins qualify) ---
    SCAN_INTERVAL_MIN: int = 240
    SCAN_POOL_PAGES: int = 5
    DISCOVERY_INTERVALS: str = "24h,6h,1h,5m"   # Jupiter top-list windows (no CoinGecko quota)
    MIN_TOKEN_AGE_DAYS: float = 7.0
    MIN_LIQUIDITY_USD: float = 75_000
    # experiment: coins with liquidity between this and MIN_LIQUIDITY_USD are traded on PAPER
    # only (same safety filters), to learn whether smaller coins are worth trading for real
    PAPER_ONLY_MIN_LIQUIDITY_USD: float = 0   # experiment ended 2026-11-21 (3 trades, -8.38$, dumps through the stop); 20_000 re-enables
    PAPER_ONLY_MAX_OPEN: int = 4
    PAPER_ONLY_WATCH_SIZE: int = 10
    MIN_VOLUME_24H_USD: float = 100_000
    MAX_FDV_USD: float = 300_000_000
    MAX_TOP10_HOLDER_PCT: float = 0.40   # Jupiter audit.topHoldersPercentage
    MIN_ORGANIC_SCORE: float = 20.0       # Jupiter organic score (filters wash trading)
    MIN_HOLDERS: int = 500
    MAX_ANALYZE_PER_SCAN: int = 35
    WATCHLIST_SIZE: int = 15
    MIN_SCORE: float = 55.0
    HISTORY_HOURS: int = 720  # 30 days of hourly candles

    # --- Wave model ---
    ZIGZAG_PCT: float = 0.10
    ENTRY_DROP_FACTOR: float = 0.85   # enter after 85% of the coin's median drop
    TP1_FACTOR: float = 0.60          # first target = 60% of median rise
    TP2_FACTOR: float = 0.85          # second target = 85% of median rise
    MIN_REWARD_RISK: float = 1.3

    # --- Reporting ---
    DAILY_REPORT_HOUR_UTC: int = 5   # 09:00 Muscat

    # --- Monitor ---
    PRICE_POLL_SEC: int = 15          # full watchlist refresh
    FAST_POLL_SEC: float = 2.0        # open positions + coins in the buy zone
    JUPITER_RPM: int = 55             # account budget per 60s (free plan = 60); 600 on Developer
    JUPITER_SCAN_RESERVE: int = 15    # slots per minute scans may never use (monitor at 2s needs ~30)
    BOUNCE_CONFIRM_PCT: float = 0.02  # buy only after a 2% bounce off the local low
    # realistic back-test (coin selection + optimiser): model the bounce entry, the measured
    # polling delay, and count candle wicks only against us (they rarely show in Jupiter's price)
    BT_REALISTIC: bool = True
    BT_ENTRY_SLIP: float = 0.007
    KNOWN_COINS_RECHECK: bool = True
    # strategy health: live entries only while the realistic back-test across ALL stored coins
    # averaged more than HEALTH_MIN_PCT per trade over the last HEALTH_WINDOW_DAYS (lagged so
    # those trades are closed). Paper keeps trading regardless, to keep measuring.
    SHADOW_GATE: bool = True          # trade only what the back-test would trade, at its levels
    SHADOW_EVERY_MIN: float = 60.0
    SHADOW_MAX_AGE_MIN: float = 150.0 # older verdict = unknown = no new entries
    HEALTH_GATE: bool = True
    HEALTH_MIN_PCT: float = 0.5      # hard floor for any live entry (= autopilot stop level)
    HEALTH_WINDOW_DAYS: float = 4.0
    HEALTH_LAG_DAYS: float = 2.0
    HEALTH_MIN_TRADES: int = 10
    HEALTH_MAX_AGE_H: float = 12.0   # older reading = unknown = live blocked
    # autopilot (/live auto): the bot switches real trading on and off by itself
    AUTO_START_HEALTH: float = 2.0       # start: health above this ...
    AUTO_START_PAPER_N: int = 10         # ... and the last N paper closes (normal coins) ...
    AUTO_START_PAPER_AVG: float = 0.0    # ... average above this (%)
    AUTO_STOP_HEALTH: float = 0.5        # stop: health at or below this, or
    AUTO_STOP_PAPER_N: int = 8           # the last N paper closes average below 0%
    AUTO_KILL_USD: float = 15.0          # live drawdown from its peak since /live auto: hard stop
    AUTO_MIN_SWITCH_H: float = 12.0      # no restart within this many hours of the last switch
    AUTO_PAPER_SINCE: str = "2026-11-19T06:15:00+00:00"   # shadow back-test gate went live (trades OPENED after)  # re-check every coin with stored history on each scan
    LIQUIDITY_DROP_EXIT_PCT: float = 0.40
    LIQUIDITY_CONFIRM_READS: int = 3       # a drop must persist this many readings…
    LIQUIDITY_CONFIRM_SEC: float = 20.0    # …spanning at least this long (filters data blips)
    LIQUIDITY_ENTRY_BLOCK_MIN: float = 60.0  # no new entry for this long after ANY big drop reading

    @property
    def max_slippage(self) -> float:
        return self.MAX_SLIPPAGE_BPS / 10_000


settings = Settings()
