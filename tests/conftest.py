import os

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("ALLOWED_ADMIN_CHAT_ID", "1")
os.environ.setdefault("ENABLE_GROK_AUDIT", "")   # empty values must not crash settings
os.environ.setdefault("BIRDEYE_API_KEY", "")
