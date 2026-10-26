import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger
from src.config import settings

class FomoClient:
    def __init__(self):
        self.base_url = settings.FOMO_API_BASE.rstrip("/")
        self.headers = {"Authorization": f"Bearer {settings.FOMO_API_KEY}"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch_leaderboard(self, timeframe: str = "7d", limit: int = 10) -> list[dict]:
        url = f"{self.base_url}/leaderboard"
        params = {"timeframe": timeframe, "limit": limit}
        async with aiohttp.ClientSession(headers=self.headers) as session:
            async with session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"FOMO API error: {resp.status} - {text}")
                data = await resp.json()
                return data.get("traders", [])
