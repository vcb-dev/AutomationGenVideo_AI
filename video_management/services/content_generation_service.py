"""
Content Generation Service using Google Gemini REST API.
Generates marketing content based on viral videos following A1-A5 strategies.
"""
import os
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
        'duration': '15-30s',
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
        'duration': '30-60s',
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
        'duration': '30-45s',
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
        'duration': '20-40s',
        'focus': 'Sản phẩm cụ thể, giá cả, CTA rõ ràng'
    },
    'A5': {
        'name': 'Combined (Tổng hợp)',
        'description': 'Kết hợp A1-A4 - Content đa chiều',
        'examples': [
            'Nội dung liên quan ngành + kiến thức + uy tín + sản phẩm'
        ],
        'tone': 'Linh hoạt, cân bằng các yếu tố',
        'duration': '45-60s',
        'focus': 'Storytelling hoàn chỉnh từ hook đến CTA'
    }
}


class ContentGenerationService:
    """Service for generating marketing content using Gemini REST API."""
    
    def __init__(self):
        """Initialize Gemini API configuration."""
        self.api_key = settings.GEMINI_API_KEY
        # Use Gemini 2.0 Flash as seen in user's dashboard (2026 version)
        self.model = "gemini-2.0-flash"
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
    
    def generate_content(
        self,
        video_description: str,
        video_title: str,
        content_type: str,
        brand_name: str = "Viễn Chí Bảo",
        industry: str = "kim hoàn (trang sức vàng bạc)",
        additional_context: Optional[str] = None,
        product_info: Optional[Dict] = None
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
        
        # Build the prompt
        prompt = self._build_prompt(
            video_description=video_description,
            video_title=video_title,
            template=template,
            brand_name=brand_name,
            industry=industry,
            additional_context=additional_context,
            product_info=product_info
        )
        
        # Call Gemini REST API
        try:
            headers = {
                'Content-Type': 'application/json',
            }
            
            payload = {
                "contents": [{
                    "parts": [{
                        "text": prompt
                    }]
                }],
                "generationConfig": {
                    "temperature": 0.8,
                    "maxOutputTokens": 4096,  # Increased for Pro model
                }
            }
            
            response = requests.post(
                f"{self.api_url}?key={self.api_key}",
                headers=headers,
                json=payload,
                timeout=60  # Increased timeout for longer generation
            )
            
            response.raise_for_status()
            result = response.json()
            
            # Extract text from response
            if 'candidates' in result and result['candidates']:
                content = result['candidates'][0]['content']['parts'][0]['text']
                return self._parse_response(content)
            else:
                raise Exception(f"API returned no content: {result}")
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Gemini API Error: {str(e)}")
            raise Exception(f"Failed to generate content: {str(e)}")

    def _build_prompt(
        self,
        video_description: str,
        video_title: str,
        template: Dict,
        brand_name: str,
        industry: str,
        additional_context: Optional[str],
        product_info: Optional[Dict] = None
    ) -> str:
        """Build the Gemini prompt with Huy Ca voice style."""
        
        # Prepare product context if available
        product_context = ""
        if product_info:
            product_context = f"""
# THÔNG TIN SẢN PHẨM CẦN LỒNG GHÉP:
- Tên: {product_info.get('name', '')}
- Loại: {product_info.get('category', '')}
- Mô tả: {product_info.get('description', '')}
- Giá: {product_info.get('price', '')}
"""

        prompt = f"""
# VAI TRÒ & NHIỆM VỤ
Bạn là trợ lý viết content chuyên nghiệp. Hãy đóng vai "Huy Ca" để viết lại nội dung dựa trên video viral sau đây.

# INPUT DATA
**Video Title:** {video_title}
**Video Description:** {video_description}
**Content Type:** {template['name']} ({template['description']})
**Yêu cầu thêm:** {additional_context if additional_context else "Không có"}
{product_context}

# QUY TẮC BẤT DI BẤT DỊCH - PERSONA HUY CA
1. **Nhân vật:**
   - Người miền Bắc, giọng trầm ấm, chân thật.
   - Thợ kim hoàn >10 năm kinh nghiệm, làm việc trực tiếp tại xưởng.
   - Đồng sáng lập thương hiệu {brand_name}.
   - Tự tay cưa, giũa, khò, chạm, đánh bóng. Không phải thương lái/trung gian.

2. **Chất giọng:**
   - Chân thật, tâm sự thủ thỉ, tình cảm, ấm áp.
   - Nguyên tắc: "Không cần nói hay, chỉ cần nói thật".

3. **Cấu trúc văn bản (Quan trọng cho Voice):**
   - Văn bản liền mạch để chuyển qua Voice AI.
   - KHÔNG dùng icon, KHÔNG dùng dấu cảm thán (!) nhiều.
   - Hạn chế dấu phẩy (,) để câu văn liền mạch, ít ngắt quãng.
   - Viết kiểu văn nói, không phải văn viết.

4. **Xưng hô:**
   - Xưng: "Huy Ca"
   - Gọi khách: "anh chị", "mình", "mọi người".

5. **Trọng tâm nội dung:**
   - Tập trung vào quá trình làm nghề, công sức, sự tỉ mỉ.
   - Dùng từ ngữ chuyên môn thủ công: "từng gram vàng", "nét chạm", "tiếng búa", "mùi kim loại", "bàn tay trầy xước".

6. **Thể hiện uy tín:**
   - KHÔNG tự nhận "tôi uy tín nhất".
   - Thể hiện qua hành động: giao hàng xa, bảo hành, chăm sóc.
   - Tinh thần: "Làm sai thì nhận - chưa tốt thì sửa".

7. **TRÁNH:**
   - Văn phong sale ("giá sốc", "nhanh tay", "mua ngay").
   - Từ phóng đại ("số 1", "đỉnh nhất").
   - Ép buộc mua hàng.

# VÍ DỤ MINH HỌA (TONE & STYLE)
**Gốc (Văn sale):** "Mua ngay nhẫn vàng 18k giá rẻ nhất thị trường, cam kết uy tín số 1..."
**Huy Ca viết lại:** 
"Thực sự mà nói với anh chị, làm cái nghề kim hoàn này nó kén người lắm. Nhiều khi cầm cái nhẫn vàng 18K trên tay, Huy Ca cứ ngắm mãi cái nước vàng nó bóng, cái nét chạm nó sâu. Để ra được một chiếc nhẫn như thế này, anh em trong xưởng phải ngồi dũa từng chút một, bụi vàng nó bám đầy cả áo. Giá cả thì Huy Ca không dám nói là rẻ nhất, nhưng chắc chắn là đúng với công sức anh em bỏ ra. Anh chị đeo lên tay mà thấy ưng, thấy sướng, thì đó là cái lãi lớn nhất của Huy Ca rồi."

**Xử lý tình huống "Làm sai/Giao sai" (Nhẹ nhàng, trách nhiệm):**
"Nếu lỡ Huy Ca có làm sai, giao nhầm hay sản phẩm chưa được ưng ý... Anh chị cứ nhắn cho Huy Ca nhé. Huy Ca sẽ nhận trách nhiệm và sửa lại cho đến khi anh chị hài lòng mới thôi. Làm nghề này cái uy tín nó là mạng sống, một lần bất tín là vạn lần bất tin mà."

# YÊU CẦU QUAN TRỌNG VỀ ĐỘ DÀI & ĐỊNH DẠNG (BẮT BUỘC):
1. **Định dạng:** Chỉ trả về văn bản thường (Plain Text). KHÔNG dùng JSON, KHÔNG dùng Markdown, KHÔNG chia mục.
2. **Độ dài:** BẮT BUỘC tối thiểu 300 từ (tương đương 60s voice). Viết càng dài, càng sâu sắc càng tốt.
3. **Cấu trúc liền mạch:**
   - Mở đầu: Lời chào đặc trưng Huy Ca + Dẫn nhập cảm xúc.
   - Thân bài: Diễn giải chi tiết, mô tả quá trình, tâm sự nghề nghiệp.
   - **KẾT BÀI (QUAN TRỌNG):** Phải có đoạn kết tách biệt. Lời cảm ơn chân thành, lời dặn dò khách hàng, và lời chào tạm biệt trọn vẹn. TUYỆT ĐỐI KHÔNG DỪNG ĐỘT NGỘT.

# OUTPUT
Chỉ trả về 01 kịch bản hoàn chỉnh (Full Script).
"""
        return prompt.strip()

    def _parse_response(self, content: str) -> Dict[str, str]:
        """Parse Gemini response as plain text script."""
        import logging
        
        logger = logging.getLogger(__name__)
        
        # Clean up the response
        script = content.strip()
        
        # Remove common markdown artifacts if AI forgets instructions
        script = script.replace('```', '').replace('**Title:**', '').strip()
        
        # Basic cleanup of potential JSON artifacts just in case
        if script.startswith('{') and script.endswith('}'):
            try:
                import json
                data = json.loads(script)
                script = data.get('script', script)
            except:
                pass

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

