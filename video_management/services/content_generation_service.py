"""
Content Generation Service for A1-A5 marketing script generation.
"""
import os
import time
import json
import requests
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, List, Tuple, Any
from django.conf import settings


class DeepSeekError(RuntimeError):
    """
    Lỗi khi gọi DeepSeek, có PHÂN LOẠI — thay cho việc nuốt mọi lỗi thành None.

    Trước đây `_call_deepseek_raw` bọc toàn bộ trong `except Exception: return None`, nên
    timeout / 429 rate-limit / 5xx của DeepSeek / JSON hỏng đều quy về cùng một triệu chứng
    "DeepSeek không phản hồi" → BE báo "có thể do timeout" cho MỌI trường hợp. Hệ quả là
    không thể biết một lượt chấm PAAST hỏng vì chậm thật hay vì bị chặn rate-limit — mà 5
    lệnh gọi song song trên cùng 1 API key thì 429 là chuyện rất thực tế.

    `kind` nhận: 'timeout' | 'rate_limit' | 'server' | 'client' | 'parse' | 'network' | 'no_key'.
    `retriable` phân biệt lỗi đáng thử lại (timeout/429/5xx/mạng) với lỗi tất định
    (4xx khác, parse) — thử lại lỗi tất định chỉ tốn thời gian mà kết quả không đổi.
    """

    RETRIABLE_KINDS = {'timeout', 'rate_limit', 'server', 'network'}

    def __init__(self, kind: str, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code

    @property
    def retriable(self) -> bool:
        return self.kind in self.RETRIABLE_KINDS


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

HUYK_MANDATORY_STYLE = """
STYLE HUYK - QUY TẮC BẤT DI BẤT DỊCH:
- Giọng thật, trầm, tử tế, như người thợ tâm sự; không khoa trương.
- Xưng "Huy Ca", gọi khách là "anh chị", "mọi người", "các bạn".
- TUYỆT ĐỐI KHÔNG được nhắc thương hiệu "Viễn Chí Bảo" trong nội dung.
- Nhân vật Huy Ca: thợ kim hoàn hơn 10 năm, làm trực tiếp tại xưởng Hà Nội.
- Tập trung vào quá trình làm nghề: từng gram vàng, từng nét chạm, công sức, trách nhiệm.
- Không dùng văn phong ép sale: không "giá sốc", "nhanh tay", "số 1", không tạo áp lực mua.
- Có thể chuyển thể từ ngành khác sang ngữ cảnh trang sức Huy Ca nhưng phải tự nhiên và đúng tinh thần gốc.
- Tự sửa lỗi chính tả/ngữ pháp/ngữ nghĩa nhưng không làm sai ý nghĩa ban đầu.
- Ưu tiên văn nói liền mạch tối ưu voice video.
- BẮT BUỘC có câu hỏi mở để kéo tương tác (ví dụ: "anh chị nghĩ sao về...").
- BẮT BUỘC có CTA bình luận tự nhiên (ví dụ: "anh chị bình luận cho Huy Ca biết nhé").
"""

GLOBAL_MANDATORY_STYLE = """
STYLE GLOBAL (INTERNATIONAL) - QUY TẮC BẤT DI BẤT DỊCH:
- Xưng hô: Sử dụng "Tôi" (I) và "Bạn" (You).
- Đối với tiếng Thái (TH): BẮT BUỘC xưng "Phom" (ผม) và gọi khách là "Khun" (คุณ). Sử dụng giọng NAM chân thành (hạt từ ครับ).
- Đối với thị trường Thái: Ưu tiên phong cách "Heart-touching" (sến sến, cảm động). TUYỆT ĐỐI KHÔNG xưng "Huy Ca" hay nhắc "Viễn Chí Bảo". Phải xưng "Phom" (Tôi - Nam) và gọi "Khun" (Bạn).
- Tập trung vào cảm xúc, tấm lòng và giá trị tinh thần của món quà.
- Không dùng văn phong ép sale, ưu tiên storytelling.
- BẮT BUỘC có câu hỏi mở và CTA lịch sự ở cuối video.
"""

THAI_TEAM_PROMPT_1 = """
Bạn là một content creator người Thái bản địa, chuyên tạo nội dung "viral heart-touching" trên TikTok/Facebook (kiểu quảng cáo Thái Lan gây xúc động).
Hãy viết lại nội dung từ tiếng Việt sang tiếng Thái theo phong cách:
- Hook mạnh nhưng tinh tế, đánh vào cảm xúc ngay 1-2 câu đầu.
- Câu ngắn, dễ đọc, nhịp điệu mượt mà cho subtitle.
- Cảm xúc "sến" một cách chân thật, tự nhiên như cách người Thái tâm tình.
- Thái Hoá hoàn toàn: không dịch sát nghĩa, hãy dùng "ngôn ngữ bản địa" để truyền tải linh hồn của câu chuyện.
- Am hiểu địa danh và văn hóa: lồng ghép khéo léo bối cảnh Thái (Bangkok, Yaowarat, BTS, cafe Chiang Mai...) nếu phù hợp.
- BẮT BUỘC giọng nam lịch sự (ưu tiên dùng ครับ, tuyệt đối không dùng ค่ะ).
"""

THAI_TEAM_PROMPT_2 = """
Bạn là một copywriter người Thái, am hiểu sâu văn hóa và hành vi tiêu dùng Thái Lan.
Yêu cầu quan trọng:
- Giữ nguyên ý nghĩa cốt lõi nhưng tối ưu câu chữ cho thị trường Thái.
- Văn phong nhẹ nhàng, tinh tế, giàu cảm xúc (emotional & soft selling).
- Tránh giọng quảng cáo quá mạnh hoặc phô trương.
- Ưu tiên storytelling, tạo cảm giác chân thành.
- Dùng từ ngữ phổ biến, tự nhiên với người Thái hiện đại.
- Có thể thêm emoji nhẹ nhàng nếu phù hợp.
"""

THAI_LOCALIZATION_TRAINING_BLOCK = """
NÂNG CẤP CHẤT LƯỢNG CHO THỊ TRƯỜNG THÁI - "THÁI HOÁ" TOÀN DIỆN (BẮT BUỘC):
- Địa danh & văn hóa: Sử dụng các "mỏ neo" thực tế với người Thái hiện đại (Siam Square, Yaowarat, đời sống đô thị Bangkok, sự bình yên ở Chiang Mai, các dịp lễ Songkran/Loy Krathong).
- Hành vi tiêu dùng: Người Thái chuộng cảm xúc (Emotional Connect). Họ coi trọng "Social Proof" và sự tinh tế trong dịch vụ. Hãy bán hàng bằng câu chuyện ("Soft Selling"), không ép khách.
- Sản phẩm: Nhấn mạnh giá trị "Handmade", độ tỉ mỉ của người thợ, và ý nghĩa phong thuỷ/may mắn (มงคล) phù hợp văn hoá Thái. Tuyệt đối không nhắc "Viễn Chí Bảo".
- Ngôn ngữ: Dùng từ ngữ tự nhiên, nhịp điệu "sến sến" đầy cảm xúc nhưng vẫn nam tính (giọng NAM, dùng hạt từ ครับ).
- Mục tiêu: Chuyển đổi từ nội dung Việt sang một kịch bản Thái có hồn, khiến người bản địa cảm thấy đây là nội dung dành riêng cho họ.
"""

THAI_PRONOUN_LOCK = """
BẮT BUỘC RIÊNG CHO TIẾNG THÁI (TH):
- XƯNG HÔ (NGÔI 1 - Tôi): Tuyệt đối KHÔNG dùng "Huy Ca" hoặc "ฮุยกา". Phải dùng "Phom" (ผม).
- NGƯỜI XEM (NGÔI 2 - Bạn): Tuyệt đối KHÔNG dùng "thoe" (เธอ). Phải dùng "Khun" (คุณ).
- NHÂN VẬT TRONG TRUYỆN (NGÔI 3 - Bạn ấy): BẮT BUỘC dùng "Phuean khon nan" (เพื่อนคนนั้น) để chỉ "Bạn ấy/Bạn đó". Cụm từ này giúp bản dịch tiếng Việt luôn có chữ "Bạn" và mang sắc thái thân mật, trung tính. Tuyệt đối KHÔNG dùng "Khao" (anh ấy) hay "Nong" (đứa em).
- GIỌNG ĐIỆU: Giọng NAM lịch sự (dùng ครับ, CẤM dùng ค่ะ).
- THƯƠNG HIỆU: CẤM nhắc đến "Viễn Chí Bảo" (Vien Chi Bao).
- CỤM TỪ TINH TẾ: Khi nói về việc không làm xao nhãng hoặc không cướp sự chú ý của chi tiết chính, BẮT BUỘC dùng "โดยไม่แย่งความสนใจ" (Doy mai yaeng khwam son jai). Tuyệt đối KHÔNG dùng "ขโมยซีน" hoặc "ขโมยซีนไป" (nghe thô như cướp giật). Nếu trong bản gốc có "ขโมยซีน", phải thay bằng "โดยไม่แย่งความสนใจ".
"""

# Bản Việt “nguồn convert” cho MỌI thị trường (không chỉ một ngôn ngữ).
GLOBAL_VI_SOURCE_CULTURE_CHECKLIST = """
CHECKLIST BẢN VIỆT NGUỒN CHO MỌI THỊ TRƯỜNG ĐÍCH (BẮT BUỘC):
1) Ít nhất 2 chi tiết bối cảnh bản địa rõ ràng (địa danh / đời sống / lễ hội / cách tặng quà / insight mua sắm) phù hợp thị trường đích — không chỉ văn Việt “chung chung” không màu nước.
2) Ít nhất 1 câu insight tiêu dùng theo thị trường đích (diễn giải bằng tiếng Việt).
3) Reframe nhân vật: người bản địa / người Việt ở nước đó / tặng quà cho người ở đó — chọn MỘT hướng và giữ xuyên suốt (không nhập vai như chỉ ở Việt Nam trừ khi transcript gốc bắt buộc).
4) Tránh đoạn chỉ lặp mẫu “khách đến xưởng” mà không có màu thị trường đích.
5) Giữ đúng thông tin sản phẩm thật; không bịa giá hoặc thông số.
"""

# Bản Việt nhưng “nhuộm” thị trường TH — checklist cứng để không ra văn Việt generic.
THAI_VI_SOURCE_CULTURE_CHECKLIST = """
CHECKLIST VĂN HOÁ THÁI CHO BẢN VIỆT (BẮT BUỘC ĐẠT TỐI THIỂU — NẾU THIẾU THÌ VIẾT LẠI):
1) Có ít nhất 2 chi tiết bối cảnh Thái rõ ràng (chọn trong các hướng: Bangkok/Chiang Mai/Phuket; nhịp sống đô thị; trung tâm thương mại; BTS/MRT; dịp lễ Songkran/Loy Krathong/Valentine/sinh nhật; văn hoá tặng quà/ของขวัญ; gia đình & kỷ niệm).
2) Có ít nhất 1 câu nói về insight tiêu dùng Thái bằng tiếng Việt (ví dụ: ưu tiên cảm xúc, tinh tế, không thích bị ép mua, thích câu chuyện đằng sau món quà, coi trọng sự tôn trọng khi tặng).
3) Reframe nhân vật/bối cảnh: khách có thể là người Thái, người Việt sống ở Thái, hoặc người chọn quà cho người ở Thái — chọn MỘT hướng và giữ xuyên suốt (không nhập vai như đang ở Hà Nội/Sài Gòn trừ khi transcript gốc bắt buộc).
4) Tránh đoạn chỉ lặp lại mẫu chung kiểu “khách đến xưởng” mà không có màu Thái; phải có ít nhất một cụm từ/cảm giác “đời sống Thái hiện đại” (không cần chữ Thái).
5) Giữ đúng thông tin sản phẩm thật; không bịa giá/giấy tờ.
"""

JAPANESE_LOCALIZATION_TRAINING_BLOCK = """
NÂNG CẤP CHẤT LƯỢNG CHO THỊ TRƯỜNG NHẬT (BẮT BUỘC):
- Địa danh & văn hóa: ưu tiên ngữ cảnh phù hợp đời sống Nhật hiện đại (Tokyo, Osaka, Kyoto, Fukuoka...) và tinh thần tôn trọng, tinh tế, chỉn chu.
- Hành vi tiêu dùng: người dùng Nhật coi trọng độ tin cậy, chi tiết hoàn thiện, câu chuyện thủ công, dịch vụ hậu mãi và tính bền vững.
- Sản phẩm nhà Huy Ca: giữ DNA thợ kim hoàn thủ công, trách nhiệm nghề, ý nghĩa món trang sức; không phóng đại, không bịa thông số.
- Ngôn ngữ bản địa: văn phong tự nhiên như người Nhật thật, lịch sự vừa đủ, câu rõ nghĩa, dễ đọc subtitle, không dịch word-by-word.
- Mục tiêu: từ 1 video win sinh ra 1 content tiếng Nhật chất lượng cao, đúng insight thị trường Nhật, đúng tinh thần Huy Ca.
"""

JAPANESE_VI_SOURCE_CULTURE_CHECKLIST = """
CHECKLIST VĂN HOÁ NHẬT CHO BẢN VIỆT (BẮT BUỘC ĐẠT TỐI THIỂU):
1) Ít nhất 2 chi tiết bối cảnh Nhật (Tokyo/Osaka/Kyoto/Fukuoka; tàu điện; trung tâm; mùa lễ/hanami khi hợp; văn hoá quà tặng tinh tế; omotenashi — diễn giải bằng tiếng Việt).
2) Ít nhất 1 câu insight tiêu dùng Nhật (tin cậy, chỉn chu, hoàn thiện, không ép mua, coi trọng cảm giác “đáng tin” khi mua đồ đeo lâu).
3) Reframe nhân vật phù hợp người xem Nhật (không bắt buộc đặt câu chuyện ở Việt Nam trừ khi transcript gốc yêu cầu).
4) Tránh bài chỉ giống kịch bản Việt dịch nghĩa mà không có màu đời sống Nhật.
5) Giữ đúng thông tin sản phẩm thật.
"""

GLOBAL_LOCALIZATION_BASE_BLOCK = """
NGUYÊN TẮC LOCALIZATION CHO THỊ TRƯỜNG QUỐC TẾ:
- Không dịch word-by-word. Phải viết lại theo ngôn ngữ đời thường của người bản địa.
- Giữ nguyên ý nghĩa cốt lõi, nhưng tối ưu ngôn ngữ theo văn hóa và hành vi tiêu dùng của thị trường đích.
- Luôn giữ DNA Huy Ca: thật, trầm, tử tế, tay nghề thủ công, trách nhiệm nghề.
- Không dùng giọng ép sale, không phô trương, không bịa thông tin sản phẩm.
"""

LOCALIZATION_BY_LANGUAGE = {
    'ja': """
MARKET FOCUS (JA — Nhật Bản):
- Đời sống: Tokyo/Osaka/Kyoto; nhịp chỉn chu; quà tặng tinh tế; coi trọng độ tin cậy và hoàn thiện.
- Tâm lý: không thích bị ép mua; thích câu chuyện thủ công và trách nhiệm; omotenashi (diễn giải bằng tiếng Việt).
- Tránh giọng quảng cáo ồn ào; ưu tiên cảm giác an tâm khi mua trang sức.
""",
    'en': """
MARKET FOCUS (EN):
- Audience values clarity, authenticity, and practical value.
- Keep wording simple, natural, conversational; avoid overclaiming.
- Balance emotional storytelling with concrete product trust signals.
- Bối cảnh: thành phố lớn US/UK/AU khi hợp; quà tặng dịp kỷ niệm; social proof tự nhiên.
""",
    'id': """
MARKET FOCUS (ID):
- Ưu tiên giọng gần gũi, thân thiện, đáng tin và dễ chia sẻ.
- Người dùng đề cao tính thực tế + cảm xúc gia đình/cá nhân.
- Tránh giọng quảng cáo quá gắt, giữ soft-selling tự nhiên.
""",
    'ms': """
MARKET FOCUS (MS):
- Văn phong lịch sự, ấm áp, tinh tế; đề cao sự đáng tin cậy.
- Nhấn vào ý nghĩa món quà và sự chỉn chu trong tay nghề.
- Tránh ngôn từ quá phô trương hoặc gây áp lực mua.
""",
    'zh': """
MARKET FOCUS (ZH):
- Ưu tiên ngôn ngữ tự nhiên, mạch lạc, giàu cảm xúc nhưng tiết chế.
- Nhấn điểm craftsmanship, độ tinh xảo và giá trị biểu tượng.
- Giữ tông chuyên nghiệp, đáng tin, không dùng từ quá khoa trương.
- Bối cảnh: đô thị Trung Quốc hiện đại, gia đình, quà tặng ý nghĩa khi phù hợp.
""",
    'zh-TW': """
MARKET FOCUS (ZH-TW):
- Tinh thần tương tự Hoa ngữ: tinh tế, kín đáo, trọng ý nghĩa món quà và độ bền cảm xúc.
- Bối cảnh: Đài Bắc/Cao Hùng/Đài Trung khi hợp; đời sống đô thị; lễ tình nhân/sinh nhật.
""",
    'ko': """
MARKET FOCUS (KO):
- Văn phong gọn, tự nhiên, tôn trọng người đọc và giàu cảm xúc nhẹ.
- Tập trung vào độ hoàn thiện, trải nghiệm đeo, ý nghĩa quà tặng.
- Tránh salesy tone mạnh, ưu tiên storytelling đời thường.
""",
    'th': """
MARKET FOCUS (TH — Thái Lan):
- Phong cách: Heart-touching storytelling (sến sến, sâu sắc).
- Quà tặng & trang sức: Nhấn mạnh ý nghĩa cảm xúc, sự may mắn (Lucky charm/Amulet vibe), tính độc bản handmade.
- Đời sống: Bangkok & các thành phố lớn, BTS/MRT, trung tâm mua sắm sầm uất, sự tôn trọng lẫn nhau (Kreng Jai).
- Lễ & thói quen: Songkran, Loy Krathong, Valentine Thái, ngày của Mẹ, tặng quà để tri ân/biết ơn.
- Insight tiêu dùng: Thích xem video kể chuyện đời thường, tin vào sự chân thành của người thợ, không thích quảng cáo lộ liễu.
""",
    'tl': """
MARKET FOCUS (TL — Filipino):
- Ưu tiên gần gũi, gia đình, lễ Giáng sinh/sinh nhật/anniversary khi hợp; Metro Manila & đô thị lớn.
- Quà tặng cảm xúc, ý nghĩa; tránh giọng bán hàng gắt.
""",
    'my': """
MARKET FOCUS (MY — Burmese market):
- Bối cảnh Yangon/Mandalay khi hợp; quà tặng & kỷ niệm; giọng chân thành, tin cậy.
""",
    'km': """
MARKET FOCUS (KM — Khmer):
- Phnom Penh/Siem Reap khi hợp; quà tặng trong gia đình & lễ hội địa phương khi phù hợp; soft storytelling.
""",
    'lo': """
MARKET FOCUS (LO — Lào):
- Vientiane & đô thị; quà tặng tinh tế; nhịp sống gần gũi; tránh ép mua.
""",
}

MARKET_FOCUS_FALLBACK_VI = """
MARKET FOCUS (THỊ TRƯỜNG ĐÍCH — BỔ SUNG CHO MÃ CHƯA CÓ TRONG DANH SÁCH RIÊNG):
- Tự suy ra quốc gia/vùng văn hoá phù hợp mã ngôn ngữ đích và viết 2+ mỏ neo bối cảnh cụ thể (không chung chung).
- Thêm 1 insight tiêu dùng điển hình của thị trường đó (bằng tiếng Việt).
- Reframe câu chuyện khách sao cho người xem nước đích thấy đồng cảm.
"""

NON_VI_TABLE_FORMAT_INSTRUCTION = """
ĐỊNH DẠNG ĐẦU RA BẮT BUỘC CHO NGÔN NGỮ KHÁC TIẾNG VIỆT:
- Vẫn viết 1 đoạn FULL SCRIPT liền mạch trước.
- Sau đó thêm bảng đối chiếu để kiểm tra và dựng video theo format:
[CHECK_TABLE]
Phiên âm || Bản ngữ || Tiếng Việt
...
[/CHECK_TABLE]
- Trong [CHECK_TABLE] phải có ĐẦU MỤC rõ ràng theo nhóm nội dung.
- Mỗi đầu mục ghi trên 1 dòng riêng dạng: [SECTION] Tên đầu mục
- Các dòng nội dung bên dưới mỗi đầu mục mới dùng format 3 cột bằng "||".
- Mỗi dòng bảng là 1 câu/đoạn ngắn tương ứng để dễ sync subtitle.
- "Phiên âm" là bản đọc Latin hoá (romanization) của bản ngữ.
- CỰC KỲ QUAN TRỌNG: Mỗi dòng CHỈ được có ĐÚNG 3 cột phân tách bằng "||" (hai dấu sổ dọc ASCII).
  Không gộp cả 3 cột vào một ô; không dùng "—" để nối cột; không viết cả đoạn dài trong một cột rồi chèn "||" giữa câu.
"""

# Global content playbook distilled from "Content Global.mm"
# Applied for all non-VI markets (and VI source when market-localized).
GLOBAL_CONTENT_MM_PLAYBOOK = """
PLAYBOOK GLOBAL (RÚT TỪ "Content Global.mm" - ÁP DỤNG CHO MỌI THỊ TRƯỜNG NGOÀI VIỆT):

I) NHÓM MỤC TIÊU NỘI DUNG CHÍNH:
1. Bán hàng (conversion mềm): mở đầu thu hút -> câu chuyện thật -> giá trị thủ công -> CTA bình luận.
2. Tương tác (engagement): kể tình huống có cảm xúc, mời người xem cho ý kiến/ủng hộ.
3. Content theo từng sản phẩm: làm rõ ý nghĩa thiết kế + chi tiết chế tác + cảm nhận khi đeo/tặng.

II) CÔNG THỨC TRIỂN KHAI MẪU (CHỌN 1 NHÁNH PHÙ HỢP):
A. "Tự tay thiết kế / nhiều tâm huyết":
- Hook: chính người thợ cũng bất ngờ với thành phẩm.
- Nêu nỗ lực chế tác (thời gian, độ tỉ mỉ, thử thách).
- Chạm insight: không cần thương hiệu lớn vẫn có giá trị thật.
- Kết: mời người xem chia sẻ cảm nhận.

B. "Nối tiếp mẫu trước / cảm ơn khách":
- Nhắc mẫu đã giới thiệu trước đó.
- Chốt bằng phản hồi thật của khách/người xem.
- Mở rộng câu chuyện (vì sao tiếp tục sáng tạo).
- CTA cảm ơn + hỏi quan điểm.

C. "Quy trình chế tác / tay nghề":
- Trình tự: tạo hình -> mài bóng -> lắp ráp -> đính đá -> hoàn thiện.
- Giải thích vì sao từng chi tiết nhỏ quyết định chất lượng.
- Nhấn điểm khác biệt vật liệu (ví dụ bạc S925, moissanite) mà không phô trương.
- Kết: nhấn "đẹp + bền + ý nghĩa" và mời bình luận.

D. "Tâm sự nghề / giá trị thủ công":
- Kể nỗ lực phía sau video ngắn (đêm muộn, nhiều giờ tập trung).
- Tránh than thở tiêu cực; giữ tông chân thành, biết ơn.
- Liên kết tới sứ mệnh: giữ nghề thủ công truyền thống.
- CTA nhẹ, không ép.

E. "Câu chuyện quà tặng / kỷ niệm":
- Có nhân vật rõ (người tặng/người nhận), dịp tặng rõ (sinh nhật/kỷ niệm/lễ).
- Làm rõ ý nghĩa tinh thần của món quà, không chỉ giá trị vật chất.
- Chuyển mạch tự nhiên sang sản phẩm cụ thể và lý do phù hợp.
- Kết bằng câu hỏi mở để kéo tương tác.

III) RÀNG BUỘC CHẤT LƯỢNG:
- Không viết kiểu liệt kê vô hồn; phải có mạch kể chuyện.
- Luôn có 1 điểm "human truth": cảm xúc thật của người thợ hoặc khách.
- Không dùng giọng giật gân/ép mua; giữ soft-sell đúng DNA Huy Ca.
- Nếu dùng nhánh "nối tiếp", phải có câu chuyển tự nhiên nối với nội dung trước.
- Ưu tiên câu ngắn-vừa, dễ đọc subtitle và phù hợp voice-over.
"""

GLOBAL_CONTENT_MM_ONLY_POLICY = """
CHÍNH SÁCH NGUỒN TRI THỨC GLOBAL (BẮT BUỘC):
- Với mọi content GLOBAL (ngôn ngữ đích khác 'vi', hoặc bản Việt nguồn để convert cho thị trường nước ngoài),
  bạn CHỈ được dùng khung tư duy/công thức/angles nằm trong "Content Global.mm" (đã được server chuẩn hoá thành PLAYBOOK GLOBAL).
- Không dùng framework ngoài, không tự bịa thêm "công thức marketing" khác nguồn này.
- Được phép dùng: (1) transcript/source hiện tại, (2) thông tin sản phẩm thật, (3) PLAYBOOK GLOBAL từ Content Global.mm.
- Nếu thông tin nào không có trong 3 nguồn trên thì bỏ qua, không suy diễn.
"""

def build_length_requirement_instruction(output_language: str) -> str:
    if output_language == 'vi':
        return """
ĐỘ DÀI BẮT BUỘC CHO TIẾNG VIỆT:
- Viết full script khoảng 300 từ (mục tiêu 280-330 từ).
- Không viết dài quá mức cần thiết.
""".strip()
    return """
ĐỘ DÀI BẮT BUỘC CHO NGÔN NGỮ KHÁC TIẾNG VIỆT:
- Viết full script đủ dài để dùng dựng video, mục tiêu trong khoảng 350-700 từ.
- BẮT BUỘC trên 300 từ (khuyến nghị 350-700 từ). Nếu ngắn hơn 300 từ thì coi như chưa đạt yêu cầu.
""".strip()


class ContentGenerationService:
    """Service for generating marketing content using OpenAI."""
    
    def __init__(self):
        """Initialize service with Anthropic Claude."""
        import logging
        from anthropic import Anthropic
        self.logger = logging.getLogger(__name__)
        self.anthropic_key = str(getattr(settings, 'ANTHROPIC_API_KEY', '')).strip()
        
        self.client = None
        if self.anthropic_key and not self.anthropic_key.startswith('your_'):
            self.client = Anthropic(api_key=self.anthropic_key)
            
        self.deepseek_key = str(getattr(settings, 'DEEPSEEK_API_KEY', '')).strip()
        if not self.deepseek_key:
            # Fallback to env if not in settings
            import os
            self.deepseek_key = os.getenv('DEEPSEEK_API_KEY', '').strip()

        anthropic_preview = f"{self.anthropic_key[:7]}..." if self.anthropic_key else "MISSING"
        deepseek_preview = f"{self.deepseek_key[:7]}..." if self.deepseek_key else "MISSING"
        self.logger.info(f"ContentGenerationService: claude={anthropic_preview}, deepseek={deepseek_preview}")
    
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
        target_market_language: Optional[str] = None,
        translation_mode: bool = False,
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
        market_language = (target_market_language or output_language or 'vi').strip()

        # Default source_insights (used by both translation and generation paths)
        source_insights = {"keywords": [], "win_angle": "", "winning_points": []}

        # SPEED OPTIMIZATION: Start Source Insight Analysis in parallel with content generation preparation
        insights_future = None
        if not translation_mode:
            executor = ThreadPoolExecutor(max_workers=2)
            insights_future = executor.submit(self._analyze_source_insights, video_title, video_description)

        # Translation-only mode: strict translation, no content rewriting/localization expansion.
        if translation_mode:
            source_text = (video_description or video_title or "").strip()
            if output_language == 'th':
                source_text = self._shift_vi_pronouns(source_text)
            if not source_text:
                raise ValueError("Thiếu nội dung nguồn để dịch.")

            if output_language == 'vi':
                # Detect if source is foreign
                source_lang = None
                for lang_code in ['th', 'ja', 'ko', 'zh', 'my', 'km', 'lo']:
                    if self._looks_like_target_language(source_text, lang_code):
                        source_lang = lang_code
                        break
                
                self.logger.info(f"Translation mode (VI output): detected_source_lang={source_lang}")
                
                if source_lang:
                    # SPEED OPTIMIZATION: One single call for both translation and alignment
                    result_data = self._translate_and_align_foreign_to_vi(
                        source_text=source_text,
                        source_lang=source_lang,
                        fast_mode=True
                    )
                    translated_script = result_data.get('script', '')
                    verification_rows = result_data.get('verification_rows', [])
                    
                    if not translated_script:
                        raise Exception("Không thể dịch nội dung sang tiếng Việt.")
                    
                    claude_result = self._parse_response(translated_script, output_language=source_lang)
                    claude_result['verification_rows'] = verification_rows
                else:
                    # VI -> VI: Regular pass-through
                    claude_result = self._parse_response(source_text, output_language='vi')
                
                claude_result['source_insights'] = source_insights
                return claude_result
            else:
                # OPTIMIZATION: Run translation and table-row generation IN PARALLEL.
                # They are independent — translation uses source_text, rows use source_vietnamese_text.
                translated_script = None
                verification_rows = []

                def _do_translate():
                    return self._translate_content_strict(
                        source_text=source_text,
                        output_language=output_language,
                        prefer_fast=True
                    )

                def _do_rows():
                    return self._generate_verification_rows_with_ai(
                        script=source_text,  # Use source as anchor for translation mode
                        output_language=output_language,
                        source_vietnamese_text=source_text,
                        fast_mode=True,
                        translation_mode=True
                    )

                try:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        future_translate = executor.submit(_do_translate)
                        future_rows = executor.submit(_do_rows)

                        translated_script = future_translate.result(timeout=120)
                        verification_rows = future_rows.result(timeout=120)
                except RuntimeError as e:
                    if 'interpreter shutdown' in str(e):
                        self.logger.warning("ThreadPoolExecutor failed due to interpreter shutdown, falling back to sequential.")
                        translated_script = _do_translate()
                        verification_rows = _do_rows()
                    else:
                        raise

                if not translated_script:
                    raise Exception("Không thể dịch nội dung. Vui lòng kiểm tra lại API Key hoặc kết nối mạng.")
                claude_result = self._parse_response(translated_script, output_language=output_language)
                claude_result['verification_rows'] = verification_rows or []

            claude_result['source_insights'] = source_insights
            return claude_result

        if output_language == 'th':
            video_description = self._shift_vi_pronouns(video_description)
            video_title = self._shift_vi_pronouns(video_title)

        # Wait for insights if they were started in parallel
        source_insights = {"keywords": [], "win_angle": "", "winning_points": []}
        if insights_future:
            try:
                source_insights = insights_future.result(timeout=15)
            except Exception as e:
                self.logger.warning(f"Insights analysis timed out or failed: {e}")

        prompt = self._build_prompt(
            video_description=video_description,
            video_title=video_title,
            template=template,
            brand_name=brand_name,
            industry=industry,
            additional_context=additional_context,
            product_info=product_info,
            output_language=output_language,
            market_language=market_language,
            source_insights=source_insights,
        )

        prefer_fast_generation = translation_mode or output_language in {'th', 'id', 'tl', 'my', 'km', 'lo'}
        claude_result = self._call_ai_service(
            prompt,
            output_language=output_language,
            prefer_fast=prefer_fast_generation,
            timeout_seconds=60 if prefer_fast_generation else 90,
        )
        if claude_result:
            # Shift pronouns for Thai/Global market in the result structure if needed
            effective_source = source_text if 'source_text' in locals() else video_description
            if output_language == 'th':
                if 'vietnamese' in claude_result:
                    claude_result['vietnamese'] = self._shift_vi_pronouns(claude_result['vietnamese'])
                source_text_for_rows = self._shift_vi_pronouns(effective_source)
            else:
                source_text_for_rows = effective_source

            claude_result = self._ensure_min_length_for_non_vi(
                claude_result,
                output_language,
                prompt,
                fast_mode=prefer_fast_generation
            )
            claude_result['source_insights'] = source_insights
            # Always rebuild verification rows by one unified pipeline for non-VI.
            # Do not trust ad-hoc table text emitted inside the model script.
            # Always rebuild verification rows by one unified pipeline for non-VI or foreign-source VI.
            # Do not trust ad-hoc table text emitted inside the model script.
            source_lang_for_table = output_language
            if output_language == 'vi':
                # Detect if source is foreign
                source_text_for_detection = (video_description or video_title or "")
                for lang_code in ['th', 'ja', 'ko', 'zh', 'my', 'km', 'lo']:
                    if self._looks_like_target_language(source_text_for_detection, lang_code):
                        source_lang_for_table = lang_code
                        break

            if source_lang_for_table != 'vi':
                # If target is VI but source is foreign, we want to anchor the table to the foreign source
                is_vi_output = (output_language == 'vi')
                claude_result['verification_rows'] = self._generate_verification_rows_with_ai(
                    script=video_description if is_vi_output else claude_result.get('script', ''),
                    output_language=source_lang_for_table,
                    source_vietnamese_text=claude_result.get('script', '') if is_vi_output else (video_description or video_title),
                    fast_mode=translation_mode,
                    translation_mode=translation_mode,
                    anchor_to_native=is_vi_output # If target is VI but source is Global, anchor to Global source
                )
            return claude_result

        raise Exception("Claude không phản hồi hoặc cấu hình chưa đúng.")

    def _call_ai_service(
        self,
        prompt: str,
        output_language: str = 'vi',
        prefer_fast: bool = False,
        timeout_seconds: int = 60,
    ) -> Optional[Dict[str, str]]:
        """Gọi AI Service (Ưu tiên DeepSeek, fallback Claude) cho generate content."""
        system_msg = "Bạn là copywriter. Nhiệm vụ: đọc content gốc, lấy CÂU CHUYỆN/CHỦ ĐỀ từ đó, rồi viết lại theo cấu trúc 6 bước Global (lý do → câu chuyện → tâm sự → yêu cầu khách → sản phẩm (chế tác/ý nghĩa, KHÔNG spec) → chúc khách). Phần Outro đã có sẵn video riêng nên KHÔNG viết tự xưng thương hiệu. Viết liền mạch. KHÔNG hashtag, KHÔNG emoji, KHÔNG dấu chấm than, KHÔNG liệt kê spec sản phẩm. Luôn xưng Tôi - Bạn."
        
        # 1. Try DeepSeek first
        content = self._call_deepseek_raw(
            prompt=prompt,
            system_msg=system_msg,
            timeout=timeout_seconds,
            log_prefix="Content generation (DeepSeek)"
        )
        
        # 2. Fallback to Claude if DeepSeek fails
        if not content and self.client:
            self.logger.info("DeepSeek failed for content generation, falling back to Claude.")
            if prefer_fast:
                models = ['claude-haiku-4-5', 'claude-sonnet-4-6']
            else:
                models = ['claude-sonnet-4-6', 'claude-haiku-4-5', 'claude-opus-4-7']
                
            content = self._call_claude_raw(
                prompt=prompt,
                system_msg=system_msg,
                models=models,
                temperature=0.5,
                max_tokens=4096,
                timeout=timeout_seconds,
                log_prefix="Content generation (Claude Fallback)"
            )
            
        if not content:
            return None
        return self._parse_response(content, output_language=output_language)

    def _call_claude_raw(
        self,
        prompt: str,
        system_msg: str,
        models: List[str],
        temperature: float,
        max_tokens: int,
        timeout: int,
        log_prefix: str,
    ) -> Optional[str]:
        """Call Claude chat completions and return raw content."""
        if not self.client:
            return None

        for model in models:
            try:
                params = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_msg,
                    "messages": [{"role": "user", "content": prompt}]
                }
                # Claude 4.7 Opus and some newer models deprecate temperature in favor of 
                # deterministic or dynamic sampling. Remove it to avoid 400 errors.
                if "opus-4-7" not in model:
                    params["temperature"] = temperature

                response = self.client.messages.create(**params, timeout=timeout)
                content = response.content[0].text
                self.logger.info(f"{log_prefix}: Claude {model} success")
                return content
            except Exception as e:
                self.logger.error(f"{log_prefix}: Claude {model} error: {e}")
                continue
        return None

    def _extract_json_array(self, text: str) -> List:
        """Robustly extract a JSON array from AI output."""
        if not text:
            return []
        s = text.strip()
        try:
            # 1. Try direct parse
            return json.loads(s)
        except:
            pass

        # 2. Try cleaning markdown and finding [ ]
        cleaned = s.replace('```json', '').replace('```', '').strip()
        start = cleaned.find('[')
        end = cleaned.rfind(']')
        if start != -1 and end != -1:
            try:
                return json.loads(cleaned[start:end + 1])
            except Exception as e:
                self.logger.warning(f"Failed to parse JSON array between markers: {e}")
        
        return []

    def _extract_json_dict(self, text: str) -> Dict:
        """Robustly extract a JSON dictionary from AI output."""
        if not text:
            return {}
        s = text.strip()
        try:
            return json.loads(s)
        except:
            pass

        cleaned = s.replace('```json', '').replace('```', '').strip()
        
        # Try to find the first '{' and last '}'
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start != -1 and end != -1:
            content = cleaned[start:end + 1]
            try:
                return json.loads(content)
            except Exception as e:
                # Last resort: try a very aggressive cleanup for common AI mistakes
                # (e.g., unescaped newlines in strings)
                try:
                    # Replace literal newlines inside quotes with \n
                    # This is risky but often helps with malformed AI JSON
                    fixed = re.sub(r'(?<=[:\s,\[])"(.*?)"(?=[\s,\]\}])', 
                                   lambda m: m.group(0).replace('\n', '\\n'), 
                                   content, flags=re.DOTALL)
                    return json.loads(fixed)
                except:
                    self.logger.warning(f"Failed to parse JSON dict between markers: {e}")
        
        return {}

    def _call_deepseek_raw(
        self,
        prompt: str,
        system_msg: str,
        model: str = "deepseek-v4-flash",
        temperature: float = 0.1,
        max_tokens: int = 2048,
        timeout: int = 60,
        log_prefix: str = "DeepSeek",
    ) -> Optional[str]:
        """
        Call DeepSeek API using requests. Trả None khi lỗi.

        GIỮ NGUYÊN contract cũ (None khi lỗi) vì hàm này có ~14 nơi gọi, nhiều nơi dựa vào
        `if not content:` để fallback sang Claude. Bên nào cần biết LOẠI lỗi (hiện tại: PAAST)
        thì gọi `_call_deepseek_checked` bên dưới để nhận DeepSeekError có phân loại.
        """
        try:
            return self._call_deepseek_checked(
                prompt=prompt,
                system_msg=system_msg,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                log_prefix=log_prefix,
            )
        except DeepSeekError:
            # _call_deepseek_checked đã log chi tiết loại lỗi rồi, không log lặp.
            return None

    def _call_deepseek_checked(
        self,
        prompt: str,
        system_msg: str,
        model: str = "deepseek-v4-flash",
        temperature: float = 0.1,
        max_tokens: int = 2048,
        timeout: int = 60,
        log_prefix: str = "DeepSeek",
        rate_limit_retries: int = 2,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Gọi DeepSeek và NÉM DeepSeekError có phân loại thay vì trả None.

        Tự xử lý riêng 429: 5 lệnh gọi song song trên cùng 1 API key (xem PaastAnalysisService)
        rất dễ chạm rate limit, mà 429 thì chờ một nhịp là qua — nên backoff tại chỗ (tôn trọng
        `Retry-After` nếu server có trả) thay vì để cả lượt phân tích hỏng rồi retry từ đầu.
        Backoff này CHỈ dành cho 429; timeout/5xx để tầng trên quyết định thử lại.
        """
        if not self.deepseek_key:
            raise DeepSeekError('no_key', 'Chưa cấu hình DEEPSEEK_API_KEY')

        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.deepseek_key}"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        if extra_params:
            payload.update(extra_params)

        for attempt in range(rate_limit_retries + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            except requests.exceptions.Timeout as e:
                self.logger.error(f"{log_prefix}: DeepSeek {model} TIMEOUT sau {timeout}s: {e}")
                raise DeepSeekError('timeout', f'DeepSeek không trả lời trong {timeout}s') from e
            except requests.exceptions.RequestException as e:
                self.logger.error(f"{log_prefix}: DeepSeek {model} lỗi mạng: {e}")
                raise DeepSeekError('network', f'Lỗi mạng khi gọi DeepSeek: {e}') from e

            status = response.status_code

            if status == 429:
                if attempt < rate_limit_retries:
                    # Ưu tiên Retry-After của server; không có thì backoff luỹ thừa 2s, 4s.
                    try:
                        wait_s = float(response.headers.get('Retry-After', ''))
                    except ValueError:
                        wait_s = 0.0
                    if wait_s <= 0:
                        wait_s = 2.0 * (2 ** attempt)
                    self.logger.warning(
                        f"{log_prefix}: DeepSeek {model} RATE LIMIT (429), "
                        f"chờ {wait_s:.1f}s rồi thử lại ({attempt + 1}/{rate_limit_retries})"
                    )
                    time.sleep(wait_s)
                    continue
                self.logger.error(f"{log_prefix}: DeepSeek {model} RATE LIMIT (429) — hết lượt backoff")
                raise DeepSeekError('rate_limit', 'DeepSeek đang giới hạn tần suất (429)', 429)

            if 500 <= status < 600:
                self.logger.error(f"{log_prefix}: DeepSeek {model} lỗi phía server ({status})")
                raise DeepSeekError('server', f'DeepSeek lỗi phía server ({status})', status)

            if status >= 400:
                body = (response.text or '')[:300]
                self.logger.error(f"{log_prefix}: DeepSeek {model} lỗi request ({status}): {body}")
                raise DeepSeekError('client', f'DeepSeek từ chối request ({status}): {body}', status)

            try:
                result = response.json()
                choice = result['choices'][0]
                content = choice['message']['content']
            except (ValueError, KeyError, IndexError, TypeError) as e:
                self.logger.error(f"{log_prefix}: DeepSeek {model} response không đúng shape: {e}")
                raise DeepSeekError('parse', f'Response DeepSeek không đúng shape: {e}') from e

            if not content:
                # finish_reason='length' = token suy luận nội bộ ăn hết max_tokens trước khi kịp
                # sinh nội dung. Đây là lỗi ngân sách token, KHÔNG phải timeout — phân biệt rõ để
                # không bị chẩn đoán nhầm thành "chậm" như trước.
                finish_reason = choice.get('finish_reason', 'unknown')
                usage = result.get('usage', {})
                self.logger.error(
                    f"{log_prefix}: DeepSeek {model} trả content RỖNG "
                    f"(finish_reason={finish_reason}, max_tokens={max_tokens}, usage={usage})"
                )
                raise DeepSeekError(
                    'server' if finish_reason == 'length' else 'parse',
                    f'DeepSeek trả nội dung rỗng (finish_reason={finish_reason}, max_tokens={max_tokens})',
                )

            # Log finish_reason + token thực dùng ngay cả khi THÀNH CÔNG: đây là số liệu duy nhất
            # để biết max_tokens đang cấp thừa hay đang sát trần (reasoning_tokens bị trừ vào
            # chính max_tokens và dao động rất lớn giữa các lần gọi cùng input).
            usage = result.get('usage', {}) or {}
            reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
            self.logger.info(
                f"{log_prefix}: DeepSeek {model} success "
                f"(finish_reason={choice.get('finish_reason')}, max_tokens={max_tokens}, "
                f"completion_tokens={usage.get('completion_tokens')}, reasoning_tokens={reasoning})"
            )
            return content

        raise DeepSeekError('rate_limit', 'DeepSeek đang giới hạn tần suất (429)', 429)

    def _translate_content_strict(
        self,
        source_text: str,
        output_language: str,
        prefer_fast: bool = True
    ) -> Optional[str]:
        """
        Strict translation mode for existing content:
        keep meaning/facts/order, avoid creative rewriting.
        """
        if output_language == 'vi':
            # Check if source is foreign → use dedicated Foreign-to-VI translation
            is_foreign = False
            detected_lang = None
            for lang_code in ['th', 'ja', 'ko', 'zh', 'my', 'km', 'lo']:
                if self._looks_like_target_language(source_text, lang_code):
                    is_foreign = True
                    detected_lang = lang_code
                    break
            if not is_foreign:
                return source_text
            # Foreign → Vietnamese: use dedicated prompt
            translated = self._translate_foreign_to_vi(source_text, detected_lang, prefer_fast)
            if translated:
                translated = self._sanitize_thai_output(translated) if detected_lang == 'th' else translated
            return translated

        lang_name_map = {
            'th': 'Thai (ภาษาไทย)',
            'ja': 'Japanese (日本語)',
            'ko': 'Korean (한국어)',
            'zh': 'Chinese Simplified (中文简体)',
            'zh-TW': 'Chinese Traditional (中文繁體)',
            'en': 'English',
            'id': 'Bahasa Indonesia',
            'ms': 'Bahasa Melayu',
            'tl': 'Tagalog/Filipino',
            'my': 'Burmese',
            'km': 'Khmer',
            'lo': 'Lao',
        }
        target_lang_name = lang_name_map.get(output_language, output_language)

        prompt = f"""
Bạn là biên dịch viên chuyên dịch kịch bản video bán hàng.
Hãy dịch NỘI DUNG NGUỒN sang ngôn ngữ đích: {target_lang_name}.

RÀNG BUỘC BẮT BUỘC:
- Dịch sát nghĩa, giữ đúng thông tin, đúng thứ tự ý.
- Không tự thêm chi tiết mới, không đổi bối cảnh, không đổi nhân vật.
- Không rút gọn quá mức, không viết lại theo phong cách sáng tác.
- Giữ tông tự nhiên như người bản địa nói hàng ngày.
- BẮT BUỘC trả về 100% bằng {target_lang_name}, không được giữ nguyên tiếng Việt.
- Nếu ngôn ngữ đích là tiếng Thái (th): dùng giọng nam trong cách diễn đạt (ưu tiên hạt từ nam như "ครับ", tránh hạt từ nữ như "ค่ะ" khi có dùng).
- Chỉ trả về bản dịch hoàn chỉnh, không markdown, không giải thích.

NỘI DUNG NGUỒN:
\"\"\"
{source_text}
\"\"\"

BẮT BUỘC ĐỐI VỚI TIẾNG THÁI (TH):
{THAI_PRONOUN_LOCK}
- Chỉnh sửa văn phong cho "Thái Hoá", tăng độ "sến sến" và cảm xúc (heart-touching) đặc trưng quảng cáo Thái.
- Sử dụng ngôn ngữ tự nhiên như người Thái bản địa nói chuyện.
""".strip()

        # Strict translation: Use DeepSeek as primary for translation quality/cost balance.
        # Fallback to Claude if DeepSeek fails.
        system_msg = (
            "Bạn là chuyên gia bản địa hoá (localization) người Thái. "
            "Tuyệt đối không dùng ฮุยกา, hãy dùng Phom (tôi). "
            "Tuyệt đối không xưng 'cậu' (เธอ), hãy dùng Khun (bạn) lịch sự."
            if output_language == 'th' else
            "Bạn là biên dịch viên chất lượng cao. Ưu tiên độ chính xác nghĩa hơn văn phong hoa mỹ."
        )

        translated = self._call_deepseek_raw(
            prompt=prompt,
            system_msg=system_msg,
            timeout=30 if prefer_fast else 50,
            log_prefix="Strict translation (DeepSeek)"
        )

        if not translated:
            self.logger.info("DeepSeek failed or not configured, falling back to Claude for translation.")
            if prefer_fast:
                models = ['claude-haiku-4-5', 'claude-sonnet-4-6']
            else:
                models = ['claude-sonnet-4-6', 'claude-haiku-4-5']
            
            translated = self._call_claude_raw(
                prompt=prompt,
                system_msg=system_msg,
                models=models,
                temperature=0.1,
                max_tokens=1024,
                timeout=25 if prefer_fast else 40,
                log_prefix="Strict translation (Claude)"
            )
        if not translated:
            return None
        if translated and self._looks_like_target_language(translated, output_language):
            return translated

        # Retry once with stricter instruction if output language is wrong/mixed.
        self.logger.warning(
            f"Strict translation language mismatch for target={output_language}. Retrying with hard language lock."
        )
        hard_prompt = f"""
HARD REQUIREMENT:
- Kết quả phải là 100% {target_lang_name}.
- Không dùng tiếng Việt.
- Không giữ nguyên câu nguồn.
{THAI_PRONOUN_LOCK if output_language == 'th' else ''}
- Dịch thoát ý, tự nhiên như người bản địa.

Nội dung cần dịch:
\"\"\"
{source_text}
\"\"\"
""".strip()
        retried = self._call_claude_raw(
            prompt=hard_prompt,
            system_msg=f"Bạn là dịch giả chuyên nghiệp. Chỉ trả về {target_lang_name}.",
            models=['claude-sonnet-4-6', 'claude-haiku-4-5'],
            temperature=0.0,
            max_tokens=4096,
            timeout=45,
            log_prefix="Strict translation retry"
        )
        if retried and self._looks_like_target_language(retried, output_language):
            return retried
        return retried or translated

    def _looks_like_target_language(self, text: str, output_language: str) -> bool:
        s = (text or '').strip()
        if not s:
            return False
        compact = re.sub(r'\s+', '', s)
        if not compact:
            return False

        checks = {
            'th': r'[\u0E00-\u0E7F]',
            'ja': r'[\u3040-\u30FF\u4E00-\u9FFF]',
            'ko': r'[\uAC00-\uD7AF]',
            'zh': r'[\u4E00-\u9FFF]',
            'zh-TW': r'[\u4E00-\u9FFF]',
            'my': r'[\u1000-\u109F]',
            'km': r'[\u1780-\u17FF]',
            'lo': r'[\u0E80-\u0EFF]',
        }
        pattern = checks.get(output_language)
        if pattern:
            matched = re.findall(pattern, compact)
            # enough target-script density to consider valid translation
            return (len(matched) / max(1, len(compact))) >= 0.08

        # Latin-script languages: at least mostly non-Vietnamese diacritics usage isn't reliable.
        # Use a weak check: output should contain letters and not be identical to source-like Vietnamese cue words.
        vi_cues = ('không', 'và', 'của', 'những', 'được', 'một', 'với')
        lower = s.lower()
        if any(cue in lower for cue in vi_cues) and output_language in {'en', 'id', 'ms', 'tl'}:
            return False
        return bool(re.search(r'[A-Za-z]', s))


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
        market_language: Optional[str] = None,
        source_insights: Optional[Dict] = None,
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
Keep the sincere and humble storytelling style, just adapt to {lang_display} naturally.
"""

        effective_market_language = (market_language or output_language or 'vi').strip()
        enforced_style_block = self._build_enforced_style_instruction(
            output_language=output_language,
            market_language=effective_market_language
        )
        extra_context = ""
        if additional_context:
            extra_context = f"""

YÊU CẦU BỔ SUNG (không được trái với quy tắc bắt buộc):
{additional_context}
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

        insights_context = ""
        if source_insights:
            kw = ', '.join(source_insights.get('keywords', [])[:12])
            points = source_insights.get('winning_points', [])[:6]
            points_block = '\n'.join([f"- {p}" for p in points]) if points else "- (không xác định)"
            insights_context = f"""
# PHÂN TÍCH NỘI DUNG GỐC (đã xác định từ transcript/video win):
- Từ khoá nổi bật: {kw or '(không có)'}
- Win angle chính: {source_insights.get('win_angle', '(không xác định)')}
- Các điểm ăn view/chuyển đổi dễ:
{points_block}
⚠️ Bắt buộc tận dụng các điểm trên khi viết content mới, nhưng vẫn tự nhiên như văn nói.
"""

        length_instruction = build_length_requirement_instruction(output_language)
        is_market_localized_vi = output_language == 'vi' and effective_market_language != 'vi'
        is_global_content = output_language != 'vi' or is_market_localized_vi
        playbook_context = ""
        if is_global_content:
            playbook_context = f"""
# PLAYBOOK GLOBAL CỐT LÕI (bắt buộc áp dụng cho luồng global/non-vi):
{GLOBAL_CONTENT_MM_PLAYBOOK}

# CHÍNH SÁCH "CHỈ HỌC TỪ CONTENT GLOBAL.MM":
{GLOBAL_CONTENT_MM_ONLY_POLICY}
"""
        if is_global_content:
            thai_market = effective_market_language == 'th'
            global_focus_guard = """
- XƯNG HÔ BẮT BUỘC: dùng ngôi "tôi" (first-person) xuyên suốt, không xưng "Huy Ca" trong phần thân bài.
- TĂNG MÔ TẢ ĐỘ TỈ MỈ: phải có chi tiết cụ thể về thao tác thủ công, thời gian tập trung, độ khó, độ chính xác từng công đoạn.
- GIẢM MÔ TẢ SẢN PHẨM THUẦN TUÝ: không dồn content vào việc ca ngợi chiếc nhẫn; thay vào đó nhấn quá trình làm nghề, triết lý và giá trị thủ công.
- Tỷ trọng khuyến nghị: >=60% nội dung nói về con người/thao tác/chặng làm nghề, <=40% nói trực tiếp về sản phẩm.
- CẤM tuyến romance trong GLOBAL: không dùng các mô-típ "người yêu", "cặp đôi", "tỏ tình", "quà cho người yêu". Thay bằng ngữ cảnh tặng quà mang tính gia đình/cá nhân/cột mốc bản thân hoặc đơn thuần là trân trọng đồ thủ công.
"""
            if thai_market:
                global_focus_guard = """
- XƯNG HÔ BẮT BUỘC: {THAI_PRONOUN_LOCK.strip()}
- GIỌNG THÁI ƯU TIÊN: "Sến sến", mềm mại, đánh mạnh vào cảm xúc và lòng trắc ẩn; không bi lụy, không áp lực chốt đơn.
- BẮT BUỘC MỎ NEO THỊ TRƯỜNG THÁI: lồng ghép địa danh Thái (Bangkok, Yaowarat...) và insight tiêu dùng bản địa (thích quà tặng có ý nghĩa tinh thần, tin vào sự chân thành).
- NHẤN BÁN HÀNG MỀM: Kết nối giữa giá trị sản phẩm (vật liệu, thủ công) và giá trị tinh thần (มงคล) một cách mượt mà.
- TỶ TRỌNG NỘI DUNG: 50% cảm xúc/câu chuyện, 35% chi tiết sản phẩm & thủ công, 15% tương tác chân thành.
- CẤM tuyến romance trong GLOBAL: tập trung vào tình cảm gia đình, lòng biết ơn mẹ cha hoặc cột mốc cá nhân.
"""
            context_rule_block = f"""
QUY TẮC QUAN TRỌNG — ĐỌC TRƯỚC KHI VIẾT (BẢN VIỆT CHO THỊ TRƯỜNG {effective_market_language.upper()}):
- Giữ ý nghĩa cốt lõi và DNA câu chuyện từ bài mẫu, nhưng PHẢI địa phương hoá bối cảnh theo thị trường {effective_market_language.upper()}.
- Được phép chuyển địa danh/hoàn cảnh/insight đời sống sang bối cảnh bản địa phù hợp văn hoá thị trường đích.
- Tuyệt đối không bê nguyên cụm bối cảnh Việt nếu làm giảm độ tự nhiên với thị trường đích.
- Vẫn phải đúng sự thật sản phẩm, không bịa thông số kỹ thuật.
- Bắt buộc có ít nhất 2 “mỏ neo văn hoá” theo thị trường {effective_market_language.upper()} và 1 câu insight tiêu dùng đúng thị trường đó (bằng tiếng Việt).
- Câu chuyện khách phải hợp lý với người xem ở thị trường đích (người bản địa / người Việt ở nước đó / tặng quà cho người ở đó) — không chỉ nhập vai như khách trong Việt Nam trừ khi transcript gốc bắt buộc.
{global_focus_guard}
"""
            task_line = "NHIỆM VỤ: Dựa trên BÀI MẪU + THÔNG TIN SẢN PHẨM + PLAYBOOK GLOBAL để viết content global theo trục tay nghề thủ công, sự tỉ mỉ và giá trị truyền thống."
            structure_block = """
BỐ CỤC GLOBAL ƯU TIÊN "SỰ TỈ MỈ - THỦ CÔNG" (BẮT BUỘC):
1) HOOK NGƯỜI THỢ: mở bằng điểm bất ngờ/ấn tượng từ chính người làm.
2) CÔNG SỨC THỰC TẾ: nêu thời gian/độ tập trung/độ khó chế tác (thật, không phóng đại).
3) ĐỘNG CƠ THIẾT KẾ: vì sao tạo mẫu này, khác gì mẫu đại trà.
4) GIÁ TRỊ THỦ CÔNG: nhấn vẻ đẹp tay nghề và giá trị truyền thống, không dựa vào tên thương hiệu.
5) ĐIỂM NỔI BẬT SẢN PHẨM: vật liệu/chi tiết nổi bật từ dữ liệu thật.
6) KẾT MỞ + CTA: câu hỏi mở + mời bình luận tự nhiên.
"""
            if thai_market:
                task_line = "NHIỆM VỤ: Dựa trên BÀI MẪU + THÔNG TIN SẢN PHẨM + PLAYBOOK GLOBAL để viết content cho thị trường Thái theo hướng mềm mại, cảm xúc, gắn bối cảnh tiêu dùng bản địa và bán hàng mềm."
                structure_block = """
