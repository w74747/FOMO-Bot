from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PORT: int = 8080
    LOG_LEVEL: str = "INFO"
    REDIS_URL: str
    DATABASE_URL: str
    FOMO_API_BASE: str = "https://fomoapi.io/api/v1"
    FOMO_API_KEY: str
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: str
    TELEGRAM_STRING_SESSION: str
    TARGET_BOT_USERNAME: str = "@DawnTradeBot"
    TELEGRAM_BOT_TOKEN: str
    ALLOWED_ADMIN_CHAT_ID: int
    XAI_API_KEY: str | None = None
    ENABLE_GROK_AUDIT: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
