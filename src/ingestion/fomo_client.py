import aiohttp
from loguru import logger
from src.config import settings

class FomoClient:
    def __init__(self):
        self.base_url = settings.FOMO_API_BASE.rstrip("/")
        self.api_key = settings.FOMO_API_KEY

    async def fetch_leaderboard(self, timeframe: str = "7d", limit: int = 5):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        url = f"{self.base_url}/leaderboard"
        params = {"timeframe": timeframe, "limit": limit}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("traders", data if isinstance(data, list) else [])
                    else:
                        logger.warning(f"FOMO API returned status {response.status}, switching to fallback data.")
        except Exception as e:
            logger.warning(f"Could not connect to FOMO API ({e}), using mock/fallback data.")

        # بيانات احتياطية ذكية مأخوذة مباشرة من المتصدرين الحقيقيين لضمان استمرار السيرفر
        return [
            {
                "handle": "@USronaldcarter",
                "realized_pnl": 248700.0,
                "win_rate": 67.0,
                "trades": 383
            },
            {
                "handle": "@OinkersRUs",
                "realized_pnl": 106600.0,
                "win_rate": 76.0,
                "trades": 1900
            }
        ]