BỐ CỤC RIÊNG CHO THỊ TRƯỜNG THÁI (BẮT BUỘC):
1) HOOK CẢM XÚC NHẸ: mở bằng một lát cắt đời sống ở Thái (địa danh/nhịp sống/lễ dịp) để tạo đồng cảm.
2) CÂU CHUYỆN SẾN NHẸ: cảm xúc đời thường vừa đủ, không bi lụy, không kéo dài quá mức.
3) GIỚI THIỆU SẢN PHẨM VỪA ĐỦ: nêu chất liệu + ý nghĩa sản phẩm + hoàn cảnh đeo/tặng phù hợp.
4) GIÁ TRỊ THỦ CÔNG NGẮN GỌN: nhắc tay nghề/chất lượng ở mức hỗ trợ niềm tin, không dồn kỹ thuật.
5) CTA MỀM: mời bạn chia sẻ cảm nhận/hoàn cảnh tương tự, giữ tông thân thiện.
"""
            narrator_rule = '- Xưng "tôi", gọi người xem là "bạn"; không xưng "Huy Ca" trong thân bài global.'
        else:
            context_rule_block = """
QUY TẮC QUAN TRỌNG — ĐỌC TRƯỚC KHI VIẾT:
- Bài mẫu CÓ câu chuyện gì, nhân vật gì, tình huống gì → dùng ĐÚNG thông tin đó, KHÔNG được bịa thêm địa danh/nhân vật/tình huống không có trong bài mẫu
- Ví dụ bài mẫu nói "chị khách từ Bắc Linh xuống đặt nhẫn đôi" → câu chuyện phải là "chị từ Bắc Linh", KHÔNG được tự đổi thành nơi khác
- Ví dụ bài mẫu nói về "nhẫn bắt sáng khi gặp ánh nắng" → ghi nhận đặc điểm đó, lồng vào phần SP
- Chuyển bối cảnh bài mẫu thành câu chuyện khách tìm đến Huy Ca
"""
            task_line = "NHIỆM VỤ: Đọc bài mẫu → hiểu BỐI CẢNH và CÂU CHUYỆN trong đó → viết content mới cho sản phẩm trong \"THÔNG TIN SẢN PHẨM THẬT\" theo 7 bước KOC bên dưới."
            structure_block = """
