import httpx
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

class TelegramService:
    @staticmethod
    async def send_message(message: str):
        # These should be in environment variables
        bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN')
        chat_id = getattr(settings, 'TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')
        
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error sending Telegram message: {e.response.status_code} - {e.response.text}")
                return None
            except Exception as e:
                logger.error(f"Error sending Telegram message: {e}", exc_info=True)
                return None

    @classmethod
    async def notify_hot_video(cls, channel_name, like_count, video_url):
        message = (
            f"🔥 <b>Hot Video Alert!</b>\n\n"
            f"Channel: <b>{channel_name}</b>\n"
            f"❤️ Likes: <b>{like_count:,}</b>\n"
            f"🔗 Link: {video_url}"
        )
        return await cls.send_message(message)
