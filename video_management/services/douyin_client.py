import httpx
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class DouyinClient:
    def __init__(self):
        # Base URL for the 3rd-party Douyin API/Proxy
        self.base_url = getattr(settings, 'DOUYIN_API_BASE_URL', 'https://api.example.com/douyin')
        self.api_key = getattr(settings, 'DOUYIN_API_KEY', '')
        self.proxy_url = getattr(settings, 'PROXY_URL', '')
        
        # Configure proxy if provided
        self.proxies = None
        if self.proxy_url:
            # Support both http and https proxies
            self.proxies = {
                'http://': self.proxy_url,
                'https://': self.proxy_url,
            }

    async def _send_request(self, method: str, endpoint: str, params: dict = None):
        """Centralized helper to send requests with API Key and Proxy."""
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        
        # httpx AsyncClient uses 'proxy' (str) or mounts for more complex cases
        proxy = self.proxy_url if self.proxy_url else None
        
        async with httpx.AsyncClient(proxy=proxy) as client:
            try:
                response = await client.request(method, url, params=params, headers=headers, timeout=10.0)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error at {endpoint}: {e.response.status_code} - {e.response.text}")
                return None
            except Exception as e:
                logger.error(f"Error at {endpoint}: {e}", exc_info=True)
                return None


    async def search_videos(self, keyword: str):
        """Fetch raw video data from Douyin API based on keyword."""
        return await self._send_request("GET", "search", params={"keyword": keyword})

    async def get_channel_videos(self, channel_id: str):
        """Fetch latest videos from a specific channel."""
        return await self._send_request("GET", "channel/videos", params={"channel_id": channel_id})

    async def parse_video_no_watermark(self, video_url: str):
        """Call 'No-Watermark' parsing service."""
        return await self._send_request("GET", "parse", params={"url": video_url})