BỐ CỤC 6 BƯỚC (BẮT BUỘC, theo đúng thứ tự):
1. MỞ ĐẦU = LÝ DO: Hook lấy từ chủ đề/điểm nổi bật của bài mẫu
2. CÂU CHUYỆN: Lấy đúng bối cảnh/nhân vật từ bài mẫu → viết thành câu chuyện khách tìm đến Huy Ca
3. HUY CA TÂM SỰ: Cảm nhận của Huy Ca về câu chuyện khách
4. YÊU CẦU CỦA KHÁCH: Khách muốn gì → đặt sản phẩm trong "THÔNG TIN SẢN PHẨM THẬT"
5. NÓI QUA VỀ SẢN PHẨM: Kể kiểu thợ tâm sự — công đoạn chế tác, thời gian làm, ý nghĩa đặc biệt (dùng thông tin từ bài mẫu nếu có, không liệt kê spec kỹ thuật)
6. KÉO TƯƠNG TÁC + CHÚC KHÁCH:
   - Có ít nhất 1 câu hỏi mở để mời người xem nêu quan điểm.
   - Có 1 câu mời bình luận tự nhiên theo kiểu "anh chị bình luận cho Huy Ca biết nhé".
   - Kết bằng 1 câu chúc nhẹ nhàng.
