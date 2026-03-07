"""
Content Generation Service using Google Gemini REST API.
Generates marketing content based on viral videos following A1-A5 strategies.
"""
import os
import time
import requests
from typing import Dict, Optional
from django.conf import settings

# Content strategy templates
CONTENT_TEMPLATES = {
    'A1': {
        'name': 'Traffic (Viral)',
        'description': 'Mẹo, tin tức, soi sản phẩm - Thu hút lượt view',
        'examples': [
            'Mẹo liên quan đến vàng bạc (đánh sáng, cài khuy)',
            'Câu hỏi liên quan đến ngành kim hoàn',
            'Soi sản phẩm vàng bạc của người nổi tiếng',
            'Tin tức nổi bật trong ngành'
        ],
        'tone': 'Hấp dẫn, gây tò mò, viral',
        'duration': '~1 phút',
        'focus': 'Hook mạnh, nội dung ngắn gọn, dễ chia sẻ'
    },
    'A2': {
        'name': 'Knowledge (Giáo dục)',
        'description': 'Kiến thức chuyên môn - Xây dựng uy tín',
        'examples': [
            'Kiến thức thương hiệu (lịch sử, câu chuyện)',
            'Thuật ngữ liên quan (phật giáo mật tông, hư không tạng)',
            'Kiến thức về chất liệu (phân biệt đá, bạc, vàng)',
            'Ý nghĩa sản phẩm, dạy nghề kim hoàn'
        ],
        'tone': 'Chuyên nghiệp, giáo dục, dễ hiểu',
        'duration': '~1 phút',
        'focus': 'Giá trị kiến thức, lồng sản phẩm tự nhiên'
    },
    'A3': {
        'name': 'Credibility (Uy tín)',
        'description': 'Xây dựng niềm tin - Flex thành tựu',
        'examples': [
            'Kéo khách về cửa hàng (theo 100 bài hát thiếu nhi)',
            'Flex giải thưởng, từ thiện, hoạt động xã hội',
            'Giao hàng cho khách/người nổi tiếng',
            'Kể chuyện bảo hành, sửa hàng cho khách',
            'Tâm sự ngành, đọc comment tư vấn'
        ],
        'tone': 'Chân thành, gần gũi, đáng tin',
        'duration': '~1 phút',
        'focus': 'Câu chuyện thật, cảm xúc, tương tác'
    },
    'A4': {
        'name': 'Conversion (Bán hàng)',
        'description': 'Chuyển đổi trực tiếp - Giới thiệu sản phẩm',
        'examples': [
            'Top list (sản phẩm cho nam dưới 100tr, nhẫn 10tr)',
            'Tâm sự cảm xúc về sản phẩm',
            'Kể chuyện khách hàng',
            'Trả lời comment khách (tại sao ít sp nữ?)',
            'Ngân sách X mua được gì? (combo, quà tặng)'
        ],
        'tone': 'Tư vấn, nhiệt tình, thuyết phục',
        'duration': '~1 phút',
        'focus': 'Sản phẩm cụ thể, giá cả, CTA rõ ràng'
    },
    'A5': {
        'name': 'Combined (Tổng hợp)',
        'description': 'Kết hợp A1-A4 - Content đa chiều',
        'examples': [
            'Nội dung liên quan ngành + kiến thức + uy tín + sản phẩm'
        ],
        'tone': 'Linh hoạt, cân bằng các yếu tố',
        'duration': '~1 phút',
        'focus': 'Storytelling hoàn chỉnh từ hook đến CTA'
    }
}


