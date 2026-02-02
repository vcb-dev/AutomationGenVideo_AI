from django.conf import settings
from openai import OpenAI
import logging

logger = logging.getLogger(__name__)

class AIWriter:
    def __init__(self):
        try:
            self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
            self.enabled = True
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {str(e)}")
            self.enabled = False

    def generate_script(self, topic, tone="professional"):
        if not self.enabled:
            return "Lỗi: OpenAI chưa được cấu hình hoặc key không hợp lệ."

        tones = {
            "professional": "Chuyên nghiệp, trang trọng, lịch sự, tạo niềm tin.",
            "casual": "Thân thiện, vui vẻ, gần gũi (dùng từ 'mình', 'các bạn'), dùng ngôn ngữ đời thường.",
            "sale": "Hấp dẫn, thúc giục mua hàng, tập trung vào ưu đãi, giảm giá và lợi ích (FOMO)."
        }
        
        selected_tone_desc = tones.get(tone, tones["professional"])

        messages = [
            {
                "role": "system",
                "content": (
                    "Bạn là chuyên gia sáng tạo nội dung (Copywriter) hàng đầu cho thương hiệu trang sức vàng bạc đá quý 'Viễn Chí Bảo'. "
                    "Nhiệm vụ của bạn là viết kịch bản short video để MC ảo (Avatar) đọc. "
                    "Hãy viết thật tự nhiên như văn nói."
                )
            },
            {
                "role": "user",
                "content": (
                    f"Hãy viết một kịch bản ngắn (khoảng 3-6 câu, dưới 100 từ) về chủ đề: '{topic}'.\n"
                    f"Phong cách (Tone): {selected_tone_desc}\n\n"
                    "Yêu cầu:\n"
                    "- Bắt đầu bằng lời chào phù hợp.\n"
                    "- Nội dung hấp dẫn, đi thẳng vào vấn đề.\n"
                    "- Kết thúc bằng lời kêu gọi hành động (CTA) nhẹ nhàng.\n"
                    "- Dùng ngôn ngữ nói tự nhiên, tránh văn viết cứng nhắc.\n"
                    "- Chỉ trả về nội dung kịch bản, không trả về chú thích."
                )
            }
        ]

        try:
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo", # Dùng gpt-3.5 cho nhanh và rẻ
                messages=messages,
                max_tokens=250,
                temperature=0.8, # Sáng tạo một chút
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"OpenAI generation error: {str(e)}")
            return f"Xin lỗi, có lỗi khi tạo kịch bản: {str(e)}"