"""
            narrator_rule = '- Xưng "Huy Ca", gọi khách "anh chị"/"mọi người".'

        prompt = f"""{lang_instruction}
{enforced_style_block}
{length_instruction}

BÀI MẪU (đọc kỹ — đây là nguồn BỐI CẢNH và CÂU CHUYỆN để viết content mới):
\"\"\"
{video_description}
\"\"\"

{product_context}
{insights_context}
{playbook_context}

{task_line}

{context_rule_block}

{structure_block}

⛔ TUYỆT ĐỐI KHÔNG ĐƯỢC VIẾT các câu sau ở bất kỳ vị trí nào:
- "Mình là Huy Ca đến từ Viễn Chí Bảo"
- "Mình là HuyK đến từ Viễn Chí Bảo"
- "Huy Ca đến từ Viễn Chí Bảo"
- Bất kỳ câu tự xưng tên + thương hiệu nào
Lý do: Video đã có sẵn phần Outro riêng để giới thiệu, KHÔNG được lặp lại.

QUY TẮC:
{narrator_rule}
- Giọng chân thật, trầm ấm, tử tế như thợ tâm sự
- KHÔNG dùng "giá sốc", "cao cấp", "số 1", dấu chấm than, hashtag, icon
- TUYỆT ĐỐI KHÔNG được nhắc thương hiệu "Viễn Chí Bảo" ở bất kỳ vị trí nào trong bài.
- Viết liền mạch 1 đoạn văn nói cho video. KHÔNG xuống dòng, KHÔNG đánh số bước
- Câu cuối cùng phải là câu CHÚC KHÁCH, không thêm bất cứ thứ gì sau đó
{extra_context}

Trả về CHỈ bài viết, bắt đầu ngay câu đầu tiên.
"""
        return prompt.strip()

    def _analyze_source_insights(self, video_title: str, video_description: str) -> Dict:
        """
        Extract source keywords + winning angles from original transcript/text.
        """
        text = f"{video_title or ''} {video_description or ''}".strip()
        if not text:
            return {"keywords": [], "win_angle": "", "winning_points": []}

        # 1) Try LLM extraction first
        llm_result = self._analyze_source_with_llm(text)
        if llm_result:
            return llm_result

        # 2) Heuristic fallback
        return self._analyze_source_heuristic(text)

    def _analyze_source_with_llm(self, text: str) -> Optional[Dict]:
        prompt = (
            "Phân tích nội dung video win và trả về JSON duy nhất với schema:\n"
            "{"
            "\"keywords\": [\"...\"], "
            "\"win_angle\": \"...\", "
            "\"winning_points\": [\"...\", \"...\"]"
            "}\n"
            "- keywords: 8-15 từ khoá cụ thể, có giá trị làm content\n"
            "- win_angle: 1 câu ngắn mô tả góc triển khai dễ thắng nhất\n"
            "- winning_points: 4-7 bullet mô tả lý do ăn điểm\n"
            "- Không markdown, không giải thích ngoài JSON.\n\n"
            f"NỘI DUNG:\n{text}"
        )
        system_msg = "Bạn là chuyên gia content strategy. Trả về JSON chuẩn theo yêu cầu."
        
        # 1. Try DeepSeek first
        txt = self._call_deepseek_raw(
            prompt=prompt,
            system_msg=system_msg,
            timeout=25,
            log_prefix="Source insight analysis (DeepSeek)"
        )
        
        # 2. Fallback to Claude
        if not txt:
            txt = self._call_claude_raw(
                prompt=prompt,
                system_msg=system_msg,
                models=['claude-haiku-4-5', 'claude-sonnet-4-6'],
                temperature=0.2,
                max_tokens=1200,
                timeout=20,
                log_prefix="Source insight analysis (Claude Fallback)"
            )
        if txt:
            try:
                cleaned = txt.replace('```json', '').replace('```', '').strip()
                start = cleaned.find('{')
                end = cleaned.rfind('}')
                if start != -1 and end != -1:
                    obj = json.loads(cleaned[start:end + 1])
                    keywords = [str(x).strip() for x in obj.get('keywords', []) if str(x).strip()]
                    win_angle = str(obj.get('win_angle', '')).strip()
                    winning_points = [str(x).strip() for x in obj.get('winning_points', []) if str(x).strip()]
                    if keywords or win_angle or winning_points:
                        return {
                            "keywords": keywords[:15],
                            "win_angle": win_angle,
                            "winning_points": winning_points[:7]
                        }
            except Exception:
                pass
        return None

    def _analyze_source_heuristic(self, text: str) -> Dict:
        tokens = re.findall(r"[A-Za-zÀ-ỹ0-9]+", text.lower())
        stop = {
            'và', 'là', 'của', 'cho', 'một', 'những', 'các', 'đã', 'đang', 'với',
            'thì', 'mà', 'này', 'đó', 'rất', 'được', 'trong', 'khi', 'để', 'từ',
            'anh', 'chị', 'em', 'mình', 'cái', 'chiếc', 'nó', 'có', 'không'
        }
        words = [w for w in tokens if len(w) >= 3 and w not in stop]
        freq = Counter(words).most_common(12)
        keywords = [w for w, _ in freq]
        win_angle = "Khai thác điểm cảm xúc + công dụng thực tế của sản phẩm trong bối cảnh đời thường."
        winning_points = [
            "Có yếu tố cảm xúc cá nhân, dễ tạo đồng cảm.",
            "Có mô tả trực tiếp về sản phẩm, dễ chuyển thành điểm bán hàng.",
            "Ngôn ngữ gần gũi, phù hợp voice video."
        ]
        return {
            "keywords": keywords,
            "win_angle": win_angle,
            "winning_points": winning_points
        }

    def _ensure_min_length_for_non_vi(
        self,
        parsed: Dict[str, str],
        output_language: str,
        base_prompt: str,
        fast_mode: bool = False
    ) -> Dict[str, str]:
        """
        Guarantee non-VI scripts are not too short after generation/parsing.
        """
        if output_language == 'vi':
            return parsed
        script = (parsed.get('script') or '').strip()
        current_count = self._estimate_word_count(script, output_language)
        if self._is_non_vi_length_ok(script, output_language, current_count):
            return parsed

        best = parsed
        best_count = current_count

        # Retry expansion with stricter constraints, keep the longest acceptable output.
        expand_attempts = [
            f"""YÊU CẦU BỔ SUNG BẮT BUỘC:
- Bản hiện tại đang quá ngắn ({current_count} từ).
- Hãy viết lại đầy đủ hơn, tối thiểu trên 300 từ, tối đa 700 từ.
- Giữ nguyên ý và phong cách đã yêu cầu.""",
            f"""YÊU CẦU BỔ SUNG CỰC KỲ QUAN TRỌNG:
- Bản hiện tại vẫn chưa đạt ({best_count} từ).
- BẮT BUỘC viết trên 300 từ (mục tiêu 350-700 từ), không được rút gọn.
- Tăng chiều sâu nội dung bằng ví dụ/cảm xúc/câu chuyển ý nhưng không đổi thông tin cốt lõi.""",
            f"""YÊU CẦU MỞ RỘNG CUỐI CÙNG:
- Bản hiện tại vẫn chưa đạt yêu cầu độ dài.
- Hãy thêm chiều sâu và chi tiết, đạt trên 300 từ thực tế và nội dung giàu hơn.
- Với tiếng Thái/Nhật: ưu tiên nội dung đầy đặn, không rút gọn câu."""
        ]

        if fast_mode:
            attempts = expand_attempts[:1]
        elif best_count >= 260:
            # Already close to threshold: one focused retry is enough.
            attempts = expand_attempts[:1]
        else:
            # Keep quality while preventing runaway latency from 3 chained long calls.
            attempts = expand_attempts[:2]
        for extra in attempts:
            expand_prompt = f"{base_prompt}\n\n{extra}"
            expanded = self._call_ai_service(
                expand_prompt,
                output_language=output_language,
                prefer_fast=True,
                timeout_seconds=35
            )
            if not expanded:
                continue
            expanded_count = self._estimate_word_count((expanded.get('script') or ''), output_language)
            if expanded_count > best_count:
                best = expanded
                best_count = expanded_count
            if self._is_non_vi_length_ok((best.get('script') or ''), output_language, best_count):
                break

        return best

    def _is_non_vi_length_ok(self, script: str, output_language: str, estimated_words: int) -> bool:
        if output_language == 'vi':
            return True
        compact = re.sub(r'\s+', '', (script or '').strip())
        min_chars_by_lang = {
            'th': 1200,
            'ja': 1000,
        }
        min_chars = min_chars_by_lang.get(output_language, 900)
        return estimated_words >= 300 and len(compact) >= min_chars

    def _estimate_word_count(self, script: str, output_language: str) -> int:
        """
        Better length estimate for languages without whitespace tokenization (Thai/Japanese).
        """
        s = (script or '').strip()
        if not s:
            return 0
        ws_count = len(s.split())
        if output_language in ('th', 'ja', 'zh', 'ko'):
            # When no spaces, whitespace count is misleadingly low.
            # Approximate by character count.
            compact = re.sub(r'\s+', '', s)
            # Use stricter ratio so displayed count better reflects effective content length.
            char_based = max(1, int(len(compact) / 3))
            return max(ws_count, char_based)
        return ws_count

    def _unpack_triple_from_cells(self, phonetic: str, native: str, vietnamese: str) -> Tuple[str, str, str]:
        """
        Model often dumps 'romaji || JP || VI' into ONE cell (usually phonetic) while other cells are empty.
        Normalize separators and split back into 3 columns.
        """
        p, n, v = (phonetic or '').strip(), (native or '').strip(), (vietnamese or '').strip()

        def _split_packed(text: str) -> Optional[Tuple[str, str, str]]:
            if not text:
                return None
            s = text.replace('｜｜', '||').replace('‖', '||')
            s = re.sub(r'\s(?:Ⅱ|II)\s', ' || ', s)
            if '||' not in s and s.count('|') >= 2:
                s = re.sub(r'\s*\|\s*', '||', s)
            if '||' not in s:
                return None
            parts = [x.strip() for x in s.split('||')]
            parts = [re.sub(r'^[—–\-]\s*', '', x).strip() for x in parts if x.strip()]
            if len(parts) >= 3:
                return parts[0], parts[1], ' || '.join(parts[2:])
            if len(parts) == 2:
                return parts[0], parts[1], ''
            return None

        # Prefer unpacking the cell that actually contains || while others are empty.
        if not n and not v:
            unpacked = _split_packed(p)
            if unpacked:
                return unpacked
        if not p and not v:
            unpacked = _split_packed(n)
            if unpacked:
                return unpacked
        if not p and not n:
            unpacked = _split_packed(v)
            if unpacked:
                return unpacked
        # One cell has full triple while another column also has stray content — still try phonetic first.
        if '||' in p and (not n or not v):
            unpacked = _split_packed(p)
            if unpacked:
                return unpacked

        return p, n, v

    def _sanitize_verification_rows(self, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Normalize rows and extract accidental [SECTION] markers leaked into cells.
        """
        cleaned: List[Dict[str, str]] = []
        section_re = re.compile(r'\[SECTION\]\s*([^\[\]\|]+)', flags=re.IGNORECASE)

        for row in rows:
            if not isinstance(row, dict):
                continue
            row_type = str(row.get('row_type', 'row')).strip().lower() or 'row'
            if row_type == 'section':
                title = str(row.get('section_title', '')).strip()
                if title:
                    cleaned.append({
                        'row_type': 'section',
                        'section_title': title,
                        'phonetic': '',
                        'native': '',
                        'vietnamese': '',
                    })
                continue

            phonetic = str(row.get('phonetic', '')).strip().replace('\n', ' ')
            native = str(row.get('native', '')).strip().replace('\n', ' ')
            vietnamese = str(row.get('vietnamese', '')).strip().replace('\n', ' ')

            phonetic, native, vietnamese = self._unpack_triple_from_cells(phonetic, native, vietnamese)

            # Recover rows where model used "II/Ⅱ" separators inside a single cell.
            if (not native and not vietnamese) and re.search(r'\s(?:Ⅱ|II)\s', phonetic):
                parts = [p.strip() for p in re.split(r'\s(?:Ⅱ|II)\s', phonetic) if p.strip()]
                if len(parts) >= 3:
                    phonetic = parts[0]
                    native = parts[1]
                    vietnamese = ' '.join(parts[2:])

            # Pull section marker out of any cell.
            merged = f"{phonetic} {native} {vietnamese}"
            marker = section_re.search(merged)
            if marker:
                cleaned.append({
                    'row_type': 'section',
                    'section_title': marker.group(1).strip(),
                    'phonetic': '',
                    'native': '',
                    'vietnamese': '',
                })
                phonetic = section_re.sub('', phonetic).strip()
                native = section_re.sub('', native).strip()
                vietnamese = section_re.sub('', vietnamese).strip()

            # If one cell is abnormally long, split to avoid unreadable mega-row in UI.
            max_len = max(len(phonetic), len(native), len(vietnamese))
            if max_len > 260:
                source = native or phonetic or vietnamese
                chunks = [p.strip() for p in re.split(r'(?<=[\.\!\?\n。！？])\s*', source) if p.strip()]
                if len(chunks) <= 1:
                    chunks = [source[i:i + 120].strip() for i in range(0, len(source), 120) if source[i:i + 120].strip()]
                for chunk in chunks[:20]:
                    cleaned.append({
                        'row_type': 'row',
                        'phonetic': chunk if phonetic else '',
                        'native': chunk if native else chunk,
                        'vietnamese': '',
                    })
                continue

            if phonetic or native or vietnamese:
                cleaned.append({
                    'row_type': 'row',
                    'phonetic': phonetic,
                    'native': native,
                    'vietnamese': vietnamese,
                })
        return cleaned

    def _compact_verification_rows_for_display(
        self,
        rows: List[Dict[str, str]],
        output_language: str
    ) -> List[Dict[str, str]]:
        """
        Make rows easier to read in UI, especially for Japanese where models
        tend to output paragraph-sized rows.
        """
        if not rows:
            return rows
        if output_language not in ('ja', 'zh', 'zh-TW', 'ko'):
            return rows

        compacted: List[Dict[str, str]] = []

        def _split_cell(text: str) -> List[str]:
            value = (text or '').strip()
            if not value:
                return []
            # Keep medium text intact to avoid over-fragmented rows.
            if len(value) <= 220:
                return [value]
            # Prefer sentence boundaries; fallback to chunk size.
            pieces = [p.strip() for p in re.split(r'(?<=[\.\!\?\n。！？])\s*', value) if p.strip()]
            if len(pieces) <= 1 and len(value) > 240:
                pieces = [value[i:i + 180].strip() for i in range(0, len(value), 180) if value[i:i + 180].strip()]
            return pieces if pieces else [value]

        def _split_to_count(text: str, count: int) -> List[str]:
            """
            Split text into exactly `count` chunks to keep row alignment across columns.
            """
            value = (text or '').strip()
            if count <= 1:
                return [value]
            if not value:
                return [''] * count

            parts = _split_cell(value)
            if len(parts) == count:
                return parts
            if len(parts) > count:
                merged = parts[:count - 1]
                merged.append(' '.join(parts[count - 1:]).strip())
                return merged

            # len(parts) < count -> expand by chunking long segments, then pad.
            expanded: List[str] = []
            for part in parts:
                if len(expanded) >= count:
                    break
                if len(part) > 180 and len(parts) < count:
                    chunk_size = max(60, len(part) // (count - len(expanded)))
                    sub = [part[i:i + chunk_size].strip() for i in range(0, len(part), chunk_size) if part[i:i + chunk_size].strip()]
                    expanded.extend(sub)
                else:
                    expanded.append(part)
            if len(expanded) > count:
                expanded = expanded[:count - 1] + [' '.join(expanded[count - 1:]).strip()]
            while len(expanded) < count:
                expanded.append('')
            return expanded

        for row in rows:
            if row.get('row_type') == 'section':
                compacted.append(row)
                continue

            phonetic = str(row.get('phonetic', '')).strip()
            native = str(row.get('native', '')).strip()
            vietnamese = str(row.get('vietnamese', '')).strip()
            if not (phonetic or native or vietnamese):
                continue

            p_parts = _split_cell(phonetic)
            n_parts = _split_cell(native)
            v_parts = _split_cell(vietnamese)

            # Keep short rows as-is.
            if max(len(phonetic), len(native), len(vietnamese)) <= 180 and max(len(p_parts), len(n_parts), len(v_parts)) <= 1:
                compacted.append({
                    'row_type': 'row',
                    'phonetic': phonetic,
                    'native': native,
                    'vietnamese': vietnamese,
                })
                continue

            # IMPORTANT: Use ONE anchor count to avoid row misalignment.
            # Native column is most reliable; fallback to phonetic/vietnamese.
            anchor_parts = n_parts or p_parts or v_parts or ['']
            # Cap split lines per source row to avoid excessively short fragments.
            line_count = min(3, max(1, len(anchor_parts)))
            p_parts = _split_to_count(phonetic, line_count)
            n_parts = _split_to_count(native, line_count)
            v_parts = _split_to_count(vietnamese, line_count)

            for i in range(line_count):
                compacted.append({
                    'row_type': 'row',
                    'phonetic': p_parts[i] if i < len(p_parts) else '',
                    'native': n_parts[i] if i < len(n_parts) else '',
                    'vietnamese': v_parts[i] if i < len(v_parts) else '',
                })

        return compacted

    def _ensure_multi_sections(self, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Enforce multiple section headers for stable 3-column table UX.
        If model returns only one generic section, re-segment rows into
        several fixed headings.
        """
        if not rows:
            return rows

        pure_rows = [r for r in rows if str(r.get('row_type', '')).lower() != 'section']
        if not pure_rows:
            return rows

        ordered_sections = [
            'Giới thiệu về món quà',
            'Câu chuyện tình yêu',
            'Ý nghĩa của món quà',
            'Quá trình chế tác',
            'Điểm nổi bật sản phẩm',
            'Kêu gọi tương tác',
        ]
        section_rank = {name: idx for idx, name in enumerate(ordered_sections)}

        def _detect_section(row: Dict[str, str], ratio: float) -> str:
            vi = str(row.get('vietnamese', '')).lower()
            native = str(row.get('native', '')).lower()
            text = f"{vi} {native}"

            if any(k in text for k in ['bình luận', 'anh chị nghĩ', 'comment', 'chúc', 'ý kiến']):
                return 'Kêu gọi tương tác'
            if any(k in text for k in ['chế tác', 'xưởng', 'đính', 's925', 'moissanite', 'đánh bóng', 'thiết kế']):
                return 'Quá trình chế tác'
            if any(k in text for k in ['ý nghĩa', 'biểu tượng', 'vĩnh cửu', 'gắn kết', 'cảm xúc', 'tình yêu']):
                return 'Ý nghĩa của món quà'
            if any(k in text for k in ['cô ấy', 'khách', 'câu chuyện', 'kỷ niệm', 'đã tìm đến', 'chia sẻ']):
                return 'Câu chuyện tình yêu'
            if any(k in text for k in ['món quà', 'bangkok', 'tokyo', 'chiang mai', 'mở đầu', 'trong nhịp sống']):
                return 'Giới thiệu về món quà'

            # Position fallback keeps narrative progression.
            if ratio < 0.18:
                return 'Giới thiệu về món quà'
            if ratio < 0.42:
                return 'Câu chuyện tình yêu'
            if ratio < 0.62:
                return 'Ý nghĩa của món quà'
            if ratio < 0.82:
                return 'Quá trình chế tác'
            return 'Kêu gọi tương tác'

        rebuilt: List[Dict[str, str]] = []
        current_section: Optional[str] = None
        current_rank = 0
        total = max(1, len(pure_rows))

        for idx, row in enumerate(pure_rows):
            suggested = _detect_section(row, idx / total)
            suggested_rank = section_rank.get(suggested, current_rank)
            if current_section is None:
                current_section = suggested
                current_rank = suggested_rank
                rebuilt.append({
                    'row_type': 'section',
                    'section_title': current_section,
                    'phonetic': '',
                    'native': '',
                    'vietnamese': '',
                })
            else:
                # Prevent section order from jumping backwards.
                if suggested_rank < current_rank:
                    suggested_rank = current_rank
                    suggested = ordered_sections[suggested_rank]
                if suggested != current_section:
                    current_section = suggested
                    current_rank = suggested_rank
                    rebuilt.append({
                        'row_type': 'section',
                        'section_title': current_section,
                        'phonetic': '',
                        'native': '',
                        'vietnamese': '',
                    })

            rebuilt.append({
                'row_type': 'row',
                'phonetic': str(row.get('phonetic', '')).strip(),
                'native': str(row.get('native', '')).strip(),
                'vietnamese': str(row.get('vietnamese', '')).strip(),
            })

        section_count = sum(1 for r in rebuilt if str(r.get('row_type', '')).lower() == 'section')
        if section_count >= 3:
            return rebuilt

        # Safety fallback: force at least 3 sections by equal slicing.
        rebuilt = []
        target_sections = min(len(ordered_sections), max(3, (len(pure_rows) + 2) // 3))
        rows_per_section = max(1, (len(pure_rows) + target_sections - 1) // target_sections)
        idx = 0
        for sec_idx in range(target_sections):
            rebuilt.append({
                'row_type': 'section',
                'section_title': ordered_sections[sec_idx],
                'phonetic': '',
                'native': '',
                'vietnamese': '',
            })
            for _ in range(rows_per_section):
                if idx >= len(pure_rows):
                    break
                rebuilt.append({
                    'row_type': 'row',
                    'phonetic': str(pure_rows[idx].get('phonetic', '')).strip(),
                    'native': str(pure_rows[idx].get('native', '')).strip(),
                    'vietnamese': str(pure_rows[idx].get('vietnamese', '')).strip(),
                })
                idx += 1

        while idx < len(pure_rows):
            rebuilt.append({
                'row_type': 'row',
                'phonetic': str(pure_rows[idx].get('phonetic', '')).strip(),
                'native': str(pure_rows[idx].get('native', '')).strip(),
                'vietnamese': str(pure_rows[idx].get('vietnamese', '')).strip(),
            })
            idx += 1

        return rebuilt

    def _fill_missing_vietnamese_cells(
        self,
        rows: List[Dict[str, str]],
        source_vietnamese_text: str,
        output_language: str
    ) -> List[Dict[str, str]]:
        """
        Guarantee Vietnamese column is not blank in verification table rows.
        First fill from source VI context by sequence, then use AI translation for remaining blanks.
        """
        if output_language == 'vi' or not rows:
            return rows

        vi_parts = [p.strip() for p in re.split(r'(?<=[\.\!\?\n])\s*', (source_vietnamese_text or '').strip()) if p.strip()]
        vi_idx = 0
        last_vi = ''

        for row in rows:
            if str(row.get('row_type', '')).lower() == 'section':
                continue
            current_vi = str(row.get('vietnamese', '')).strip()
            if current_vi:
                last_vi = current_vi
                continue
            if vi_idx < len(vi_parts):
                row['vietnamese'] = vi_parts[vi_idx]
                last_vi = vi_parts[vi_idx]
                vi_idx += 1
            elif last_vi:
                row['vietnamese'] = last_vi

        # Final pass: translate remaining blanks from native cell.
        missing_pairs = []
        for idx, row in enumerate(rows):
            if str(row.get('row_type', '')).lower() == 'section':
                continue
            if str(row.get('vietnamese', '')).strip():
                continue
            native = str(row.get('native', '')).strip() or str(row.get('phonetic', '')).strip()
            if native:
                missing_pairs.append((idx, native))

        if missing_pairs:
            payload_rows = [{"idx": i, "text": t} for i, t in missing_pairs[:40]]
            prompt = (
                "Dịch sang tiếng Việt tự nhiên theo ngữ cảnh trang sức. "
                "Trả về JSON ARRAY, mỗi phần tử gồm {\"idx\": number, \"vi\": string}. "
                "Giữ nguyên thứ tự idx, không thêm giải thích.\n\n"
                f"DỮ LIỆU:\n{json.dumps(payload_rows, ensure_ascii=False)}"
            )
            # 1. Try DeepSeek first
            ai_text = self._call_deepseek_raw(
                prompt=prompt,
                system_msg="Bạn là biên dịch viên. Chỉ trả JSON hợp lệ.",
                timeout=40,
                log_prefix="Fill missing Vietnamese cells (DeepSeek)"
            )
            
            # 2. Fallback to Claude
            if not ai_text:
                ai_text = self._call_claude_raw(
                    prompt=prompt,
                    system_msg="Bạn là biên dịch viên. Chỉ trả JSON hợp lệ.",
                    models=['claude-haiku-4-5', 'claude-sonnet-4-6'],
                    temperature=0.2,
                    max_tokens=1200,
                    timeout=40,
                    log_prefix="Fill missing Vietnamese cells (Claude Fallback)"
                )
            if ai_text:
                try:
                    cleaned = ai_text.replace('```json', '').replace('```', '').strip()
                    start = cleaned.find('[')
                    end = cleaned.rfind(']')
                    if start != -1 and end != -1:
                        parsed = json.loads(cleaned[start:end + 1])
                        if isinstance(parsed, list):
                            vi_map = {}
                            for item in parsed:
                                if not isinstance(item, dict):
                                    continue
                                idx = item.get('idx')
                                vi = str(item.get('vi', '')).strip()
                                if isinstance(idx, int) and vi:
                                    vi_map[idx] = vi
                            for idx, _ in missing_pairs:
                                if vi_map.get(idx):
                                    rows[idx]['vietnamese'] = vi_map[idx]
                except Exception as e:
                    self.logger.warning(f"fill_missing_vietnamese parse error: {e}")

        return rows

    def _build_enforced_style_instruction(self, output_language: str, market_language: Optional[str] = None) -> str:
        """
        Server-side mandatory style policy.
        This is always enforced regardless of FE input.
        """
        effective_market = (market_language or output_language or 'vi').strip()

        if output_language == 'vi' and effective_market != 'vi':
            locale_block = LOCALIZATION_BY_LANGUAGE.get(
                effective_market,
                MARKET_FOCUS_FALLBACK_VI,
            )
            market_training = GLOBAL_VI_SOURCE_CULTURE_CHECKLIST
            if effective_market == 'th':
                market_training = (
                    f"{GLOBAL_VI_SOURCE_CULTURE_CHECKLIST}\n"
                    f"{THAI_LOCALIZATION_TRAINING_BLOCK}\n"
                    f"{THAI_TEAM_PROMPT_1}\n{THAI_TEAM_PROMPT_2}\n"
                    f"{THAI_VI_SOURCE_CULTURE_CHECKLIST}"
                )
            elif effective_market == 'ja':
                market_training = (
                    f"{GLOBAL_VI_SOURCE_CULTURE_CHECKLIST}\n"
                    f"{JAPANESE_LOCALIZATION_TRAINING_BLOCK}\n"
                    f"{JAPANESE_VI_SOURCE_CULTURE_CHECKLIST}"
                )

            return f"""
QUY TẮC BẮT BUỘC TỪ SERVER (BẢN VIỆT THEO VĂN HOÁ THỊ TRƯỜNG ĐÍCH):
{GLOBAL_MANDATORY_STYLE}
{GLOBAL_LOCALIZATION_BASE_BLOCK}
{locale_block}
{market_training}
"""

        if output_language == 'th':
            return f"""
QUY TẮC BẮT BUỘC TỪ SERVER (THÁI LAN):
{THAI_PRONOUN_LOCK}
{GLOBAL_MANDATORY_STYLE}
{THAI_TEAM_PROMPT_1}
{THAI_TEAM_PROMPT_2}

Ràng buộc thêm:
- Không làm sai thông tin cốt lõi của nội dung nguồn.
- Xưng hô theo format global: dùng "tôi" (Phom) và gọi người xem là "bạn" (Khun).
- Tuyệt đối KHÔNG được có chữ "Huy Ca" hay "HuyK" trong bản dịch.
- TUYỆT ĐỐI KHÔNG nhắc thương hiệu "Viễn Chí Bảo" trong nội dung.
{THAI_LOCALIZATION_TRAINING_BLOCK}
{NON_VI_TABLE_FORMAT_INSTRUCTION}
"""

        if output_language == 'ja':
            return f"""
QUY TẮC BẮT BUỘC TỪ SERVER (NHẬT BẢN):
{GLOBAL_MANDATORY_STYLE}
{JAPANESE_LOCALIZATION_TRAINING_BLOCK}
- Xưng hô theo format global: dùng "tôi" và gọi người xem là "bạn".
- TUYỆT ĐỐI KHÔNG nhắc thương hiệu "Viễn Chí Bảo" trong nội dung.
{NON_VI_TABLE_FORMAT_INSTRUCTION}
"""

        if output_language != 'vi':
            locale_block = LOCALIZATION_BY_LANGUAGE.get(
                output_language,
                MARKET_FOCUS_FALLBACK_VI,
            )
            return f"""
QUY TẮC BẮT BUỘC TỪ SERVER (NGÔN NGỮ KHÁC VIỆT):
{GLOBAL_MANDATORY_STYLE}
{GLOBAL_LOCALIZATION_BASE_BLOCK}
{locale_block}
- Xưng hô theo format global: dùng "tôi" và gọi người xem là "bạn".
- TUYỆT ĐỐI KHÔNG nhắc thương hiệu "Viễn Chí Bảo" trong nội dung.
{NON_VI_TABLE_FORMAT_INSTRUCTION}
"""

        return f"""
QUY TẮC BẮT BUỘC TỪ SERVER (HUYK):
{HUYK_MANDATORY_STYLE}
"""

    def _extract_check_table(self, script: str) -> (str, List[Dict[str, str]]):
        """
        Parse optional [CHECK_TABLE] block.
        Returns cleaned script and structured rows.
        """
        import re
        rows: List[Dict[str, str]] = []

        start_match = re.search(r'\[CHECK_TABLE\]', script, flags=re.IGNORECASE)
        if not start_match:
            return script.strip(), rows

        start_idx = start_match.start()
        content_start = start_match.end()
        end_match = re.search(r'\[/CHECK_TABLE\]', script[content_start:], flags=re.IGNORECASE)
        if end_match:
            end_idx = content_start + end_match.start()
            after_end_idx = content_start + end_match.end()
            table_block = script[content_start:end_idx].strip()
            cleaned_script = (script[:start_idx] + script[after_end_idx:]).strip()
        else:
            # Nếu model quên đóng tag, coi như phần còn lại là bảng
            table_block = script[content_start:].strip()
            cleaned_script = script[:start_idx].strip()

        # Normalize separators often seen in JP/KR output
        table_block = table_block.replace('｜｜', '||').replace('｜', '|').replace('‖', '||')
        table_block = re.sub(r'\s(?:Ⅱ|II)\s', ' || ', table_block)

        # Xoá header cột nếu bị dính cùng dòng
        table_block = re.sub(
            r'phiên âm\s*\|+\s*bản ngữ\s*\|+\s*tiếng việt',
            '',
            table_block,
            flags=re.IGNORECASE
        ).strip()

        # 1) Parse theo từng dòng chuẩn
        for raw_line in table_block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.lower().startswith('[section]'):
                section_title = line[len('[section]'):].strip() or 'Đầu mục'
                rows.append({
                    'row_type': 'section',
                    'section_title': section_title,
                    'phonetic': '',
                    'native': '',
                    'vietnamese': '',
                })
                continue
            if '||' not in line:
                if line.count('|') >= 2:
                    line = re.sub(r'\s*\|\s*', '||', line)
                elif re.search(r'\s(?:Ⅱ|II)\s', line):
                    line = re.sub(r'\s(?:Ⅱ|II)\s', '||', line)
                else:
                    continue
            parts = [p.strip() for p in line.split('||')]
            if len(parts) < 3:
                continue
            rows.append({
                'row_type': 'row',
                'phonetic': parts[0],
                'native': parts[1],
                'vietnamese': ' || '.join(parts[2:]),
            })

        # 2) Fallback parse khi model trả thành 1 dòng dài
        if not rows and '||' in table_block:
            tokens = [t.strip() for t in table_block.split('||') if t.strip()]
            for i in range(0, len(tokens) - 2, 3):
                rows.append({
                    'row_type': 'row',
                    'phonetic': tokens[i],
                    'native': tokens[i + 1],
                    'vietnamese': tokens[i + 2],
                })

        # 3) Recover section-based malformed output (content collapsed into giant cells)
        if len(rows) <= 1 and '[section]' in table_block.lower():
            rows = []
            section_chunks = re.findall(
                r'\[SECTION\]\s*(.*?)(?=\[SECTION\]|\Z)',
                table_block,
                flags=re.IGNORECASE | re.DOTALL
            )
            for chunk in section_chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                first_line, _, remainder = chunk.partition('\n')
                section_title = first_line.strip() or 'Đầu mục'
                rows.append({
                    'row_type': 'section',
                    'section_title': section_title,
                    'phonetic': '',
                    'native': '',
                    'vietnamese': '',
                })
                body = remainder.strip()
                if not body:
                    continue

                parsed_any = False
                for raw in body.splitlines():
                    line = raw.strip()
                    if not line:
                        continue
                    if line.count('|') >= 2 and '||' not in line:
                        line = re.sub(r'\s*\|\s*', '||', line)
                    if '||' in line:
                        parts = [p.strip() for p in line.split('||')]
                        if len(parts) >= 3:
                            rows.append({
                                'row_type': 'row',
                                'phonetic': parts[0],
                                'native': parts[1],
                                'vietnamese': parts[2],
                            })
                            parsed_any = True
                if parsed_any:
                    continue

                # Hard fallback so UI never shows a single huge row
                parts = [p.strip() for p in re.split(r'(?<=[\.\!\?\n。！？])\s*', body) if p.strip()]
                if len(parts) <= 1:
                    parts = [body[i:i + 120].strip() for i in range(0, len(body), 120) if body[i:i + 120].strip()]
                for part in parts[:12]:
                    rows.append({
                        'row_type': 'row',
                        'phonetic': part,
                        'native': part,
                        'vietnamese': '',
                    })

        return cleaned_script, rows

    def _generate_verification_rows_with_ai(
        self,
        script: str,
        output_language: str,
        source_vietnamese_text: str,
        fast_mode: bool = False,
        translation_mode: bool = False,
        anchor_to_native: bool = False,
    ) -> List[Dict[str, str]]:
        # Shift pronouns for Thai market so table matches script
        if output_language == 'th':
            source_vietnamese_text = self._shift_vi_pronouns(source_vietnamese_text)
        """
        Fallback: if model forgets [CHECK_TABLE], ask AI to produce rows.
        Returns [] if cannot build.
        """
        if not script.strip():
            return []

        # Translation-only mode:
        if translation_mode:
            if anchor_to_native:
                rows = self._build_global_to_vi_rows(
                    source_script=script,
                    source_lang=output_language,
                    target_vi_text=source_vietnamese_text,
                    fast_mode=fast_mode
                )
            else:
                rows = self._build_translation_mode_rows(
                    output_language=output_language,
                    source_vietnamese_text=source_vietnamese_text,
                    fast_mode=fast_mode
                )
            
            if rows:
                rows = self._sanitize_verification_rows(rows)
                rows = self._compact_verification_rows_for_display(rows, output_language)
                rows = self._ensure_multi_sections(rows)
                
                # RETURN IMMEDIATELY for translation mode to prevent DeepSeek overrides
                if translation_mode:
                    return rows
                    
                rows = self._fill_missing_vietnamese_cells(
                    rows,
                    source_vietnamese_text=source_vietnamese_text,
                    output_language=output_language
                )
                return self._repair_invalid_phonetic_rows(rows, output_language)

        prompt = f"""
Bạn là trợ lý kiểm duyệt subtitle.
Hãy chia nội dung dưới đây thành các câu/đoạn ngắn để dựng video và trả về JSON ARRAY DUY NHẤT.

YÊU CẦU:
- Mỗi phần tử phải có key row_type:
  - row_type='section': có thêm section_title
  - row_type='row': có phonetic, native, vietnamese
- BẮT BUỘC có NHIỀU section (tối thiểu 4 section), KHÔNG gộp tất cả vào 1 section.
- native: BẮT BUỘC giữ nguyên câu ở ngôn ngữ đích ({output_language}) dùng chữ bản địa (Thai script nếu là TH).
- phonetic: phiên âm Latin dễ đọc của native
- vietnamese: dịch nghĩa tiếng Việt sát ý để đối chiếu
{THAI_PRONOUN_LOCK if output_language == 'th' else ''}
- TUYỆT ĐỐI KHÔNG lặp lại tiếng Việt vào cột native nếu output_language là {output_language}.
- Không markdown, không giải thích, không text ngoài JSON

NỘI DUNG NGÔN NGỮ ĐÍCH:
\"\"\"
{script}
\"\"\"

NGỮ CẢNH NGUỒN TIẾNG VIỆT (để đối chiếu nghĩa):
\"\"\"
{source_vietnamese_text}
\"\"\"
""".strip()

        text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg="Bạn là trợ lý kiểm duyệt subtitle đa ngôn ngữ. Trả về JSON đúng schema, không thêm text ngoài JSON.",
            timeout=30 if fast_mode else 50,
            log_prefix="Verification rows generation (DeepSeek)"
        )
        # DeepSeek is faster and cheaper now, skipping Claude fallback for table rows to avoid balance issues
        if text:
            parsed = self._extract_json_array(text)
            if parsed and isinstance(parsed, list):
                try:
                    rows: List[Dict[str, str]] = []
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        row_type = str(item.get('row_type', 'row')).strip().lower() or 'row'
                        if row_type == 'section':
                            section_title = str(item.get('section_title', '')).strip()
                            if section_title:
                                rows.append({
                                    'row_type': 'section',
                                    'section_title': section_title,
                                    'phonetic': '',
                                    'native': '',
                                    'vietnamese': '',
                                })
                            continue
                        phonetic = str(item.get('phonetic', '')).strip()
                        native = str(item.get('native', '')).strip()
                        vietnamese = str(item.get('vietnamese', '')).strip()
                        if native and (phonetic or vietnamese):
                            rows.append({
                                'row_type': 'row',
                                'phonetic': phonetic,
                                'native': native,
                                'vietnamese': vietnamese,
                            })
                    if rows:
                        cleaned_rows = self._sanitize_verification_rows(rows)
                        cleaned_rows = self._compact_verification_rows_for_display(cleaned_rows, output_language)
                        cleaned_rows = self._ensure_multi_sections(cleaned_rows)
                        
                        # OPTIMIZATION: Skip expensive fill/repair steps if we are in translation mode
                        # because row-by-row generation is already accurate and complete.
                        if translation_mode:
                            return cleaned_rows
                            
                        cleaned_rows = self._fill_missing_vietnamese_cells(
                            cleaned_rows,
                            source_vietnamese_text=source_vietnamese_text,
                            output_language=output_language
                        )
                        return self._repair_invalid_phonetic_rows(cleaned_rows, output_language)
                except Exception as e:
                    self.logger.warning(f"verification_rows processing error: {e}")

        # Final fallback: heuristically split script into rows so UI always has 3-column table for non-vi
        cleaned_rows = self._sanitize_verification_rows(self._build_verification_rows_heuristic(script, source_vietnamese_text, output_language))
        cleaned_rows = self._compact_verification_rows_for_display(cleaned_rows, output_language)
        cleaned_rows = self._ensure_multi_sections(cleaned_rows)
        cleaned_rows = self._fill_missing_vietnamese_cells(
            cleaned_rows,
            source_vietnamese_text=source_vietnamese_text,
            output_language=output_language
        )
        return self._repair_invalid_phonetic_rows(cleaned_rows, output_language)

    def _build_translation_mode_rows(
        self,
        output_language: str,
        source_vietnamese_text: str,
        fast_mode: bool = False
    ) -> List[Dict[str, str]]:
        """
        Build accurate verification rows for translation mode by anchoring each row to Vietnamese source.
        """
        vi_parts = [p.strip() for p in re.split(r'(?<=[\.\!\?\n])\s*', (source_vietnamese_text or '').strip()) if p.strip()]
        if output_language == 'th':
            vi_parts = [self._shift_vi_pronouns(p) for p in vi_parts]
        if not vi_parts:
            return []
        vi_parts = vi_parts[:24]

        payload = [{"idx": idx, "vi": text} for idx, text in enumerate(vi_parts)]
        prompt = f"""
Bạn đang tạo bảng kiểm đối chiếu cho chế độ DỊCH CONTENT.
Trả về JSON ARRAY DUY NHẤT, mỗi phần tử có đúng keys:
- idx: number
- native: câu ở ngôn ngữ đích ({output_language})
- phonetic: phiên âm Latin dễ đọc của native

RÀNG BUỘC CHÍNH XÁC:
- Giữ đúng ý của từng câu tiếng Việt theo idx, không đổi thứ tự.
- native BẮT BUỘC dùng chữ bản địa {output_language} (Thai script nếu là TH). KHÔNG được dùng tiếng Việt.
- phonetic không được để trống, phải là Latin.
- Nếu output_language='th': native BẮT BUỘC dùng chữ Thái Lan (Thai script), phonetic là Latin. TUYỆT ĐỐI KHÔNG dùng tiếng Việt.
{THAI_PRONOUN_LOCK if output_language == 'th' else ''}
- Không thêm giải thích, không markdown.

DỮ LIỆU NGUỒN:
{json.dumps(payload, ensure_ascii=False)}
""".strip()
        text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg="Bạn là trợ lý song ngữ. Trả về JSON hợp lệ, đúng schema.",
            timeout=30 if fast_mode else 50,
            log_prefix="Translation-mode verification rows (DeepSeek)"
        )
        if not text:
            self.logger.info("DeepSeek failed for translation-mode rows, falling back to Claude.")
            text = self._call_claude_raw(
                prompt=prompt,
                system_msg="Bạn là trợ lý song ngữ. Trả về JSON hợp lệ, đúng schema.",
                models=['claude-haiku-4-5', 'claude-sonnet-4-6'],
                temperature=0.2,
                max_tokens=2200,
                timeout=25 if fast_mode else 45,
                log_prefix="Translation-mode verification rows (Claude)"
            )
        if not text:
            return []
        parsed = self._extract_json_array(text)
        if not parsed or not isinstance(parsed, list):
            return []
        
        try:
            by_idx: Dict[int, Dict[str, str]] = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                idx = item.get('idx')
                if not isinstance(idx, int):
                    continue
                by_idx[idx] = item

            rows: List[Dict[str, str]] = []
            rows.append({
                'row_type': 'section',
                'section_title': 'Nội dung đối chiếu',
                'phonetic': '',
                'native': '',
                'vietnamese': '',
            })
            for idx, vi in enumerate(vi_parts):
                info = by_idx.get(idx, {})
                rows.append({
                    'row_type': 'row',
                    'phonetic': str(info.get('phonetic', '')).strip(),
                    'native': str(info.get('native', '')).strip(),
                    'vietnamese': vi,
                })
            return rows
        except Exception as e:
            self.logger.warning(f"translation_mode rows parse error: {e}")
            return []

    def _translate_foreign_to_vi(
        self,
        source_text: str,
        source_lang: str,
        prefer_fast: bool = True
    ) -> Optional[str]:
        """
        Translate foreign text (Thai, Japanese, etc.) to Vietnamese.
        Dedicated prompt for Foreign → VI direction.
        """
        lang_name_map = {
            'th': 'Tiếng Thái', 'ja': 'Tiếng Nhật', 'ko': 'Tiếng Hàn',
            'zh': 'Tiếng Trung', 'my': 'Tiếng Myanmar', 'km': 'Tiếng Khmer', 'lo': 'Tiếng Lào',
        }
        source_lang_name = lang_name_map.get(source_lang, source_lang)

        prompt = f"""
Bạn là biên dịch viên chuyên nghiệp, dịch kịch bản video từ {source_lang_name} sang Tiếng Việt.

NỘI DUNG GỐC ({source_lang_name}):
{source_text}

RÀNG BUỘC:
- Dịch sát nghĩa, giữ đúng thông tin và thứ tự ý.
- Không thêm chi tiết mới, không đổi bối cảnh.
- Văn phong tự nhiên, mượt mà như người Việt viết.
- Xưng hô: Tôi (ngôi 1), Bạn (ngôi 2).
- Chỉ trả về bản dịch Tiếng Việt, không giải thích.
""".strip()

        system_msg = "Bạn là biên dịch viên chất lượng cao. Dịch chính xác từ ngoại ngữ sang Tiếng Việt."

        translated = self._call_deepseek_raw(
            prompt=prompt, system_msg=system_msg,
            timeout=30 if prefer_fast else 50,
            log_prefix=f"Foreign-to-VI translation ({source_lang_name}, DeepSeek)"
        )
        if translated and source_lang == 'th':
            translated = self._sanitize_thai_output(translated)
        return translated

    def _build_global_to_vi_rows(
        self,
        source_script: str,
        source_lang: str,
        target_vi_text: str,
        fast_mode: bool = False
    ) -> List[Dict[str, str]]:
        """
        Build verification rows for Global -> VI.
        Mode 1: If source has newlines (>=3 lines) → split by newlines, match Vietnamese.
        Mode 2: If source is a paragraph (1-2 lines) → use Vietnamese sentences as anchor.
        """
        if not source_script.strip():
            return []

        # Detect mode: structured (has newlines) vs paragraph
        raw_lines = [line.strip() for line in source_script.strip().split('\n') if line.strip()]

        if len(raw_lines) >= 3:
            # MODE 1: Structured - split by newlines
            return self._build_rows_structured(raw_lines, source_lang, target_vi_text, fast_mode)
        else:
            # MODE 2: Paragraph - use Vietnamese as anchor
            return self._build_rows_paragraph(source_script.strip(), source_lang, target_vi_text, fast_mode)

    def _build_rows_structured(
        self,
        thai_lines: List[str],
        source_lang: str,
        target_vi_text: str,
        fast_mode: bool = False
    ) -> List[Dict[str, str]]:
        """Mode 1: Source has clear newlines. Split by newlines, ask AI to match Vietnamese."""
        numbered = '\n'.join([f"{i+1}. {line}" for i, line in enumerate(thai_lines)])

        prompt = f"""
NHIỆM VỤ: Ghép từng dòng {source_lang} với câu Tiếng Việt tương ứng.

CÁC DÒNG GỐC ({source_lang}) - đã đánh số:
{numbered}

BẢN DỊCH TIẾNG VIỆT ĐẦY ĐỦ:
{target_vi_text}

QUY TẮC:
1. Với mỗi dòng gốc (1, 2, 3...), tìm câu/đoạn Tiếng Việt tương ứng về NGHĨA từ bản dịch.
2. Mỗi câu Tiếng Việt chỉ xuất hiện MỘT LẦN. Không lặp lại.
3. Cột "native" phải CHÍNH XÁC dòng gốc, giữ nguyên 100% bao gồm cả dấu "...".
4. Cột "phonetic" là phiên âm Latin của native.
5. Nếu một dòng gốc không có câu Việt tương ứng rõ ràng, vẫn giữ dòng đó và để vietnamese trống.

Trả về JSON ARRAY có ĐÚNG {len(thai_lines)} phần tử:
[{{"id": 1, "native": "...", "vietnamese": "...", "phonetic": "..."}}]
""".strip()

        text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg="Bạn là chuyên gia căn chỉnh kịch bản song ngữ. Trả về JSON ARRAY.",
            timeout=60 if fast_mode else 90,
            log_prefix="Global-to-VI structured-matching (DeepSeek)"
        )

        if not text:
            return []

        parsed = self._extract_json_array(text)
        if not parsed or not isinstance(parsed, list):
            return []

        try:
            rows: List[Dict[str, str]] = []
            rows.append({
                'row_type': 'section',
                'section_title': 'Nội dung đối chiếu',
                'phonetic': '', 'native': '', 'vietnamese': '',
            })
            id_map = {}
            for item in parsed:
                if isinstance(item, dict) and 'id' in item:
                    id_map[item['id']] = item

            for i, thai_line in enumerate(thai_lines):
                item = id_map.get(i + 1) or (parsed[i] if i < len(parsed) else {})
                native = thai_line
                if source_lang == 'th':
                    native = self._sanitize_thai_output(native)
                
                rows.append({
                    'row_type': 'row',
                    'phonetic': str(item.get('phonetic', '')).strip(),
                    'native': native,
                    'vietnamese': str(item.get('vietnamese', '')).strip(),
                })
            return rows
        except Exception as e:
            self.logger.warning(f"global_to_vi structured parse error: {e}")
            return []

    def _build_rows_paragraph(
        self,
        source_text: str,
        source_lang: str,
        target_vi_text: str,
        fast_mode: bool = False
    ) -> List[Dict[str, str]]:
        """Mode 2: Source is a single paragraph. Use Vietnamese sentences as anchor."""
        prompt = f"""
NHIỆM VỤ: Căn chỉnh đoạn văn {source_lang} với bản dịch Tiếng Việt thành bảng đối chiếu 1-1.

ĐOẠN VĂN GỐC ({source_lang}):
{source_text}

BẢN DỊCH TIẾNG VIỆT:
{target_vi_text}

QUY TẮC:
1. LẤY TIẾNG VIỆT LÀM MỐC: Chia bản dịch Tiếng Việt thành từng câu hoàn chỉnh (10-15 hàng).
2. TRUY NGƯỢC: Với mỗi câu Tiếng Việt, tìm đúng đoạn {source_lang} tương ứng từ đoạn văn gốc.
3. GIỮ NGUYÊN: Cột native phải trích xuất chính xác từ đoạn văn gốc, giữ nguyên 100% kể cả dấu "...".
4. KHÔNG LẶP: Mỗi phần chỉ xuất hiện MỘT LẦN.
5. Cột phonetic là phiên âm Latin của native.

Trả về JSON ARRAY:
[{{"native": "...", "vietnamese": "...", "phonetic": "..."}}]
""".strip()

        text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg="Bạn là chuyên gia căn chỉnh kịch bản song ngữ. Trả về JSON ARRAY.",
            timeout=70 if fast_mode else 100,
            log_prefix="Global-to-VI paragraph-matching (DeepSeek)"
        )

        if not text:
            return []

        parsed = self._extract_json_array(text)
        if not parsed or not isinstance(parsed, list):
            return []

        try:
            rows: List[Dict[str, str]] = []
            rows.append({
                'row_type': 'section',
                'section_title': 'Nội dung đối chiếu',
                'phonetic': '', 'native': '', 'vietnamese': '',
            })
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                native = str(item.get('native', '')).strip()
                if source_lang == 'th':
                    native = self._sanitize_thai_output(native)
                    
                vietnamese = str(item.get('vietnamese', '')).strip()
                phonetic = str(item.get('phonetic', '')).strip()
                if native:
                    rows.append({
                        'row_type': 'row',
                        'phonetic': phonetic,
                        'native': native,
                        'vietnamese': vietnamese,
                    })
            return rows
        except Exception as e:
            self.logger.warning(f"global_to_vi paragraph parse error: {e}")
            return []

    def _translate_and_align_foreign_to_vi(
        self,
        source_text: str,
        source_lang: str,
        fast_mode: bool = True
    ) -> Dict[str, Any]:
        """
        One-shot translation and alignment for maximum speed.
        Returns {'script': str, 'verification_rows': List}
        """
        lang_name_map = {'th': 'Tiếng Thái', 'ja': 'Tiếng Nhật', 'ko': 'Tiếng Hàn'}
        source_lang_name = lang_name_map.get(source_lang, source_lang)
        
        # Split source into lines to help AI align
        # CASE 1: Thai text with line breaks → each line = 1 row
        # CASE 2: Thai text as single paragraph → AI will split into sentences
        source_lines = [line.strip() for line in source_text.strip().split('\n') if line.strip()]
        is_single_paragraph = len(source_lines) <= 2
        
        numbered_source = '\n'.join([f"{i+1}. {line}" for i, line in enumerate(source_lines)])

        if is_single_paragraph:
            # CASE 2: Single paragraph - instruct AI to split into natural sentences
            prompt = f"""
NHIỆM VỤ: Chia kịch bản {source_lang_name} thành các câu/đoạn ngắn tự nhiên, rồi dịch từng câu sang Tiếng Việt.

KỊCH BẢN GỐC ({source_lang_name}):
{source_text.strip()}

YÊU CẦU:
- Tự chia nội dung thành 8-20 câu ngắn tự nhiên (mỗi câu ~1-2 ý).
- Dịch mượt mà (Huy Ca style), xưng hô Tôi - Bạn.
- Mỗi câu = 1 đối tượng JSON.
- Cột "native": câu gốc {source_lang_name} (chữ bản địa).
- Cột "vietnamese": Bản dịch tiếng Việt mượt mà.
- Cột "phonetic": Phiên âm Latin của native.

TRẢ VỀ DUY NHẤT 1 JSON ARRAY (KHÔNG GIẢI THÍCH):
[
  {{"id": 1, "native": "...", "vietnamese": "...", "phonetic": "..."}}
]
""".strip()
        else:
            # CASE 1: Multiple lines - align 1-1 with original lines
            prompt = f"""
NHIỆM VỤ: Dịch kịch bản {source_lang_name} sang Tiếng Việt và khớp 1-1 từng dòng.

KỊCH BẢN GỐC ({source_lang_name}):
{numbered_source}

YÊU CẦU:
- Dịch mượt mà (Huy Ca style), xưng hô Tôi - Bạn.
- Khớp 1-1: Mỗi dòng gốc phải có đúng 1 đối tượng JSON tương ứng.
- Cột "native": Giữ nguyên 100% dòng gốc.
- Cột "vietnamese": Bản dịch tiếng Việt mượt mà.
- Cột "phonetic": Phiên âm Latin của native.

TRẢ VỀ DUY NHẤT 1 JSON ARRAY (KHÔNG GIẢI THÍCH):
[
  {{"id": 1, "native": "...", "vietnamese": "...", "phonetic": "..."}}
]
""".strip()

        text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg=f"Bạn là chuyên gia dịch thuật và căn chỉnh kịch bản {source_lang_name}-Việt. Trả về JSON ARRAY.",
            timeout=60 if fast_mode else 90,
            log_prefix="One-shot Array (DeepSeek)"
        )
        
        if not text:
            return {'script': '', 'verification_rows': []}
            
        rows_data = self._extract_json_array(text)
        if not rows_data:
            return {'script': '', 'verification_rows': []}

        # Reconstruct full script from rows for smoothness
        full_script = " ".join([str(r.get('vietnamese', '')).strip() for r in rows_data if r.get('vietnamese')])
        
        # Post-process Thai
        if source_lang == 'th':
            full_script = self._sanitize_thai_output(full_script)
            for r in rows_data:
                r['native'] = self._sanitize_thai_output(r.get('native', ''))
                r['vietnamese'] = self._sanitize_thai_output(r.get('vietnamese', ''))
        
        formatted_rows = self._sanitize_and_format_rows(rows_data, source_lang)
        return {'script': full_script, 'verification_rows': formatted_rows}

    def _sanitize_and_format_rows(self, rows_data: List[Dict], source_lang: str) -> List[Dict]:
        rows = [{
            'row_type': 'section',
            'section_title': 'Nội dung đối chiếu',
            'phonetic': '', 'native': '', 'vietnamese': '',
        }]
        for item in rows_data:
            native = str(item.get('native', '')).strip()
            if source_lang == 'th':
                native = self._sanitize_thai_output(native)
            rows.append({
                'row_type': 'row',
                'phonetic': str(item.get('phonetic', '')).strip(),
                'native': native,
                'vietnamese': str(item.get('vietnamese', '')).strip(),
            })
        return rows

    def _sanitize_thai_output(self, text: str) -> str:
        """
        Hard-coded post-processing for Thai to ensure brand persona.
        Replaces aggressive/forbidden words with preferred elegant ones.
        """
        if not text:
            return text
        
        # 1. Replace "robbery" word with "elegant attention"
        # Handles both with and without suffix
        text = text.replace("ขโมยซีนไป", "โดยไม่แย่งความสนใจ")
        text = text.replace("ขโมยซีน", "โดย không แy่ง ความ สนใจ") # From prompt spec
        
        # 2. Enforce Pronouns (Phom vs Huy Ca)
        text = text.replace("Huy Ca", "ผม")
        text = text.replace("ฮุยกา", "ผม")
        
        # 3. Final cleanup for specific user request spelling
        text = text.replace("โดย không แy่ง ความ สนใจ", "โดย không แy่ง ความ สนใจ")
        
        return text

    def _repair_invalid_phonetic_rows(self, rows: List[Dict[str, str]], output_language: str) -> List[Dict[str, str]]:
        """
        Fix rows where native/phonetic are invalid for target language.
        """
        if output_language == 'vi' or not rows:
            return rows
        invalid_native_targets = []
        invalid_phonetic_targets = []
        script_pattern = {
            'th': r'[\u0E00-\u0E7F]',
            'ja': r'[\u3040-\u30FF\u4E00-\u9FFF]',
            'zh': r'[\u4E00-\u9FFF]',
            'zh-TW': r'[\u4E00-\u9FFF]',
            'ko': r'[\uAC00-\uD7AF]',
        }.get(output_language)
        # Vietnamese diacritics pattern to detect if phonetic column leaks Vietnamese
        vi_pattern = r'[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ]'
        if not script_pattern:
            return rows

        for idx, row in enumerate(rows):
            if str(row.get('row_type', '')).lower() == 'section':
                continue
            native = str(row.get('native', '')).strip()
            phonetic = str(row.get('phonetic', '')).strip()
            if not native:
                continue
            native_ok = self._looks_like_target_language(native, output_language)
            if not native_ok:
                vi_text = str(row.get('vietnamese', '')).strip() or native
                if output_language == 'th':
                    vi_text = self._shift_vi_pronouns(vi_text)
                invalid_native_targets.append((idx, vi_text))
                continue
            same_text = phonetic and phonetic == native
            has_native_script = bool(re.search(script_pattern, phonetic))
            has_vi_script = bool(re.search(vi_pattern, phonetic))
            no_latin = not bool(re.search(r'[A-Za-z]', phonetic))
            
            # If phonetic is same as native, or contains native script, or contains Vietnamese, or has NO latin chars -> repair
            if not phonetic or same_text or has_native_script or has_vi_script or no_latin:
                invalid_phonetic_targets.append((idx, native))

        if invalid_native_targets:
            payload = [{"idx": i, "vi": t} for i, t in invalid_native_targets[:60]]
            prompt = (
                f"BẮT BUỘC: Dịch từng câu tiếng Việt sau sang {output_language}. "
                "Cột 'native' PHẢI là chữ bản địa (Thai script), KHÔNG ĐƯỢC là tiếng Việt. "
                "Cột 'phonetic' là phiên âm Latin. "
                "Trả về JSON ARRAY dạng {\"idx\": number, \"native\": string, \"phonetic\": string}. "
                f"{THAI_PRONOUN_LOCK if output_language == 'th' else ''}\n"
                "Không thêm giải thích.\n\n"
                f"DỮ LIỆU:\n{json.dumps(payload, ensure_ascii=False)}"
            )
            ai_text = self._call_deepseek_raw(
                prompt=prompt,
                system_msg="Bạn là dịch giả + chuyên gia bản địa hoá. TUYỆT ĐỐI KHÔNG lặp lại tiếng Việt vào cột bản ngữ. Chỉ trả JSON.",
                timeout=40,
                log_prefix="Repair native+phonetic rows (DeepSeek)"
            )
            if not ai_text:
                self.logger.info("DeepSeek failed for repair rows, falling back to Claude.")
                ai_text = self._call_claude_raw(
                    prompt=prompt,
                    system_msg="Bạn là dịch giả + chuyên gia bản địa hoá. TUYỆT ĐỐI KHÔNG lặp lại tiếng Việt vào cột bản ngữ. Chỉ trả JSON.",
                    models=['claude-sonnet-4-6', 'claude-haiku-4-5'] if output_language == 'th' else ['claude-haiku-4-5', 'claude-sonnet-4-6'],
                    temperature=0.0,
                    max_tokens=1800,
                    timeout=40,
                    log_prefix="Repair native+phonetic rows (Claude)"
                )
            if ai_text:
                try:
                    parsed = self._extract_json_array(ai_text)
                    if parsed and isinstance(parsed, list):
                        for item in parsed:
                            if not isinstance(item, dict):
                                continue
                            idx = item.get('idx')
                            native = str(item.get('native', '')).strip()
                            phonetic = str(item.get('phonetic', '')).strip()
                            if not isinstance(idx, int) or idx < 0 or idx >= len(rows):
                                continue
                            if native and self._looks_like_target_language(native, output_language):
                                rows[idx]['native'] = native
                            if phonetic and re.search(r'[A-Za-z]', phonetic):
                                rows[idx]['phonetic'] = phonetic
                except Exception as e:
                    self.logger.warning(f"repair native+phonetic parse error: {e}")

            # recompute phonetic validation after native repair
            invalid_phonetic_targets = []
            for idx, row in enumerate(rows):
                if str(row.get('row_type', '')).lower() == 'section':
                    continue
                native = str(row.get('native', '')).strip()
                phonetic = str(row.get('phonetic', '')).strip()
                if not native or not self._looks_like_target_language(native, output_language):
                    continue
                same_text = phonetic and phonetic == native
                has_native_script = bool(re.search(script_pattern, phonetic))
                has_vi_script = bool(re.search(vi_pattern, phonetic))
                no_latin = not bool(re.search(r'[A-Za-z]', phonetic))
                if not phonetic or same_text or has_native_script or has_vi_script or no_latin:
                    invalid_phonetic_targets.append((idx, native))

        if not invalid_phonetic_targets:
            return rows

        payload = [{"idx": i, "native": t} for i, t in invalid_phonetic_targets[:60]]
        prompt = (
            f"Tạo phiên âm Latin (Karaoke/Romanization) cho từng câu native ({output_language}) dưới đây. "
            "BẮT BUỘC: Cột 'phonetic' CHỈ ĐƯỢC chứa ký tự Latin (A-Z). "
            "TUYỆT ĐỐI KHÔNG ĐƯỢC dùng chữ bản địa, KHÔNG ĐƯỢC dùng tiếng Việt trong cột phonetic. "
            "Trả về JSON ARRAY dạng {\"idx\": number, \"phonetic\": string}. "
            + ("Nếu native là tiếng Thái thì phiên âm theo giọng nam (dùng Phom, Khun, Phuean khon nan). " if output_language == 'th' else "")
            + "Không thêm giải thích.\n\n"
            f"DỮ LIỆU:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        ai_text = self._call_deepseek_raw(
            prompt=prompt,
            system_msg="Bạn là chuyên gia phiên âm. Chỉ trả JSON hợp lệ.",
            timeout=30,
            log_prefix="Repair phonetic rows (DeepSeek)"
        )
        if not ai_text:
            return rows
        try:
            cleaned = ai_text.replace('```json', '').replace('```', '').strip()
            start = cleaned.find('[')
            end = cleaned.rfind(']')
            if start == -1 or end == -1:
                return rows
            parsed = json.loads(cleaned[start:end + 1])
            if not isinstance(parsed, list):
                return rows
            phonetic_map: Dict[int, str] = {}
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                idx = item.get('idx')
                pho = str(item.get('phonetic', '')).strip()
                if isinstance(idx, int) and pho:
                    phonetic_map[idx] = pho
            for idx, _ in invalid_phonetic_targets:
                if phonetic_map.get(idx):
                    rows[idx]['phonetic'] = phonetic_map[idx]
        except Exception as e:
            self.logger.warning(f"repair phonetic parse error: {e}")
        return rows

    def _build_verification_rows_heuristic(self, script: str, source_vietnamese_text: str, output_language: str) -> List[Dict[str, str]]:
        """
        Build minimal verification rows when AI JSON-table generation fails.
        Ensures non-vi always has rows for recheck UI.
        """
        cleaned_script = (script or '').strip()
        if not cleaned_script:
            return []

        rows: List[Dict[str, str]] = [{
            'row_type': 'section',
            'section_title': 'Nội dung chính',
            'phonetic': '',
            'native': '',
            'vietnamese': '',
        }]

        # Split by punctuation first; if still one long chunk, split by length.
        parts = [p.strip() for p in re.split(r'(?<=[\.\!\?\n。！？])\s*', cleaned_script) if p.strip()]
        if len(parts) <= 1:
            chunk_size = 120
            parts = [cleaned_script[i:i + chunk_size].strip() for i in range(0, len(cleaned_script), chunk_size) if cleaned_script[i:i + chunk_size].strip()]

        vi_parts = [p.strip() for p in re.split(r'(?<=[\.\!\?\n])\s*', (source_vietnamese_text or '').strip()) if p.strip()]
        if output_language == 'th':
            vi_parts = [self._shift_vi_pronouns(p) for p in vi_parts]

        for idx, p in enumerate(parts[:18]):
            rows.append({
                'row_type': 'row',
                'phonetic': p,  # fallback: use native text as phonetic placeholder
                'native': p,
                'vietnamese': vi_parts[idx] if idx < len(vi_parts) else '',
            })
        return rows


    def _shift_vi_pronouns(self, text: str) -> str:
        """
        Manually shift Vietnamese pronouns for Thai/Global market.
        Huy Ca -> Tôi, Anh chị -> Bạn
        """
        if not text:
            return text
        # Order matters: replace longer specific phrases first
        replacements = [
            (r'\bHuy Ca\b', 'Tôi'),
            (r'\bhuy ca\b', 'tôi'),
            (r'\bHuyK\b', 'Tôi'),
            (r'\banh chị\b', 'bạn'),
            (r'\bAnh chị\b', 'Bạn'),
            (r'\bcác bạn\b', 'bạn'),
            (r'\bCác bạn\b', 'Bạn'),
            (r'\bmọi người\b', 'bạn'),
            (r'\bMọi người\b', 'Bạn'),
        ]
        res = text
        for pattern, replacement in replacements:
            res = re.sub(pattern, replacement, res, flags=re.IGNORECASE)
        return res

    def _parse_response(self, content: str, output_language: str = 'vi') -> Dict[str, str]:
        """Parse model response as plain text script."""
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
        
        # Loại bỏ hashtag, và chuẩn hoá ký tự đặc biệt
        script = re.sub(r'#\S+', '', script)  # hashtags
        if output_language != 'th':
            # Thái cho phép emoji nhẹ theo guideline, nên không strip emoji ở luồng 'th'
            script = re.sub(r'[\U0001F600-\U0001F9FF\U00002702-\U000027B0\U0001F1E0-\U0001F1FF\U0000FE00-\U0000FE0F\U00002600-\U000026FF]', '', script)
        script = re.sub(r'[!]+', '.', script)  # dấu ! → dấu .
        script = re.sub(r'\s{2,}', ' ', script).strip()  # multiple spaces

        # ⛔ XÓA câu tự xưng tên branding ở cuối (Outro video sẽ nói phần này)
        # Bắt tất cả biến thể: "Mình là Huy Ca...", "HuyK đến từ...", v.v.
        outro_patterns = [
            r'\.?\s*Mình là Huy\s*[Cc]a[^.]*Viễn Chí Bảo[^.]*\.?',
            r'\.?\s*Mình là Huy\s*[Kk][^.]*Viễn Chí Bảo[^.]*\.?',
            r'\.?\s*Huy\s*[Cc]a đến từ Viễn Chí Bảo[^.]*\.?',
            r'\.?\s*Huy\s*[Kk] đến từ Viễn Chí Bảo[^.]*\.?',
            r'\.?\s*Mình là Huy\s*[Cc]a[^.]*\.?\s*$',
            r'\.?\s*từ Viễn Chí Bảo[^.]*\.?\s*$',
        ]
        for pattern in outro_patterns:
            before = script
            script = re.sub(pattern, '.', script, flags=re.IGNORECASE).strip()
            if script != before:
                logger.info(f"🚫 Stripped outro self-introduction phrase from generated content")
        # Dọn dấu câu thừa ở cuối
        script = re.sub(r'[\.]+\s*$', '.', script).strip()
        
        # Gộp nhiều xuống dòng thành 1 đoạn văn liền (tối đa 2 đoạn) - tránh tách từng câu
        # Riêng tiếng Thái giữ nguyên nhiều dòng để đáp ứng format 3 phần (latin/thai/vi đối chiếu).
        lines = [line.strip() for line in script.split('\n') if line.strip()]
        if output_language != 'th':
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

        # Giới hạn trên theo ngôn ngữ để tránh cắt ngắn non-vi không cần thiết
        max_words_by_lang = {
            'vi': 360,
            'th': 900,
            'ja': 900,
        }
        MAX_WORDS = max_words_by_lang.get(output_language, 900 if output_language != 'vi' else 360)
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

        # Keep script clean by stripping any inline [CHECK_TABLE] block from model text.
        # The actual table rows are generated by a unified backend pipeline.
        script, extracted_rows = self._extract_check_table(script)
        verification_rows: List[Dict[str, str]] = extracted_rows or []
        word_count = self._estimate_word_count(script, output_language)
        
        # Return simple structure with full script
        return {
            'title': 'Content Huy Ca (Full Script)',
            'hook': '',
            'problem': '',
            'solution': '',
            'cta': '',
            'script': script,
            'word_count': word_count,
            'verification_rows': verification_rows
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
        
        content = self._call_claude_raw(
            prompt=prompt,
            system_msg="Bạn là chuyên gia Prompt Engineering và Content Marketing.",
            models=['claude-haiku-4-5', 'claude-sonnet-4-6'],
            temperature=0.7,
            max_tokens=1024,
            timeout=40,
            log_prefix="Generate optimization prompt"
        )
        if content:
            return content.strip()

        return "Hãy viết một kịch bản hấp dẫn dựa trên video này."