class ContentGenerationService:
    """Service for generating marketing content using Gemini REST API."""
    
    def __init__(self):
        """Initialize service with Gemini (primary) + OpenAI (fallback)."""
        import logging
        self.logger = logging.getLogger(__name__)
        self.api_key = str(settings.GEMINI_API_KEY).strip()
        self.openai_key = str(getattr(settings, 'OPENAI_API_KEY', '')).strip()

        # Danh sách Gemini model theo thứ tự ưu tiên
        self.FALLBACK_MODELS = [
            getattr(settings, 'GEMINI_MODEL', 'gemini-2.0-flash'),
            'gemini-2.0-flash',
            'gemini-1.5-pro-latest',
            'gemini-1.5-flash-latest',
            'gemini-2.0-flash-lite',
            'gemini-1.5-flash-8b',
            'gemini-1.5-flash-001',
        ]
        seen = set()
        self.FALLBACK_MODELS = [m for m in self.FALLBACK_MODELS if not (m in seen or seen.add(m))]

        self.model = self.FALLBACK_MODELS[0]
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

        gemini_preview = f"{self.api_key[:6]}...{self.api_key[-4:]}" if self.api_key else "MISSING"
        openai_preview = f"{self.openai_key[:7]}..." if self.openai_key else "MISSING"
        self.logger.info(f"ContentGenerationService: gemini={gemini_preview}, openai={openai_preview}")
    
    def generate_content(
        self,
        video_description: str,
        video_title: str,
        content_type: str,
        brand_name: str = "Viễn Chí Bảo",
        industry: str = "kim hoàn (trang sức vàng bạc)",
        additional_context: Optional[str] = None,
        product_info: Optional[Dict] = None,
        output_language: str = 'vi',
    ) -> Dict[str, str]:
        """
        Generate marketing content based on viral video and content type.
        
        Args:
            video_description: Description/transcript of the viral video
            video_title: Title of the viral video
            content_type: Content type (A1/A2/A3/A4/A5)
            brand_name: Brand name to incorporate
            industry: Industry/product category
            additional_context: Additional context or requirements
            product_info: Optional dictionary containing product details
            
        Returns:
            Dict containing: title, script, hook, problem, solution, cta, word_count
        """
        if content_type not in CONTENT_TEMPLATES:
            raise ValueError(f"Invalid content type: {content_type}")

        template = CONTENT_TEMPLATES[content_type]

        prompt = self._build_prompt(
            video_description=video_description,
            video_title=video_title,
            template=template,
            brand_name=brand_name,
            industry=industry,
            additional_context=additional_context,
            product_info=product_info,
            output_language=output_language,
        )

        # ━━━ PRIMARY: Gemini (mạnh hơn cho tiếng Việt, structural mirroring) ━━━
        headers = {'Content-Type': 'application/json'}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096}
        }

        last_error = None
        tried_models = set()

        for model_candidate in self.FALLBACK_MODELS:
            if model_candidate in tried_models:
                continue
            tried_models.add(model_candidate)

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_candidate}:generateContent"
            self.logger.info(f"Trying Gemini model: {model_candidate}")
            try:
                response = requests.post(
                    f"{url}?key={self.api_key}",
                    headers=headers, json=payload, timeout=20
                )
                if response.status_code in (429, 404, 403):
                    status_map = {429: 'rate-limited', 404: 'not found', 403: 'forbidden'}
                    self.logger.warning(f"Gemini {model_candidate}: {status_map.get(response.status_code)} → skip")
                    last_error = f"{response.status_code} on {model_candidate}"
                    continue

                response.raise_for_status()
                result = response.json()
                if 'candidates' in result and result['candidates']:
                    content = result['candidates'][0]['content']['parts'][0]['text']
                    self.logger.info(f"Content generated via Gemini {model_candidate} ✅")
                    return self._parse_response(content)

            except requests.exceptions.HTTPError as e:
                status = getattr(response, 'status_code', 0)
                if status in (404, 403, 429):
                    last_error = str(e)
                    continue
                self.logger.error(f"Gemini HTTP error {model_candidate}: {e}")
                raise Exception(f"Failed to generate content: {e}")
            except Exception as e:
                self.logger.error(f"Gemini error {model_candidate}: {e}")
                last_error = str(e)
                continue

        # ━━━ FALLBACK: OpenAI (nếu Gemini đều fail) ━━━
        self.logger.warning("All Gemini models failed, trying OpenAI fallback...")
        openai_result = self._call_openai(prompt)
        if openai_result:
            return openai_result

        raise Exception(
            f"Tất cả AI providers đều không phản hồi. "
            f"Gemini models đã thử: {list(tried_models)}. "
            f"Lỗi cuối: {last_error}"
        )

    def _call_openai(self, prompt: str) -> Optional[Dict[str, str]]:
        """Gọi OpenAI API với cùng prompt. Return parsed result hoặc None nếu fail."""
        if not self.openai_key:
            self.logger.warning("OpenAI key not configured, skipping fallback")
            return None

        openai_models = ['gpt-4o', 'gpt-4o-mini']
        for model in openai_models:
            try:
                url = "https://api.openai.com/v1/chat/completions"
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.openai_key}'
                }
                system_msg = "Bạn là copywriter. Nhiệm vụ: đọc content gốc, lấy CÂU CHUYỆN/CHỦ ĐỀ từ đó, rồi viết lại theo cấu trúc 7 bước KOC Huy Ca (lý do → câu chuyện → tâm sự → yêu cầu khách → sản phẩm (chế tác/ý nghĩa, KHÔNG spec) → chúc → outro). Viết liền mạch. KHÔNG hashtag, KHÔNG emoji, KHÔNG dấu chấm than, KHÔNG liệt kê spec sản phẩm."
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.5,
                    "max_tokens": 4096
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code in (404, 429, 401, 403):
                    self.logger.warning(f"OpenAI {model}: {resp.status_code}, trying next...")
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data['choices'][0]['message']['content']
                self.logger.info(f"Content generated via OpenAI {model} (✅ fallback success)")
                return self._parse_response(content)
            except Exception as e:
                self.logger.error(f"OpenAI {model} error: {e}")
                continue
        return None


    def _build_prompt(
        self,
        video_description: str,
        video_title: str,
        template: Dict,
        brand_name: str,
        industry: str,
        additional_context: Optional[str],
        product_info: Optional[Dict] = None,
        output_language: str = 'vi',
    ) -> str:
        """Build the prompt with Huy Ca voice style."""
        import re
        # Lọc hashtag và emoji khỏi title
        video_title = re.sub(r'#\S+', '', video_title).strip()
        video_title = re.sub(r'[\U0001F600-\U0001F9FF\U00002702-\U000027B0\U0001F1E0-\U0001F1FF]', '', video_title).strip()

        # Bản đồ ngôn ngữ → tên hiển thị
        LANGUAGE_NAMES = {
            'vi': 'Tiếng Việt',
            'en': 'English',
            'zh': 'Chinese (Mandarin)',
            'ko': 'Korean',
            'ja': 'Japanese',
            'th': 'Thai',
            'id': 'Bahasa Indonesia',
        }
        lang_display = LANGUAGE_NAMES.get(output_language, output_language)

        lang_instruction = '' if output_language == 'vi' else f"""
# LANGUAGE REQUIREMENT
Write the entire output in **{lang_display}**. All content MUST be in {lang_display}.
Keep the Huy Ca storytelling style, just adapt to {lang_display} naturally.
"""
        
        # Prepare product context if available
        product_context = ""
        if product_info:
            product_context = f"""
# THÔNG TIN SẢN PHẨM THẬT (từ dữ liệu công ty — CHỈ dùng thông tin dưới đây, KHÔNG được bịa thêm):
- Tên: {product_info.get('name', '')}
- Loại: {product_info.get('category', '')}
- Mô tả/Đặc điểm: {product_info.get('description', '')}
- Giá: {product_info.get('price', '')}
⚠️ CHỈ được nói về sản phẩm này với ĐÚNG thông tin ở trên. KHÔNG được tự sáng tác thêm số liệu (trọng lượng, kích thước, chất liệu...) nếu không có trong mô tả. Nếu thiếu thông tin thì KHÔNG nhắc đến, đừng bịa.
"""

        prompt = f"""{lang_instruction}
BÀI MẪU (đọc kỹ — đây là nguồn BỐI CẢNH và CÂU CHUYỆN để viết content mới):
\"\"\"
{video_description}
\"\"\"

{product_context}

NHIỆM VỤ: Đọc bài mẫu → hiểu BỐI CẢNH và CÂU CHUYỆN trong đó → viết content mới cho sản phẩm trong "THÔNG TIN SẢN PHẨM THẬT" theo 7 bước KOC bên dưới.

QUY TẮC QUAN TRỌNG — ĐỌC TRƯỚC KHI VIẾT:
- Bài mẫu CÓ câu chuyện gì, nhân vật gì, tình huống gì → dùng ĐÚNG thông tin đó, KHÔNG được bịa thêm địa danh/nhân vật/tình huống không có trong bài mẫu
- Ví dụ bài mẫu nói "chị khách từ Bắc Linh xuống đặt nhẫn đôi" → câu chuyện phải là "chị từ Bắc Linh", KHÔNG được tự đổi thành nơi khác
- Ví dụ bài mẫu nói về "nhẫn bắt sáng khi gặp ánh nắng" → ghi nhận đặc điểm đó, lồng vào phần SP
- Chuyển bối cảnh bài mẫu thành câu chuyện khách tìm đến Huy Ca

BỐ CỤC 7 BƯỚC (BẮT BUỘC, theo đúng thứ tự):
1. MỞ ĐẦU = LÝ DO: Hook lấy từ chủ đề/điểm nổi bật của bài mẫu
2. CÂU CHUYỆN: Lấy đúng bối cảnh/nhân vật từ bài mẫu → viết thành câu chuyện khách tìm đến Huy Ca
3. HUY CA TÂM SỰ: Cảm nhận của Huy Ca về câu chuyện khách
4. YÊU CẦU CỦA KHÁCH: Khách muốn gì → đặt sản phẩm trong "THÔNG TIN SẢN PHẨM THẬT"
5. NÓI QUA VỀ SẢN PHẨM: Kể kiểu thợ tâm sự — công đoạn chế tác, thời gian làm, ý nghĩa đặc biệt (dùng thông tin từ bài mẫu nếu có, không liệt kê spec kỹ thuật)
6. CHÚC KHÁCH: Một câu chúc nhẹ nhàng
7. OUTRO: "Mình là HuyCa đến từ {brand_name}."

QUY TẮC:
- Xưng "Huy Ca", gọi khách "anh chị"/"mọi người"
- Giọng chân thật, trầm ấm, tử tế như thợ tâm sự
- KHÔNG dùng "giá sốc", "cao cấp", "số 1", dấu chấm than, hashtag, icon
- Viết liền mạch 1 đoạn văn nói cho video. KHÔNG xuống dòng, KHÔNG đánh số bước

Trả về CHỈ bài viết, bắt đầu ngay câu đầu tiên.
"""
        return prompt.strip()


    def _parse_response(self, content: str) -> Dict[str, str]:
        """Parse Gemini response as plain text script."""
        import logging
        
        logger = logging.getLogger(__name__)
        
        import re
        
        # Clean up the response
        script = content.strip()
        
        # Remove common markdown artifacts if AI forgets instructions
        script = script.replace('```', '').replace('**Title:**', '').strip()
        
        # Loại bỏ labels từ prompt bị echo lại
        script = re.sub(r'^Tiêu đề:.*?\n', '', script, flags=re.MULTILINE)
        script = re.sub(r'^Nội dung:.*?\n', '', script, flags=re.MULTILINE)
        script = re.sub(r'^Content Type:.*?\n', '', script, flags=re.MULTILINE)
        script = re.sub(r'^Yêu cầu thêm:.*?\n', '', script, flags=re.MULTILINE)
        script = re.sub(r'^#.*?\n', '', script, flags=re.MULTILINE)  # markdown headers
        script = re.sub(r'^\*\*.*?\*\*\s*', '', script, flags=re.MULTILINE)  # **bold labels**
        script = script.strip()
        
        # Loại bỏ hashtag, emoji, và ký tự đặc biệt
        script = re.sub(r'#\S+', '', script)  # hashtags
        script = re.sub(r'[\U0001F600-\U0001F9FF\U00002702-\U000027B0\U0001F1E0-\U0001F1FF\U0000FE00-\U0000FE0F\U00002600-\U000026FF]', '', script)  # emoji
        script = re.sub(r'[!]+', '.', script)  # dấu ! → dấu .
        script = re.sub(r'\s{2,}', ' ', script).strip()  # multiple spaces
        
        # Gộp nhiều xuống dòng thành 1 đoạn văn liền (tối đa 2 đoạn) - tránh tách từng câu
        lines = [line.strip() for line in script.split('\n') if line.strip()]
        if len(lines) > 2:
            script = ' '.join(lines)  # Gộp tất cả thành 1 đoạn liền
        elif len(lines) == 2:
            script = lines[0] + '\n\n' + lines[1]  # Giữ tối đa 2 đoạn
        
        # Basic cleanup of potential JSON artifacts just in case
        if script.startswith('{') and script.endswith('}'):
            try:
                import json
                data = json.loads(script)
                script = data.get('script', script)
            except:
                pass

        # Giới hạn ~1 phút nói (~150-200 từ, tối đa 220 từ)
        MAX_WORDS = 350
        words = script.split()
        if len(words) > MAX_WORDS:
            words = words[:MAX_WORDS]
            # Cắt tại câu hoàn chỉnh (tìm dấu chấm gần nhất)
            truncated = ' '.join(words)
            last_period = truncated.rfind('.')
            if last_period > len(truncated) * 0.5:  # Chỉ cắt nếu có câu hợp lý
                script = truncated[:last_period + 1].strip()
            else:
                script = truncated

        word_count = len(script.split())
        
        # Return simple structure with full script
        return {
            'title': 'Content Huy Ca (Full Script)',
            'hook': '',
            'problem': '',
            'solution': '',
            'cta': '',
            'script': script,
            'word_count': word_count
        }

    def generate_optimization_prompt(
        self,
        video_description: str,
        video_title: str,
        product_info: Optional[Dict] = None
    ) -> str:
        """
        Generate an optimized prompt based on video content to guide the AI generation process.
        
        Args:
            video_description: Description of the video
            video_title: Title of the video
            product_info: Optional product info
            
        Returns:
            str: An optimized prompt string
        """
        # Prepare product context if available
        product_context = ""
        if product_info:
            product_context = f"""
Sản phẩm liên quan:
- Tên: {product_info.get('name', '')}
- Loại: {product_info.get('category', '')}
- Mô tả: {product_info.get('description', '')}
"""

        prompt = f"""
VAI TRÒ: Bạn là chuyên gia về Prompt Engineering và Content Marketing.
NHIỆM VỤ: Phân tích nội dung video dưới đây và viết một PROMPT TỐI ƯU để yêu cầu AI viết kịch bản video marketing.

THÔNG TIN ĐẦU VÀO:
- Tiêu đề video gốc: {video_title}
- Mô tả video gốc: {video_description}
{product_context}

YÊU CẦU CHO PROMPT ĐẦU RA:
1. Phải là một chỉ thị rõ ràng cho AI (như "Hãy đóng vai...", "Viết kịch bản về...").
2. Tận dụng các điểm thú vị/viral của video gốc nhưng hướng nó về việc bán hàng/giới thiệu sản phẩm (nếu có thông tin sản phẩm) hoặc chia sẻ kiến thức.
3. Chỉ định rõ giọng điệu: Chân thật, trầm ấm, tử tế (style Huy Ca).
4. Yêu cầu cấu trúc: Fullscript, viết liền mạch, ngắt nghỉ bằng dấu câu.
5. Ngắn gọn, súc tích, đi thẳng vào vấn đề.

OUTPUT ONLY: Chỉ trả về nội dung PROMPT (không giải thích thêm).
"""
        
        # Call Gemini REST API — dùng FALLBACK_MODELS để tự động thử model khác nếu 404/403
        headers = {'Content-Type': 'application/json'}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}
        }

        for model_candidate in self.FALLBACK_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_candidate}:generateContent"
            try:
                response = requests.post(
                    f"{url}?key={self.api_key}",
                    headers=headers, json=payload, timeout=30
                )
                if response.status_code in (404, 403):
                    self.logger.warning(f"generate_prompt: model {model_candidate} unavailable ({response.status_code}), trying next...")
                    continue
                response.raise_for_status()
                result = response.json()
                if 'candidates' in result and result['candidates']:
                    return result['candidates'][0]['content']['parts'][0]['text'].strip()
            except Exception as e:
                self.logger.error(f"generate_prompt error on {model_candidate}: {e}")
                continue

        return "Hãy viết một kịch bản hấp dẫn dựa trên video này."
