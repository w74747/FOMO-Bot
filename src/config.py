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
    STOP_COOLDOWN_HOURS: float = 12.0
    LIQUIDITY_BAN_HOURS: float = 72.0   # coins whose liquidity collapsed are ignored this long
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
    MIN_TOKEN_AGE_DAYS: float = 7.0
    MIN_LIQUIDITY_USD: float = 75_000
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
    FAST_POLL_SEC: float = 3.0        # open positions + coins in the buy zone
    BOUNCE_CONFIRM_PCT: float = 0.02  # buy only after a 2% bounce off the local low
    LIQUIDITY_DROP_EXIT_PCT: float = 0.40

    @property
    def max_slippage(self) -> float:
        return self.MAX_SLIPPAGE_BPS / 10_000


settings = Settings()
