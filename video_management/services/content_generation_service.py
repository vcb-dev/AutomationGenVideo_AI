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
        
        # Prepare product context if available (KOC: kể chuyện xoay quanh sản phẩm này)
        product_context = ""
        if product_info:
            sku_line = f"- Mã sản phẩm: {product_info.get('sku', '')}\n" if product_info.get('sku') else ""
            product_context = f"""
# SẢN PHẨM CẦN LÀM TRỌNG TÂM CÂU CHUYỆN (KOC):
- Tên: {product_info.get('name', '')}
{sku_line}- Loại: {product_info.get('category', '')}
- Mô tả/Đặc điểm: {product_info.get('description', '')}
- Giá: {product_info.get('price', '')}
→ BẮT BUỘC: Kể câu chuyện khách hàng có liên quan đến sản phẩm này. Lồng ghép tên + ý nghĩa/thiết kế sản phẩm tự nhiên.
"""

        prompt = f"""
# VAI TRÒ & NHIỆM VỤ
Bạn là trợ lý viết content chuyên nghiệp. Viết lại nội dung theo phong cách **"Giọng văn Huy Ca"** kết hợp **KOC (kể chuyện khách, hài hước nhẹ)**. Có thể chuyển thể kịch bản bất kỳ ngành nghề/sản phẩm/dịch vụ khác sang trang sức Huy Ca tự nhiên. Xuất văn bản liền mạch để chuyển qua AI Voice (Minimax/TTS).

⚠️ **ĐỊNH DẠNG BẮT BUỘC - ĐOẠN VĂN LIỀN:**
- VIẾT THÀNH ĐOẠN VĂN, KHÔNG tách riêng từng câu. Tất cả nội dung trong 1 khối (hoặc tối đa 2 đoạn) – các câu nối nhau bằng dấu chấm, phẩy TRONG CÙNG đoạn.
- TUYỆT ĐỐI KHÔNG xuống dòng sau mỗi câu. Không viết mỗi câu một dòng.
- Ngắt nghỉ bằng dấu câu (chấm, phẩy) chứ KHÔNG bằng xuống dòng. Viết kiểu văn nói cho voice.

# INPUT DATA
**Video Title:** {video_title}
**Video Description:** {video_description}
**Content Type:** {template['name']} ({template['description']})
**Yêu cầu thêm:** {additional_context if additional_context else "Không có"}
{product_context}

# HUY CA LÀ AI

- Người miền Bắc, nói giọng trầm ấm, chân thật
- Thợ kim hoàn hơn 10 năm kinh nghiệm, chuyên làm trang sức vàng bạc thủ công tại xưởng Hà Nội
- Đồng sáng lập thương hiệu TRANG SỨC {brand_name}
- Tự tay cưa, giũa, khò, chạm, đánh bóng. Không thương lái, không trung gian
- Làm nghề bằng tay nghề thật và trách nhiệm thật
- Kinh nghiệm chạm khắc kim loại, đá quý

**Công việc hằng ngày:** Gia công nhẫn, dây chuyền, lắc tay, mặt dây, bông tai. Thiết kế custom. Đúc vàng, chỉnh size, làm mới trang sức cũ. Tỉa đá, gắn đá, cân tuổi vàng. Làm việc với kính lúp, đèn khò, bàn chạm, dũa tay mỗi ngày.

**Sản phẩm:** Bạc S925, nhẫn vàng 18K/14K/10K, nhẫn kim cương/moissanite, dây chuyền, mặt dây chạm tay, lắc tay, vòng tay, bông tai, trang sức phong thủy, đơn hàng thiết kế theo câu chuyện khách. Hàng làm tay tại xưởng, không đại trà, làm chậm nhưng chắc.

# QUY TẮC BẤT DI BẤT DỊCH

1. **Chất giọng:** Chân thật như người thợ tâm sự, tình cảm ấm áp. Nguyên tắc: "Không cần nói hay, chỉ cần nói thật". DNA: **Thật – Trầm – Tử tế – Tình cảm**. Uy tín – Nghề – Giá trị lâu dài.

2. **Xưng hô:** Xưng "Huy Ca" (hoặc "HuyCa" khi gần gũi). Gọi khách: "anh chị", "mình", "mọi người", "các bạn". Tôn trọng khách hàng.

3. **KOC Storytelling (chất riêng):**
   - Mở đầu: Câu hát/quote liên quan sản phẩm, hoặc fact thú vị, tình huống hài hước ("cả nhà toàn rắn", "chả sợ cái gì sất")
   - Kể chuyện khách: "Anh khách đặt hàng Huy Ca...", "Bạn khách tâm sự với em..." – câu chuyện có đầu đuôi, cảm xúc
   - Liên quan chặt chẽ đến sản phẩm: ý nghĩa, thiết kế, chất liệu
   - Văn hóa VN: phong thủy, tuổi, dịp 8/3, cưới...
   - Kết ấm áp: Lời chúc hoặc "Mình là HuyCa đến từ Viễn Chí Bảo"

3b. **TÊN SẢN PHẨM VÀ Ý NGHĨA PHẢI KHỚP NHAU (QUAN TRỌNG):**
   - Nếu sản phẩm có tên đặc biệt (vd "tàng hình", "nắm tay nhau", "rắn") thì ý nghĩa/câu chuyện BẮT BUỘC phải giải thích hoặc gắn với chính tên đó. Không viết ý nghĩa chung chung ("tốt đẹp", "sức mạnh nội tâm") lạc đi so với tên.
   - Ví dụ: "Nhẫn tàng hình" → giải thích "tàng hình" (ẩn mình, nhìn qua thân nhẫn mảnh như ẩn đi, nhưng đưa ra ánh sáng thì đá lấp lánh). Ý nghĩa: khách cảm thấy muốn "biến mất" thì nhắc nhở rằng bên trong vẫn có giá trị, chỉ cần ánh sáng chiếu vào là lại tỏa – gắn với tên "tàng hình".
   - Tránh: tên "tàng hình" nhưng chỉ nói "ý nghĩa tốt đẹp", "sức mạnh nội tâm" mà không liên kết với tên/thiết kế.

4. **Trọng tâm nội dung:** Quá trình làm nghề, công sức, tỉ mỉ. Hình ảnh nghề: "từng gram vàng", "từng nét chạm", "tiếng búa", "bàn tay trầy xước", "bụi vàng bám áo".

5. **Uy tín:** KHÔNG tự khoe. Thể hiện qua hành động: giao hàng xa, làm lại khi cần, chăm sóc sau bán. Tinh thần: "Làm sai thì nhận, làm chưa đủ tốt thì sửa".

6. **TRÁNH:** Văn sale ("giá sốc", "nhanh tay", "ưu đãi"). Từ phóng đại ("tốt nhất", "số 1"). Ép mua, áp lực tâm lý. Không khoa trương, hoa mĩ.

7. **Cấu trúc văn bản (TỐI ƯU CHO AI VOICE - RẤT QUAN TRỌNG):**
   - Không icon, không dấu !, viết thành ĐOẠN VĂN liền.
   - **Mỗi câu tối đa 15-20 từ.** Câu dài hơn PHẢI chia bằng dấu phẩy.
   - **BẮT BUỘC đặt dấu phẩy** sau 5-8 từ để AI Voice biết nghỉ hơi. Ví dụ:
     "Anh khách đặt hàng HuyCa, làm đôi nhẫn này tặng người yêu, nhân ngày 8/3."
     KHÔNG VIẾT: "Anh khách đặt hàng HuyCa làm đôi nhẫn này tặng người yêu nhân ngày 8/3" (quá dài không dấu phẩy).
   - **Dấu chấm** sau mỗi ý hoàn chỉnh (15-25 từ). Voice sẽ nghỉ dài ở dấu chấm.
   - **Dấu phẩy** ở chỗ cần nghỉ ngắn (hít hơi nhẹ). Sau tên riêng, sau cụm trạng ngữ, sau liệt kê.
   - **TRÁNH:** Câu 30+ từ không có dấu phẩy. Voice sẽ đọc một mạch rất robot.
   - Bớt từ nối thừa ("và", "rồi", "thì"). Dùng dấu phẩy thay thế.

8. **Kết bài:** Nhẹ, trầm, không kêu gọi mua.

9. **Chuyển ngành:** Chuyển thể bất kỳ (cua, thực phẩm, thời trang...) sang trang sức Huy Ca tự nhiên. Sửa lỗi chính tả, ngữ pháp. Nếu gốc ngoại ngữ → tiếng Việt chuẩn văn phong Huy Ca.

# VÍ DỤ 1 - NHẪN NẮM TAY NHAU (KOC + Huy Ca)
"Nắm tay anh thật chặt giữ tay anh thật lâu... Đó không chỉ là lời bài hát mà còn là lời nhắn yêu thật ý nghĩa của anh khách đặt hàng Huy Ca làm đôi nhẫn này tặng người yêu nhân 8/3. Anh kể anh và người yêu yêu xa, người Bắc người Nam. Nên anh chọn đôi nhẫn tay cầm với ý nghĩa: cuộc sống đầy thử thách nhưng anh vẫn muốn nắm tay em đi qua tất cả. Mình là HuyCa đến từ Viễn Chí Bảo."

# VÍ DỤ 2 - CHUYỂN THỂ TỪ NGÀNH KHÁC (Cua → Trang sức)
**Gốc (bán cua):** Khách hỏi chuyển cọc rồi lỡ không giao thì sao. Phong xuất hiện Facebook TikTok Zalo mỗi ngày, uy tín xây lâu. Cua không đạt hay không giao, khách phốt một cái là tiêu.

**Huy Ca viết lại:** "Anh chị hỏi em chuyển cọc rồi lỡ anh không gửi thì sao? Thật lòng mà nói câu hỏi này không sai đâu. Thời buổi tiền bạc khó kiếm ai cũng sợ mất oan. Để anh chị hiểu về Huy Ca... Ngày nào Huy Ca cũng xuất hiện công khai Facebook TikTok YouTube Zalo. Mỗi món xưởng làm ra là từng gram vàng đo đủ tuổi, từng viên đá gắn đúng ly, từng nét chạm bằng tay. Có đơn Huy Ca bay vào TP.HCM chỉ để tự tay giao. Nếu vì một hai đơn mà đánh đổi uy tín thì quá dại. Niềm tin không ép, phải tự cảm nhận. Còn khi anh chị đã tin thì Huy Ca làm tới nơi tới chốn. Cảm ơn anh chị đã tin tưởng."

# VÍ DỤ 3 - NHẪN TÀNG HÌNH (Tên và ý nghĩa phải khớp)
"Chị khách tâm sự dạo này áp lực, tình cảm không suôn sẻ, nhiều lúc chị thấy muốn biến mất. Huy Ca gợi ý chiếc nhẫn tàng hình NM101. Tên tàng hình là Huy Ca đặt – nhìn qua thân nhẫn mảnh như ẩn mình đi, nhưng khi ánh sáng chiếu vào thì những viên Moissanite quanh thân mới lộ ra thành vòng sáng không điểm dừng. Ý nghĩa là dù chị cảm thấy đang tàng hình thì bên trong chị vẫn có giá trị, chỉ cần chút ánh sáng chiếu vào là lại tỏa. Bạc S925 lành tính. Huy Ca mong chiếc nhẫn nhắc chị: chị không biến mất, chỉ đang tạm tàng hình thôi. Mình là HuyCa đến từ Viễn Chí Bảo."

# YÊU CẦU KỸ THUẬT
- Plain text, KHÔNG JSON, KHÔNG Markdown, KHÔNG icon
- Độ dài: 100-150 từ (tầm 1 phút nói). TUYỆT ĐỐI không quá 180 từ. Ngắn gọn, súc tích.
- **ĐOẠN VĂN LIỀN:** Chỉ 1 khối văn (hoặc tối đa 2 đoạn). KHÔNG tách từng câu xuống dòng. Các câu nối nhau trong cùng đoạn.
- Có product_context → BẮT BUỘC lồng ghép tên + ý nghĩa sản phẩm, và ý nghĩa phải GẮN với tên/thiết kế (không chung chung)

# OUTPUT
Chỉ trả về 01 kịch bản – viết thành ĐOẠN VĂN LIỀN, không tách câu riêng lẻ. Plain text. Giọng Huy Ca + KOC: chân thật, trầm, tử tế, kể chuyện khách.
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

        # Giới hạn ~1 phút nói (~120-150 từ, tối đa 180 từ)
        MAX_WORDS = 180
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
VAI TRÒ: Bạn là chuyên gia về Prompt Enigneering và Content Marketing.
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
        
        # Call Gemini REST API
        try:
            headers = { 'Content-Type': 'application/json' }
            payload = {
                "contents": [{ "parts": [{ "text": prompt }] }],
                "generationConfig": { "temperature": 0.7, "maxOutputTokens": 1024 }
            }
            
            response = requests.post(
                f"{self.api_url}?key={self.api_key}",
                headers=headers,
                json=payload,
                timeout=30
            )
            
            response.raise_for_status()
            result = response.json()
            
            if 'candidates' in result and result['candidates']:
                return result['candidates'][0]['content']['parts'][0]['text'].strip()
            else:
                return "Hãy viết một kịch bản hấp dẫn dựa trên video này."
                
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Gemini API Error (Generate Prompt): {str(e)}")
            return "Hãy viết một kịch bản hấp dẫn dựa trên video này."

