"""
PAAST Content Analyzer — engine phân tích & chấm điểm.

Nguồn nghiệp vụ: PAAST_Business_Logic_Rules v1.1 (thang điểm 100, mỗi lớp tối đa 20) + tài liệu
nghiệp vụ PAAST đầy đủ (patch v3 — thang chấm 0-5/tiêu chí, "có nhưng yếu ≠ điểm cao", "không tự
suy diễn", "không chấm theo độ dài/số lượng tiêu chí xuất hiện").
Nguồn kỹ thuật/rubric detect: PAAST_Analyzer_Spec.md v1.0 (mục 5.1, 5.4, 5.5).

Thiết kế: LLM chỉ làm nhiệm vụ CHẤM (level 0-5 + evidence quote + reasoning) cho từng tiêu chí —
không tự gán status pass/miss/na hay primary/secondary/off, không tự cộng điểm. Việc suy ra status
từ level (`_normalize_classification`) và quy đổi thành điểm số (`compute_scores`) đều là hàm
thuần Python, không qua LLM — để công thức chấm điểm có một nguồn duy nhất, không lệch giữa lần
phân tích đầu và lần phân tích lại sau khi nâng cấp nội dung, và để tránh phó thác các ràng buộc
nghiệp vụ (vd tối đa 1 primary, ngưỡng "đạt") cho LLM tự tuân thủ.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from video_management.services.content_generation_service import (
    ContentGenerationService,
    DeepSeekError,
    DEEPSEEK_DEFAULT_MODEL,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ngân sách thời gian
# ---------------------------------------------------------------------------
# Timeout TRONG (Django→DeepSeek) phải nhỏ hơn HẲN timeout NGOÀI (BE→AI service), nếu không
# BE luôn bung trước: Django còn cần thời gian cho handshake, đọc body, parse JSON, normalize
# và serialize response SAU KHI DeepSeek trả xong. Trước đây 2 mốc bằng nhau (120s = 120s) nên
# mọi lượt chạm trần đều bị BE huỷ ngang dù Django sắp trả kết quả hợp lệ.
DEEPSEEK_TIMEOUT_MARGIN_S = 20

# Khi caller không gửi timeout_seconds (gọi trực tiếp, test, hoặc client cũ).
DEFAULT_ANALYZE_TIMEOUT_S = 120

# Tối đa 3 lượt/nhóm (1 đầu + 2 thử lại) — nhưng chỉ thử lại khi ngân sách còn đủ, xem
# _classify_group. Thử lại TẠI CHỖ trong worker của nhóm, thay vì để BE chạy lại cả 5 nhóm:
# trước đây 1 nhóm hỏng là raise cả lượt, nên xác suất hỏng chung = 1-(1-p)^5 (p=5%/nhóm đã
# ra 23% lượt hỏng), và mỗi lần BE retry lại tung xúc xắc 5 mặt lần nữa.
MAX_GROUP_ATTEMPTS = 3

# Dưới mức này thì thử lại chỉ để chắc chắn timeout thêm lần nữa — dừng sớm, nhường phần
# quyết định thử lại cho BE.
MIN_RETRY_BUDGET_S = 25

# Tối đa 3 lượt cho bước "viết lại kịch bản" của upgrade_scripted() — cùng số lần với
# writeContentTransformWithRetry() phía BE (nay chuyển hẳn việc thử lại vào đây, xem
# upgrade_scripted). Cùng lý do với MAX_GROUP_ATTEMPTS: model reasoning timeout ngẫu nhiên,
# thử lại tại chỗ rẻ hơn để BE gọi lại nguyên cả request.
MAX_SCRIPTED_WRITE_ATTEMPTS = 3

# Trần cho MỖI lượt gọi của 1 nhóm. Cần thiết vì nếu để 1 lượt được dùng trọn ngân sách còn
# lại thì một lượt bất thường sẽ ăn sạch budget và retry per-group không bao giờ chạy được —
# đúng lỗi đã mắc ở bản sửa đầu: lỗi "reasoning ăn hết token" là lỗi CHẬM (~90s), không phải
# lỗi nhanh như giả định. Với suy luận đã tắt, lượt bình thường chỉ ~9s nên trần 45s vừa rất
# rộng cho input dài, vừa đảm bảo luôn còn budget cho ít nhất 1 lượt thử lại.
PER_ATTEMPT_TIMEOUT_CAP_S = 45


# ---------------------------------------------------------------------------
# Định nghĩa 30 tiêu chí (+ 6 insight Prefer) — dùng để build prompt và để
# điền đầy đủ metadata (tên EN/VN) vào response kể cả khi LLM không trả đủ.
# ---------------------------------------------------------------------------

PREFER_INSIGHTS = [
    {"code": "C", "name_en": "Curiosity", "name_vi": "Tò mò",
     "signal": "Câu hỏi mở, thông tin trái ngược, sự kiện kỳ lạ, twist bất ngờ"},
    {"code": "R", "name_en": "Reactions", "name_vi": "Cảm xúc mạnh",
     "signal": "Kích thích cảm xúc THẬT — cười, khóc, sợ, hồi hộp, bất ngờ, hay các cảm xúc mạnh khác "
               "— miễn là cảm xúc đó được XÂY DỰNG thật sự trong content, không phải chỉ 1 chi tiết/"
               "con số gây sốc thoáng qua rồi thôi (cái đó vẫn thuộc về Curiosity nếu chỉ tạo tò mò "
               "chứ chưa thực sự chạm cảm xúc)"},
    {"code": "A", "name_en": "Aesthetics", "name_vi": "Giác quan",
     "signal": "Mô tả trực quan hình ảnh, âm thanh, bối cảnh đẹp"},
    {"code": "V", "name_en": "Vicarious Living", "name_vi": "Sống thay",
     "signal": "POV nhập vai, theo chân nhân vật, nơi ít người biết"},
    {"code": "E", "name_en": "Enrichment", "name_vi": "Học hỏi & phát triển",
     "signal": "Tips, checklist, framework, kiến thức chuyên môn"},
    {"code": "S", "name_en": "Superiority", "name_vi": "Cái tôi khác biệt",
     "signal": "Nhấn tính hiếm, \"dành riêng cho\", nhóm ≤ 1%"},
]

ACTION_CRITERIA = [
    {"code": "STOP", "name_en": "Stop", "name_vi": "Dừng lại (Hook)",
     "signal": "Câu mở hook mạnh — twist, câu hỏi ngược, con số sốc trong 1-2 dòng đầu"},
    {"code": "FEEL", "name_en": "Feel", "name_vi": "Cảm nhận (Like)",
     "signal": "Đoạn chạm nỗi đau/ước mơ/trải nghiệm, HOẶC lời mời trực tiếp kiểu \"nếu thấy hay/đồng "
               "cảm thì hãy thả like nhé\" → người đọc muốn thả like"},
    {"code": "ANSWER", "name_en": "Answer", "name_vi": "Đối thoại (Comment)",
     "signal": "Câu hỏi mở/mời tranh luận, HOẶC lời mời trực tiếp kiểu \"hãy để lại comment/bình luận "
               "cho mình biết nhé\""},
    {"code": "CONNECT", "name_en": "Connect", "name_vi": "Kết nối (Share)",
     "signal": "Câu chốt súc tích dễ trích, HOẶC lời mời trực tiếp kiểu \"hãy chia sẻ cho người thân, "
               "bạn bè cùng biết/xem nhé\""},
    {"code": "ENGAGE", "name_en": "Engage", "name_vi": "Gắn bó (Save)",
     "signal": "Tips/formula/checklist đủ dense để save, HOẶC lời mời trực tiếp kiểu \"hãy lưu video "
               "này lại\""},
    {"code": "SEE_AGAIN", "name_en": "See Again", "name_vi": "Xem lại (Rewatch)",
     "signal": "Nhiều lớp info, chi tiết ẩn, câu chốt sâu đáng đọc/xem lại"},
]

ACKNOWLEDGE_CRITERIA = [
    {"code": "BASICS", "name_en": "Basics", "name_vi": "Nền tảng cốt lõi",
     "signal": "\"Mình là ai, mình đang làm/bán sản phẩm-dịch vụ CỤ THỂ gì\" tự nhiên trong story — "
               "xoay quanh một sản phẩm/dịch vụ rõ ràng, không nói chung chung"},
    {"code": "REASONS", "name_en": "Reasons to Choose", "name_vi": "Lý do lựa chọn",
     "signal": "Điểm khác biệt/ưu nhược so với lựa chọn khác"},
    {"code": "AUDIENCE", "name_en": "Audience", "name_vi": "Khách hàng mục tiêu",
     "signal": "Vẽ chân dung KH cụ thể — \"lần đầu...\", \"bạn cũng như tôi...\""},
    {"code": "NEEDS_CONTEXT", "name_en": "Needs Context", "name_vi": "Bối cảnh sử dụng",
     "signal": "Tình huống đời thường khi cần sản phẩm"},
    {"code": "DEEPER_VALUE", "name_en": "Deeper Value", "name_vi": "Giá trị sâu hơn",
     "signal": "Lợi ích cảm xúc vượt trên công dụng"},
    {"code": "STORY", "name_en": "Story", "name_vi": "Câu chuyện & Tầm nhìn",
     "signal": "Gốc gác, hành trình, tầm nhìn brand"},
]

# 2 tiêu chí text-detectable — chấm bình thường qua LLM.
STICK_TEXT_DETECTABLE_CRITERIA = [
    {"code": "SIGNATURE_FACE", "name_en": "Signature Face", "name_vi": "Diện mạo IP",
     "signal": "Nhân vật xưng danh, có thể phát triển thành IP"},
    {"code": "CORE_MANTRA", "name_en": "Core Mantra", "name_vi": "Thần chú cốt lõi",
     "signal": "Câu ngắn có thể lặp lại như mantra"},
]

# 4 tiêu chí luôn `na` khi input chỉ là text thuần (business doc §7.2) — không gửi cho LLM.
STICK_PRODUCTION_ONLY_CRITERIA = [
    {"code": "THEMED_STAGE", "name_en": "Themed Stage", "name_vi": "Bối cảnh đặc trưng"},
    {"code": "ICONIC_TOTEM", "name_en": "Iconic Totem", "name_vi": "Đạo cụ biểu tượng"},
    {"code": "KINETIC_RITUAL", "name_en": "Kinetic Ritual", "name_vi": "Nghi thức chuyển động"},
    {"code": "SONIC_EMOTION", "name_en": "Sonic Emotion", "name_vi": "Âm thanh cảm xúc"},
]

TRUST_CRITERIA = [
    {"code": "TRANSPARENCY", "name_en": "Transparency", "name_vi": "Minh bạch",
     "signal": "Chia sẻ hậu trường/hình ảnh sản xuất, quy trình, khó khăn thật — chủ yếu thể hiện qua "
               "VIDEO/hình ảnh; với kịch bản chỉ có câu chữ, mặc định tính đạt nếu văn bản không có "
               "bằng chứng nào (không thể đòi hỏi text phải tự chứng minh một điều thuộc về hình ảnh)"},
    {"code": "RESPONSIBILITY", "name_en": "Responsibility", "name_vi": "Trách nhiệm xã hội",
     "signal": "Cam kết môi trường, cộng đồng"},
    {"code": "UNBIASED_AUTHORITY", "name_en": "Unbiased Authority", "name_vi": "Chứng thực chuyên gia",
     "signal": "Chứng thực từ BÊN NGOÀI — chuyên gia độc lập, KOL, chứng chỉ/giải thưởng do bên thứ 3 "
               "cấp; KHÔNG tính lời tự nhận chuyên môn của chính thương hiệu (\"chúng tôi là chuyên "
               "gia\", \"đội ngũ giàu kinh nghiệm\")"},
    {"code": "SOCIAL_PROOF", "name_en": "Social Proof", "name_vi": "Xã hội chứng thực",
     "signal": "Feedback thật, số lượng KH, case study"},
    {"code": "TANGIBLE_EVIDENCE", "name_en": "Tangible Evidence", "name_vi": "Thực chứng",
     "signal": "Số liệu, giải thưởng, chứng nhận"},
    {"code": "STORYTELLING_HUMAN_TOUCH", "name_en": "Storytelling Human Touch", "name_vi": "Nhân hoá",
     "signal": "Câu chuyện founder / nhân viên / KH thật"},
]

# ---------------------------------------------------------------------------
# Thang điểm 0-5 cho TỪNG tiêu chí (thay cho pass/miss nhị phân cũ) — vận hành hoá nguyên tắc
# "có nhưng yếu ≠ điểm cao" từ tài liệu nghiệp vụ PAAST (Quy tắc 2.2/§9 Quy tắc 2). Một tiêu chí
# có XUẤT HIỆN trong content không còn tự động ăn trọn điểm — mức độ triển khai mạnh/yếu quyết
# định điểm thật. LLM chỉ chấm "level"; status pass/miss/na và điểm số đều SUY RA từ level bằng
# hàm thuần ở _normalize_classification/compute_scores, không để LLM tự gán nhãn/tự cộng điểm.
# ---------------------------------------------------------------------------
LEVEL_LABELS: Dict[int, str] = {
    0: "Không có",
    1: "Rất yếu",
    2: "Yếu",
    3: "Khá",
    4: "Mạnh",
    5: "Xuất sắc",
}

# Từ mức nào trở lên thì coi là "đạt" (status=pass) cho mục đích suy ra status/verdict/hiển thị
# icon — 3 "Khá" trở lên nghĩa là tiêu chí có tác động thật, không chỉ tồn tại hình thức.
LEVEL_PASS_THRESHOLD = 3

# Prefer: ngưỡng để 1 insight đủ tư cách làm "chủ đạo" (primary) / "phụ trợ" (secondary) — xem
# PaastAnalysisService._select_prefer_statuses().
PREFER_PRIMARY_LEVEL_THRESHOLD = 3
PREFER_SECONDARY_LEVEL_THRESHOLD = 2

# Lookup nhanh theo code — dùng để enrich prompt nâng cấp (§5.5) với mô tả "signal" gốc của
# từng tiêu chí, thay vì chỉ dựa vào evidence/suggestion do LLM sinh ra lúc phân tích (thường
# ngắn và không đủ ngữ cảnh để viết ra câu văn tự nhiên, sát tiêu chí).
ALL_CRITERIA_BY_CODE: Dict[str, Dict[str, str]] = {
    defn["code"]: defn
    for defn in (
        PREFER_INSIGHTS + ACTION_CRITERIA + ACKNOWLEDGE_CRITERIA
        + STICK_TEXT_DETECTABLE_CRITERIA + STICK_PRODUCTION_ONLY_CRITERIA + TRUST_CRITERIA
    )
}

# 5 nhóm phân loại — mỗi nhóm là 1 lệnh gọi LLM riêng, chạy song song (xem _classify).
# Tách nhỏ thay vì 1 prompt gộp cả 30 tiêu chí giúp giảm độ trễ (thời gian chờ = lệnh
# chậm nhất trong 5 lệnh chạy song song, không phải tổng cả 5) và giảm rủi ro response
# bị cắt cụt do quá dài (mỗi lệnh giờ chỉ cần trả tối đa 6 tiêu chí thay vì cả 30+6).
#
# TẮT SUY LUẬN cho toàn bộ phần phân loại PAAST — đây là thay đổi quan trọng nhất.
#
# Phân loại PAAST là tác vụ TRÍCH XUẤT CÓ CẤU TRÚC (đọc content, gán status + quote lại câu
# nguyên văn), không phải tác vụ cần suy luận nhiều bước. Chạy nó trên model reasoning gây
# đúng bệnh "over-thinking": reasoning_tokens NỞ RA LẤP ĐẦY mọi max_tokens được cấp rồi không
# còn chỗ sinh nội dung ⇒ content rỗng ⇒ 502. Thực đo trên nhóm prefer (nhóm nặng nhất):
#
#   max_tokens=16000 + suy luận : 0/3 lượt thành công, ~115s/lượt, reasoning_tokens ~11k-16k
#   max_tokens=12000 + suy luận : 0/3 lượt thành công, ~90s/lượt, reasoning ăn trọn 12000
#   TẮT suy luận                : thành công, ~9s/lượt, completion chỉ ~1530 token
#
# Tức nâng/hạ max_tokens đều không cứu được — model luôn tiêu hết phần được cấp. Tắt hẳn suy
# luận vừa sửa đúng lỗi vừa nhanh hơn ~10 lần. Chất lượng phân loại KHÔNG dựa vào suy luận:
# điểm số do compute_scores (Python thuần) tính, LLM chỉ gán nhãn và trích quote.
DISABLE_THINKING_PARAMS = {"thinking": {"type": "disabled"}}

# Với suy luận đã tắt, nhu cầu token trở nên DỰ ĐOÁN ĐƯỢC (thực đo: nhóm nặng nhất ~1530
# token cho ~3900 ký tự JSON) nên mới cấp được theo số tiêu chí một cách an toàn. Vẫn để dư
# gấp ~2.5 lần mức thực đo cho input dài.
#
# Patch v2.1: mỗi tiêu chí giờ có thêm field "reasoning" (1-2 câu) — tăng nhẹ các mốc token so
# với bản trước để có headroom, vẫn tắt suy luận (đây là extraction có cấu trúc, không phải
# suy luận nhiều bước) nên nhu cầu token vẫn dự đoán được.
GROUP_MAX_TOKENS_6_CRITERIA = 5000
GROUP_MAX_TOKENS_STICK = 2500
# Prefer cần nhiều hơn hẳn: ngoài 6 insight x reasoning còn có takeaway_statement, wow_strength,
# coherence.warning — toàn bộ vẫn 1 lệnh gọi, tắt suy luận, chỉ là JSON dài hơn.
GROUP_MAX_TOKENS_PREFER = 6500
# Video Realism Check: 4 field văn bản tự do + 1 enum, không có vòng lặp theo code như các
# nhóm khác — độ dài tương đương ~3-4 tiêu chí.
VIDEO_REALISM_MAX_TOKENS = 2000

# extra_keys: các field top-level KHÁC "group_key" mà _classify_group phải trích thêm từ cùng
# 1 JSON response — dùng cho prefer (coherence/takeaway/wow phải ra từ CÙNG 1 lượt đọc toàn bài
# với việc phân loại insight, không tách thành lệnh gọi riêng vì sẽ mất tính nhất quán).
CLASSIFICATION_GROUPS = [
    ("prefer", "NHÓM PREFER (đánh giá TỔNG THỂ toàn bài, không phải câu-by-câu)", PREFER_INSIGHTS, "level 0-5 cho từng insight", GROUP_MAX_TOKENS_PREFER, ["prefer_meta"]),
    ("action", "NHÓM ACTION — S-FACES (đánh giá từng câu/đoạn cụ thể)", ACTION_CRITERIA, "level 0-5", GROUP_MAX_TOKENS_6_CRITERIA, None),
    ("acknowledge", "NHÓM ACKNOWLEDGE — BRANDS (đánh giá từng câu/đoạn cụ thể)", ACKNOWLEDGE_CRITERIA, "level 0-5", GROUP_MAX_TOKENS_6_CRITERIA, None),
    ("stick", "NHÓM STICK text-detectable (chỉ 2 tiêu chí này detect được từ text thuần)", STICK_TEXT_DETECTABLE_CRITERIA, "level 0-5", GROUP_MAX_TOKENS_STICK, None),
    ("trust", "NHÓM TRUST — TRUSTS (đánh giá từng câu/đoạn cụ thể)", TRUST_CRITERIA, "level 0-5", GROUP_MAX_TOKENS_6_CRITERIA, None),
]

# CTA compliance patterns — port nguyên văn từ PAAST_Analyzer_Spec.md §5.4.
CTA_VIOLATION_PATTERNS = [
    r"mua ngay", r"order ngay", r"inbox ngay", r"chốt đơn",
    r"sale \d+%", r"giảm \d+%",
    r"còn \d+ (suất|sản phẩm|slot)", r"sắp hết",
    r"nhanh tay", r"deadline",
    r"like page", r"follow ngay", r"chia sẻ để nhận",
]

CTA_OK_PATTERNS = [
    r"(comment|bình luận|chia sẻ) (nhé|thử|bên dưới)",
    r"(kể|nêu) (quan điểm|câu chuyện|trải nghiệm)",
    r"lưu lại (để )?(xem|dùng) (sau|khi)",
    r"tag (một|người) bạn",
    r"(gửi|share) cho (bạn bè|người thân)",
]


class PaastAnalysisService:
    """Phân tích content theo khung PAAST (5 lớp × 6 tiêu chí) và tính điểm 0-100."""

    def __init__(self):
        self.logger = logger
        # Compose lại thay vì viết trùng logic gọi DeepSeek/parse JSON.
        self._gen = ContentGenerationService()

    # ------------------------------------------------------------------
    # LLM classification
    # ------------------------------------------------------------------

    @staticmethod
    def _build_group_prompt(content: str, group_key: str, group_label: str, items: List[Dict[str, str]], status_options: str) -> str:
        lines = [f"{group_label} (chấm {status_options}):"]
        for it in items:
            lines.append(f"- {it['code']} | {it['name_en']} ({it['name_vi']}) — dấu hiệu: {it['signal']}")
        group_desc = "\n".join(lines)

        level_scale_text = (
            'Thang "level" 0-5 dùng CHUNG cho mọi tiêu chí PAAST (không dùng pass/miss nhị phân):\n'
            '  5 — Xuất sắc: triển khai rất rõ, tự nhiên, nổi bật, có khả năng tạo tác động mạnh.\n'
            '  4 — Mạnh: triển khai rõ ràng, có chủ đích, tác động tốt.\n'
            '  3 — Khá: thể hiện tương đối rõ, có khả năng tạo tác động.\n'
            '  2 — Yếu: có triển khai nhưng còn chung chung, chưa đủ mạnh.\n'
            '  1 — Rất yếu: dấu hiệu rất nhỏ hoặc mang tính hình thức, gần như không tác động.\n'
            '  0 — Không có: không tìm thấy bằng chứng.\n'
            'QUAN TRỌNG — "có" khác "tốt": một câu CTA/claim/câu hỏi CÓ XUẤT HIỆN không tự động được '
            'level cao. VD: "Hãy để lại comment nhé" đặt trơ trọi, không gắn với chi tiết/cảm xúc cụ '
            'thể nào của content chỉ nên ở mức 1-2; còn "Bạn nghĩ sao nếu chỉ 1 sai số nhỏ khi chế tác '
            'khiến cả viên đá vỡ đôi? Comment xem bạn sẽ xử lý thế nào" tạo lý do THẬT để trả lời nên '
            'có thể ở mức 4-5. Tương tự, "sản phẩm này rất tốt" / "được nhiều người tin dùng" không phải '
            'bằng chứng cụ thể (mức 0-1); còn "9/10 khách hàng trong khảo sát tháng 7 đánh giá đạt kỳ '
            'vọng" là bằng chứng cụ thể, có thể chấm cao hơn hẳn.\n'
            'KHÔNG tự suy diễn: chỉ chấm dựa trên những gì THỰC SỰ có trong content — không dùng kiến '
            'thức/giả định bên ngoài (vd content nói "được nhiều người lựa chọn" thì KHÔNG được tự suy '
            'đoán số lượng/đối tượng cụ thể nào không có trong text).\n'
            'KHÔNG chấm cao chỉ vì content dài hoặc vì nhồi được nhiều tiêu chí cùng lúc — 1 content '
            'ngắn, chỉ tập trung tốt vài tiêu chí thật sự phù hợp mục tiêu của nó vẫn hoàn toàn có thể '
            'chấm cao hơn 1 content dài dàn trải, cố nhồi đủ mọi tiêu chí một cách hời hợt.'
        )

        if group_key == "prefer":
            rule_text = (
                'BƯỚC BẮT BUỘC trước khi chấm bất kỳ insight nào: đọc toàn bộ content MỘT LƯỢT như đang '
                'XEM nó dưới dạng video hoàn chỉnh (không phải đọc để tìm từ khoá). Từ đó xác định '
                '"takeaway_statement" — nếu chỉ được nói 1 câu, điều gì ĐỌNG LẠI trong đầu người xem sau '
                'khi xem xong? Chấm "wow_strength" (strong|moderate|weak) của takeaway đó dựa trên việc '
                'nó có đủ bất ngờ/cảm xúc/khác biệt để người xem nhớ và muốn kể lại cho người khác, hay '
                'chỉ là thông tin nhạt.\n\n'
                + level_scale_text + '\n\n'
                'Áp dụng thang level trên cho CẢ 6 insight C,R,A,V,E,S — với ý nghĩa RIÊNG cho Prefer: '
                'level đo mức độ insight đó THỰC SỰ là động lực khiến người xem dừng lại/xem tiếp/nhớ '
                'tới content, KHÔNG PHẢI chỉ vì câu chữ viết hay hay hình ảnh sinh động. 5 = insight này '
                'rõ ràng là trục chính, bỏ đi content mất hẳn sức hút. 3-4 = có vai trò thật nhưng chưa '
                'chắc là trục chính. 1-2 = chỉ thấp thoáng/phụ hoạ dù có thể xuất hiện dưới dạng câu chữ '
                'đẹp. 0 = không hiện diện. Content ngắn (TikTok/Reels) thường chỉ có 1-2 câu cho mỗi '
                'insight nhưng vẫn có thể ở mức 4-5 nếu đủ đắt giá — KHÔNG đếm số câu bằng chứng một '
                'cách máy móc.\n\n'
                'Phân biệt QUAN TRỌNG giữa Curiosity (C) và Reactions (R) — 2 nhóm dễ nhầm nhất: Curiosity '
                'là sự tò mò/muốn biết tiếp (câu hỏi mở, twist, nghịch lý, khoảng trống thông tin). '
                'Reactions là CẢM XÚC THẬT được kích thích — cười, khóc, sợ, hồi hộp, bất ngờ, hay cảm '
                'xúc mạnh khác — miễn là cảm xúc đó được XÂY DỰNG thật sự trong content; một chi tiết/con '
                'số gây sốc thoáng qua rồi thôi (chỉ tạo tò mò, chưa thực sự chạm cảm xúc) vẫn thuộc về '
                'Curiosity, không phải Reactions.\n\n'
                'Riêng "A" (Aesthetics): nếu content chỉ là câu chữ và KHÔNG mô tả cụ thể hình ảnh/âm '
                'thanh/bối cảnh (chỉ có lời kể/thông tin thuần), field "reasoning" của A phải ghi rõ '
                '"Không đủ dữ liệu để đánh giá đầy đủ yếu tố hình ảnh/âm thanh" — đây là giới hạn của '
                'việc chỉ có text, không phải lỗi của content, nên đừng chấm như thể content thực sự '
                'thiếu sót.\n\n'
                'SAU KHI chấm xong level cho cả 6 insight, HỆ THỐNG (không phải bạn) sẽ tự chọn insight '
                'nào là "chủ đạo" (primary) và "phụ trợ" (secondary) dựa trên level — bạn KHÔNG cần tự '
                'gán nhãn primary/secondary/off, chỉ cần chấm level + description/reasoning thật trung '
                'thực và nhất quán với nhau cho từng insight (đừng cố "né" cho 2 insight cùng level cao '
                'nhất nếu thực sự cả 2 đều mạnh ngang nhau — hệ thống sẽ tự phát hiện đó là dấu hiệu '
                'content chưa hội tụ).\n\n'
                'Field "coherence": tự đánh giá ĐỘC LẬP với việc chấm level — content có giữ đúng 1 '
                'trọng tâm xuyên suốt từ hook đến payoff không, hay giữa chừng "đổi insight" (vd: mở bài '
                'bằng một câu hỏi tò mò nhưng nửa sau nội dung chuyển hẳn sang chủ đề không liên quan)? '
                'Nếu có dấu hiệu lệch hướng, đặt "coherence.is_coherent" = false kèm "warning" giải thích '
                'CỤ THỂ đoạn nào gây lệch — kể cả khi các mức level bạn chấm không tự nói lên điều này '
                '(đây là 2 phép kiểm tra độc lập, bổ sung cho nhau). Nếu content giữ đúng 1 trọng tâm '
                'xuyên suốt, đặt "is_coherent" = true và bỏ trống "warning".\n\n'
                'Field "reasoning" của MỖI insight: 1-2 câu giải thích TẠI SAO nó ở mức level đó, dựa trên '
                'VAI TRÒ của nó trong toàn bộ mạch nội dung (vd: "đây là trục cảm xúc dẫn dắt từ hook đến '
                'payoff") — TUYỆT ĐỐI không viết dạng "vì có N câu chứa từ khoá X". Vẫn liệt kê TẤT CẢ câu '
                'bằng chứng tìm được vào "evidence_sentences" làm dẫn chứng minh hoạ cho mọi mức level, '
                'kể cả level thấp/0 (evidence_sentences có thể rỗng nếu level=0).'
            )
            shape = (
                '{ "prefer_meta": {"takeaway_statement": "1 câu điều đọng lại sau khi xem xong toàn bộ content", '
                '"wow_strength": "strong|moderate|weak", "coherence": {"is_coherent": true|false, '
                '"warning": "chỉ điền khi is_coherent=false — nêu cụ thể đoạn nào gây lệch hướng"}}, '
                '"prefer": [ {"code": "C", "level": 0-5, '
                '"description": "1 câu mô tả ý nghĩa insight này với content này", '
                '"reasoning": "1-2 câu TẠI SAO ở mức level này — dựa trên đọc hiểu, không đếm từ khoá", '
                '"evidence_sentences": ["...", "..."]}, '
                '... đủ 6 code C,R,A,V,E,S ] }'
            )
        else:
            base_rule_text = (
                level_scale_text + '\n\n'
                'Với level ≤ 2: field "evidence" = gợi ý CỤ THỂ nên thêm/sửa gì (không viết chung chung '
                'như "cần cải thiện"). Với level ≥ 3: field "evidence" = quote nguyên văn câu trong '
                'content làm bằng chứng. Field "reasoning" (bắt buộc, MỌI tiêu chí): 1-2 câu giải thích '
                'TẠI SAO ở mức level đó, dựa trên vai trò thật của đoạn đó (hoặc khoảng trống đó) trong '
                'TOÀN BỘ mạch nội dung — TUYỆT ĐỐI không viết dạng "vì có/không có câu chứa từ khoá X".'
            )

            if group_key == "action":
                rule_text = base_rule_text + (
                    '\n\nQUAN TRỌNG cho nhóm Action — FEEL/ANSWER/CONNECT/ENGAGE/SEE_AGAIN: lời mời tương '
                    'tác TRỰC TIẾP (vd: "hãy để lại comment cho mình biết nhé", "hãy lưu video này lại", '
                    '"nếu cảm thấy thích hay đồng cảm thì hãy like và chia sẻ cho người thân bạn bè cùng '
                    'biết nhé") LÀ MỘT DẠNG HỢP LỆ của các tiêu chí này — không tự động bị loại chỉ vì là '
                    'lời mời trực tiếp thay vì câu hỏi khiêu khích. NHƯNG mức level của nó phụ thuộc vào '
                    'việc CTA đó có được neo vào một LÝ DO/CẢM XÚC/CHI TIẾT THẬT của content hay không: '
                    'CTA trơ trọi, không gắn với nội dung cụ thể phía trên (vd chỉ "hãy comment nhé" đặt '
                    'cuối bài, không liên quan gì câu chuyện vừa kể) chỉ nên ở mức 1-2; CTA có nối với 1 '
                    'câu hỏi/chi tiết/cảm xúc cụ thể của chính content mới xứng mức 4-5.\n'
                    'STOP (hook): level 0-5 CHÍNH LÀ độ mạnh của hook — 5 là hook gần như chắc chắn khiến '
                    'người xem dừng lướt (sốc/tò mò/cảm xúc rất rõ trong 1-2 dòng đầu), 1-2 là hook mờ '
                    'nhạt, hầu như không có yếu tố khiến dừng lại.'
                )
            elif group_key == "acknowledge":
                rule_text = base_rule_text + (
                    '\n\nBối cảnh riêng cho Acknowledge: nội dung cần xoay quanh MỘT sản phẩm/dịch vụ CỤ '
                    'THỂ của chính người nói/thương hiệu — không phải lời khẳng định thương hiệu chung '
                    'chung, mơ hồ, có thể áp dụng cho bất kỳ sản phẩm nào khác.'
                )
            elif group_key == "trust":
                rule_text = base_rule_text + (
                    '\n\nRiêng tiêu chí TRANSPARENCY: yếu tố này chủ yếu thể hiện qua HÌNH ẢNH/hậu trường '
                    'khi lên video (cảnh quay xưởng, quy trình thật...) — một kịch bản CÂU CHỮ thuần tuý '
                    'thường không thể hiện được điều này. Nếu không tìm thấy bằng chứng rõ trong văn bản, '
                    'vẫn chấm level ≥ 3 (không chấm thấp như thể content thiếu sót), evidence ghi rõ lý do '
                    '(VD: "không có bằng chứng trong text — yếu tố này thuộc hình ảnh/hậu trường khi lên '
                    'video"); chỉ chấm level thấp khi văn bản có dấu hiệu NGƯỢC LẠI rõ ràng (che giấu, '
                    'không trung thực).\n'
                    'Riêng tiêu chí UNBIASED_AUTHORITY: lời tự quảng cáo/tự nhận chuyên môn của chính '
                    'thương hiệu ("chúng tôi là chuyên gia", "đội ngũ giàu kinh nghiệm") KHÔNG được tính '
                    'là bằng chứng — phải là chứng thực từ BÊN NGOÀI (chuyên gia độc lập, KOL, chứng chỉ/'
                    'giải thưởng do bên thứ 3 cấp) mới được chấm level cao.'
                )
            else:
                rule_text = base_rule_text

            codes = ",".join(it["code"] for it in items)
            shape = (
                f'{{ "{group_key}": [ {{"code": "...", "level": 0-5, "evidence": "...", "reasoning": "..."}}, '
                f'... đủ {len(items)} code {codes} ] }}'
            )

        return f"""Phân tích kịch bản content dưới đây theo khung PAAST — CHỈ nhóm tiêu chí sau đây. Với MỖI tiêu chí,
chấm mức độ triển khai và trích dẫn NGUYÊN VĂN câu trong content làm bằng chứng (không diễn giải lại,
không bịa câu không có trong content).

KỊCH BẢN CẦN PHÂN TÍCH:
\"\"\"
{content}
\"\"\"

{group_desc}

{rule_text}

Trả về DUY NHẤT một JSON object theo đúng shape sau, không thêm text giải thích ngoài JSON:
{shape}"""

    def _classify_group(
        self,
        content: str,
        group_key: str,
        group_label: str,
        items: List[Dict[str, str]],
        status_options: str,
        max_tokens: int,
        deadline: float,
        extra_keys: Optional[List[str]] = None,
    ):
        """
        Phân loại 1 nhóm, tự thử lại TẠI CHỖ tối đa MAX_GROUP_ATTEMPTS lượt.

        Thử lại trong worker của chính nhóm này thay vì để BE chạy lại cả 5 nhóm: retry ở BE
        vứt bỏ cả 4 nhóm vừa thành công rồi tung lại xúc xắc 5 mặt, trong khi lỗi ở đây thường
        là lỗi ngẫu nhiên của riêng 1 nhóm (429, JSON hỏng) — thử lại đúng nhóm đó rẻ hơn nhiều.

        Ngân sách kép: DEADLINE chung cho cả lượt phân tích, CỘNG trần
        PER_ATTEMPT_TIMEOUT_CAP_S cho từng lượt. Trần mỗi lượt là bắt buộc — nếu để một lượt
        dùng trọn thời gian còn lại thì một lượt bất thường sẽ ăn sạch budget và vòng thử lại
        này không bao giờ chạy được (đúng lỗi của bản sửa đầu tiên).
        """
        system_msg = (
            "Bạn là chuyên gia phân tích content marketing theo khung PAAST. "
            "Chỉ trả JSON hợp lệ theo đúng shape được yêu cầu, không thêm markdown fence, không thêm lời giải thích."
        )
        prompt = self._build_group_prompt(content, group_key, group_label, items, status_options)
        last_err: Optional[str] = None

        for attempt in range(1, MAX_GROUP_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_RETRY_BUDGET_S:
                break

            try:
                raw = self._gen._call_deepseek_checked(
                    prompt=prompt,
                    system_msg=system_msg,
                    # temperature=0: phân loại phải TẤT ĐỊNH — cùng 1 nội dung phải luôn ra cùng
                    # 1 điểm. Ở 0.2, thực đo 15 lượt trên CÙNG một kịch bản cho ra điểm dao động
                    # 80-93, khiến người dùng bấm "chấm lại" là thấy điểm khác dù không sửa gì.
                    # Đây là tác vụ gán nhãn theo rubric cố định, không phải sinh nội dung nên
                    # không cần đa dạng đầu ra.
                    temperature=0,
                    max_tokens=max_tokens,
                    # Cắt trần mỗi lượt để 1 lượt bất thường không nuốt trọn ngân sách, còn chừa
                    # chỗ cho lượt thử lại — thực đo lượt bình thường chỉ ~9s nên trần này rất rộng.
                    timeout=int(min(remaining, PER_ATTEMPT_TIMEOUT_CAP_S)),
                    log_prefix=f"PAAST analyze/{group_key} lượt {attempt}/{MAX_GROUP_ATTEMPTS} (DeepSeek)",
                    extra_params=DISABLE_THINKING_PARAMS,
                )
            except DeepSeekError as e:
                last_err = str(e)
                # Lỗi tất định (request sai, thiếu API key) — thử lại chỉ tốn thời gian mà kết
                # quả không đổi. Các loại còn lại (timeout/429/5xx/mạng/parse) đều ngẫu nhiên.
                if e.kind in ('client', 'no_key'):
                    raise RuntimeError(f"nhóm {group_key}: {e}") from e
                self.logger.warning(
                    f"PAAST analyze/{group_key}: lượt {attempt}/{MAX_GROUP_ATTEMPTS} hỏng "
                    f"(kind={e.kind}): {e}"
                )
                continue

            parsed = self._gen._extract_json_dict(raw)
            if not parsed:
                # LLM trả text không phải JSON hợp lệ — không tất định, lượt sau thường ổn.
                last_err = "không parse được JSON từ phản hồi LLM"
                self.logger.warning(
                    f"PAAST analyze/{group_key}: lượt {attempt}/{MAX_GROUP_ATTEMPTS} — {last_err}"
                )
                continue

            if attempt > 1:
                self.logger.warning(f"PAAST analyze/{group_key}: thành công ở lượt {attempt}/{MAX_GROUP_ATTEMPTS}")
            result_items = parsed.get(group_key, [])
            if extra_keys:
                return result_items, {k: parsed.get(k) for k in extra_keys}
            return result_items

        raise RuntimeError(f"nhóm {group_key}: {last_err or 'hết ngân sách thời gian'}")

    @staticmethod
    def _build_video_realism_prompt(content: str) -> str:
        return f"""Đọc kịch bản content dưới đây MỘT LƯỢT như thể bạn sắp XEM nó dưới dạng video ngắn
(TikTok/Reels, có hình ảnh, giọng đọc, nhịp cắt) — không phải để tìm từ khoá hay đánh giá câu chữ trên giấy.

KỊCH BẢN CẦN PHÂN TÍCH:
\"\"\"
{content}
\"\"\"

Đưa ra 4 nhận định, LUÔN CỤ THỂ — chỉ đích danh đoạn nào trong content gây ra nhận định đó, tránh
nhận xét chung chung kiểu "content khá ổn":
- opening_beat: câu/hình ảnh mở đầu (1-3 giây đầu khi lướt điện thoại) có thực sự khiến người xem
  dừng lại không? Nếu câu mở quá dài hoặc cấu trúc phức tạp, nêu rõ rủi ro không truyền tải kịp
  trong khung thời gian cực ngắn đó khi đọc thành lời.
- pacing_note: nhịp độ toàn bài — có đoạn nào lan man/dồn quá nhiều ý trước khi tới payoff, khiến
  người xem có thể bỏ ngang trước khi "wow" xuất hiện không?
- show_vs_tell: phân biệt phần nào đang "kể" (exposition, câu chữ mô tả cảm xúc/thông tin) so với
  phần nào thực sự "cho xem" (có scene/hình ảnh/hành động cụ thể có thể quay được). Cảnh báo nếu
  insight chính đang dựa hoàn toàn vào lời kể thay vì hình ảnh có thể quay.
- payoff_note: đoạn kết/khoảnh khắc chốt có thực sự tạo hiệu ứng "wow" khi XEM hay chỉ là câu chữ
  hay khi ĐỌC? Nếu payoff phụ thuộc vào việc đọc lại/suy ngẫm (chỉ hiệu quả trên văn bản, không hiệu
  quả khi xem lướt qua) → cảnh báo rõ.

Từ 4 nhận định trên, kết luận "overall_feasibility": nếu quay ĐÚNG y kịch bản này thành video
30-60 giây, khả năng đạt hiệu ứng như trên giấy là "realistic" (khả thi) | "needs-adjustment"
(cần điều chỉnh trước khi quay) | "high-risk" (rủi ro cao, dễ "chết" khi lên hình).

Trả về DUY NHẤT một JSON object theo đúng shape sau, không thêm text ngoài JSON:
{{ "video_realism": {{ "opening_beat": "...", "pacing_note": "...", "show_vs_tell": "...", "payoff_note": "...", "overall_feasibility": "realistic|needs-adjustment|high-risk" }} }}"""

    def _classify_video_realism(self, content: str, deadline: float) -> Dict[str, Any]:
        """Mô phỏng 'xem như video thật' — độc lập với 5 lớp PAAST (business rule §4 patch v2.1).
        Cùng cơ chế thử lại tại chỗ với _classify_group, tách riêng vì shape khác (1 object 5
        field, không phải mảng theo code)."""
        system_msg = (
            "Bạn là Content Strategist review video ngắn theo chuẩn PAAST. "
            "Chỉ trả JSON hợp lệ theo đúng shape được yêu cầu, không thêm markdown fence, không thêm lời giải thích."
        )
        prompt = self._build_video_realism_prompt(content)
        last_err: Optional[str] = None

        for attempt in range(1, MAX_GROUP_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_RETRY_BUDGET_S:
                break

            try:
                raw = self._gen._call_deepseek_checked(
                    prompt=prompt,
                    system_msg=system_msg,
                    temperature=0,
                    max_tokens=VIDEO_REALISM_MAX_TOKENS,
                    timeout=int(min(remaining, PER_ATTEMPT_TIMEOUT_CAP_S)),
                    log_prefix=f"PAAST analyze/video_realism lượt {attempt}/{MAX_GROUP_ATTEMPTS} (DeepSeek)",
                    extra_params=DISABLE_THINKING_PARAMS,
                )
            except DeepSeekError as e:
                last_err = str(e)
                if e.kind in ('client', 'no_key'):
                    raise RuntimeError(f"video_realism: {e}") from e
                self.logger.warning(
                    f"PAAST analyze/video_realism: lượt {attempt}/{MAX_GROUP_ATTEMPTS} hỏng "
                    f"(kind={e.kind}): {e}"
                )
                continue

            parsed = self._gen._extract_json_dict(raw)
            if not parsed:
                last_err = "không parse được JSON từ phản hồi LLM"
                self.logger.warning(
                    f"PAAST analyze/video_realism: lượt {attempt}/{MAX_GROUP_ATTEMPTS} — {last_err}"
                )
                continue

            if attempt > 1:
                self.logger.warning(f"PAAST analyze/video_realism: thành công ở lượt {attempt}/{MAX_GROUP_ATTEMPTS}")
            return parsed.get("video_realism", {})

        raise RuntimeError(f"video_realism: {last_err or 'hết ngân sách thời gian'}")

    def _classify(self, content: str, timeout_s: int) -> Dict[str, Any]:
        """Chạy 5 lệnh gọi LLM (1 lệnh/lớp) SONG SONG với 1 lệnh Video Realism Check (patch v2.1,
        business §4) — 6 lệnh tổng cộng, thay vì 1 lệnh gộp cả 30+6 tiêu chí + realism: thời gian
        chờ = lệnh chậm nhất trong 6, không phải tổng cộng dồn (xem CLASSIFICATION_GROUPS).

        `timeout_s` là ngân sách cho phần gọi DeepSeek, đã trừ biên an toàn so với timeout mà BE
        cho phép. 6 lệnh chạy song song và dùng CHUNG một deadline nên tổng thời gian vẫn nằm
        trong ngân sách đó dù mỗi lệnh có thể thử lại vài lượt.

        KHÔNG degrade partial: 1 nhóm hỏng sau khi đã tự thử lại ⇒ hỏng cả lượt (quyết định
        nghiệp vụ — thà báo lỗi còn hơn trả điểm thiếu lớp, vì điểm thiếu lớp luôn thấp hơn
        thực tế mà người dùng không có cách nào biết). Video Realism Check áp dụng CÙNG nguyên
        tắc — patch v2.1 yêu cầu nó "luôn luôn xuất hiện", nên hỏng cũng phải báo lỗi rõ thay vì
        âm thầm thiếu field trong response.
        """
        deadline = time.monotonic() + timeout_s
        result: Dict[str, Any] = {}
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=len(CLASSIFICATION_GROUPS) + 1) as executor:
            future_to_key = {
                executor.submit(
                    self._classify_group, content, key, label, items, status_options, max_tokens, deadline, extra_keys
                ): key
                for key, label, items, status_options, max_tokens, extra_keys in CLASSIFICATION_GROUPS
            }
            future_to_key[executor.submit(self._classify_video_realism, content, deadline)] = "video_realism"

            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    res = future.result()
                    if isinstance(res, tuple):
                        result[key], result[f"{key}_meta"] = res
                    else:
                        result[key] = res
                except Exception as e:
                    errors.append(str(e))

        if errors:
            raise RuntimeError("Lỗi phân tích PAAST: " + "; ".join(errors))
        return result

    @staticmethod
    def _index_by_code(items: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        if not items:
            return {}
        return {it.get("code"): it for it in items if isinstance(it, dict) and it.get("code")}

    @staticmethod
    def _parse_level(raw_level: Any) -> int:
        """Chuẩn hoá "level" LLM trả về thành int 0-5 — mọi giá trị thiếu/hỏng đều rơi về 0 (KHÔNG
        CÓ), an toàn/bảo thủ hơn là mặc định lên mức giữa (tương tự cách status thiếu/hỏng rơi về
        "miss" ở bản pass/miss cũ)."""
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            return 0
        return max(0, min(5, level))

    @staticmethod
    def _select_prefer_statuses(levels_by_code: Dict[str, int]) -> Dict[str, str]:
        """Chọn primary/secondary/off THUẦN theo level (0-5) đã chấm cho từng insight — quyết định
        bằng code, không phó thác cho LLM tự gán nhãn status (cùng triết lý pure-function của
        compute_scores/coherence override — business rule không thể phó thác hoàn toàn cho LLM tự
        tuân thủ). Primary = insight có level cao nhất VÀ đạt ngưỡng PREFER_PRIMARY_LEVEL_THRESHOLD
        trở lên; nếu có ≥2 insight đồng hạng cao nhất, đó CHÍNH LÀ dấu hiệu content chưa hội tụ —
        gán TẤT CẢ các insight đó "primary" để cơ chế override (primary_count > 1 ⇒ is_coherent =
        False) ở _normalize_classification tự bắt lỗi này, không cần logic riêng cho trường hợp
        này. Secondary = insight cao thứ nhì trong số còn lại, CHỈ gán khi đạt ngưỡng
        PREFER_SECONDARY_LEVEL_THRESHOLD trở lên VÀ không bị đồng hạng (đồng hạng ở vị trí phụ trợ
        không phải lỗi hội tụ — chỉ đơn giản bỏ trống secondary để tránh chọn tuỳ tiện).
        """
        codes_in_order = [d["code"] for d in PREFER_INSIGHTS]
        statuses = {c: "off" for c in codes_in_order}

        max_level = max(levels_by_code.get(c, 0) for c in codes_in_order)
        if max_level < PREFER_PRIMARY_LEVEL_THRESHOLD:
            return statuses  # không insight nào đủ mạnh để làm chủ đạo

        top_codes = [c for c in codes_in_order if levels_by_code.get(c, 0) == max_level]
        if len(top_codes) > 1:
            for c in top_codes:
                statuses[c] = "primary"
            return statuses

        primary_code = top_codes[0]
        statuses[primary_code] = "primary"

        remaining = [c for c in codes_in_order if c != primary_code]
        if not remaining:
            return statuses
        second_level = max(levels_by_code.get(c, 0) for c in remaining)
        if second_level < PREFER_SECONDARY_LEVEL_THRESHOLD:
            return statuses
        second_codes = [c for c in remaining if levels_by_code.get(c, 0) == second_level]
        if len(second_codes) == 1:
            statuses[second_codes[0]] = "secondary"
        return statuses

    def _normalize_classification(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Đảm bảo đủ đúng 30+6 tiêu chí, kể cả khi LLM trả thiếu — thiếu thì coi là level 0 (miss/off).

        Patch v3: mọi tiêu chí giờ chấm "level" 0-5 (thay pass/miss nhị phân) — LLM chỉ chấm level,
        "status" (pass/miss/na hoặc primary/secondary/off) và điểm số đều SUY RA bằng hàm thuần ở
        đây/compute_scores, không phó thác cho LLM tự gán nhãn (nguồn nghiệp vụ: tài liệu PAAST
        "Quy tắc 2 — Có nhưng yếu ≠ điểm cao" + "Quy tắc 3 — Không tự suy diễn").
        """
        prefer_by_code = self._index_by_code(raw.get("prefer"))
        prefer_levels = {defn["code"]: self._parse_level((prefer_by_code.get(defn["code"], {})).get("level")) for defn in PREFER_INSIGHTS}
        prefer_statuses = self._select_prefer_statuses(prefer_levels)

        prefer_insights = []
        for defn in PREFER_INSIGHTS:
            code = defn["code"]
            item = prefer_by_code.get(code, {})
            level = prefer_levels[code]
            prefer_insights.append({
                "code": code,
                "name_en": defn["name_en"],
                "name_vi": defn["name_vi"],
                "status": prefer_statuses[code],
                "level": level,
                "level_label": LEVEL_LABELS[level],
                # Giữ description/reasoning/evidence cho MỌI status (kể cả off) — người dùng cần
                # biết TẠI SAO 1 insight bị xếp off, không chỉ những insight được chọn.
                "description": item.get("description", ""),
                "reasoning": item.get("reasoning", ""),
                "evidence_sentences": item.get("evidence_sentences", []),
            })

        primary_count = sum(1 for it in prefer_insights if it["status"] == "primary")
        secondary_count = sum(1 for it in prefer_insights if it["status"] == "secondary")

        prefer_meta = raw.get("prefer_meta") or {}
        coherence_raw = prefer_meta.get("coherence") or {}
        is_coherent = coherence_raw.get("is_coherent")
        if not isinstance(is_coherent, bool):
            # Model không trả field này — không phạt oan, coi như coherent cho tới khi có bằng
            # chứng ngược lại (chính là nhánh primary_count > 1 ngay dưới đây).
            is_coherent = True
        warning = (coherence_raw.get("warning") or "").strip()

        # Ràng buộc cứng (business §3.1): >1 primary LUÔN là dấu hiệu chưa hội tụ, bất kể model có
        # tự đánh dấu coherence hay không. Từ patch v3, primary_count > 1 chỉ có thể xảy ra khi
        # _select_prefer_statuses() thấy ≥2 insight đồng hạng cao nhất về level — vẫn giữ override
        # này làm lưới an toàn (không dựa hoàn toàn vào việc model tự giác đánh dấu coherence).
        if primary_count > 1:
            is_coherent = False
            auto_note = (
                f"Tự động phát hiện: {primary_count} insight cùng được gán \"primary\" trong khi "
                "1 content chỉ được có đúng 1 insight chủ đạo — content chưa hội tụ về 1 trọng tâm."
            )
            warning = f"{warning} {auto_note}".strip() if warning else auto_note

        coherence: Dict[str, Any] = {"is_coherent": is_coherent}
        if not is_coherent:
            coherence["warning"] = warning or "Nội dung có dấu hiệu đổi trọng tâm giữa chừng, chưa hội tụ về 1 insight chủ đạo."

        wow_strength = prefer_meta.get("wow_strength")
        if wow_strength not in ("strong", "moderate", "weak"):
            wow_strength = "moderate"

        prefer = {
            "insights": prefer_insights,
            "primary_count": primary_count,
            "secondary_count": secondary_count,
            "takeaway_statement": (prefer_meta.get("takeaway_statement") or "").strip(),
            "wow_strength": wow_strength,
            "coherence": coherence,
        }

        def normalize_criteria_group(group_key: str, definitions: List[Dict[str, str]]) -> List[Dict[str, Any]]:
            by_code = self._index_by_code(raw.get(group_key))
            out = []
            for defn in definitions:
                item = by_code.get(defn["code"], {})
                level = self._parse_level(item.get("level"))
                status = "pass" if level >= LEVEL_PASS_THRESHOLD else "miss"
                evidence = item.get("evidence", "")
                if status == "miss" and not evidence:
                    evidence = f"Gợi ý — thêm yếu tố thể hiện \"{defn['name_vi']}\": {defn['signal']}"
                reasoning = item.get("reasoning", "")
                out.append({
                    "code": defn["code"],
                    "name_en": defn["name_en"],
                    "name_vi": defn["name_vi"],
                    "status": status,
                    "level": level,
                    "level_label": LEVEL_LABELS[level],
                    "evidence": evidence,
                    "reasoning": reasoning,
                })
            return out

        action = normalize_criteria_group("action", ACTION_CRITERIA)
        acknowledge = normalize_criteria_group("acknowledge", ACKNOWLEDGE_CRITERIA)
        trust = normalize_criteria_group("trust", TRUST_CRITERIA)

        # TRANSPARENCY chủ yếu thể hiện qua hình ảnh/hậu trường khi lên video — một kịch bản chỉ có
        # câu chữ thường không thể hiện được điều này. Không phạt oan: nếu model chấm level thấp vì
        # không tìm thấy bằng chứng, ép level lên đúng ngưỡng LEVEL_PASS_THRESHOLD tại đây thay vì
        # chỉ dựa vào prompt tự giác — cùng nguyên tắc với override coherence ở trên (business rule
        # không thể phó thác hoàn toàn cho LLM tự tuân thủ). Ép ĐÚNG NGƯỠNG (không ép lên 5) vì đây
        # là "miễn trừ ở mức đạt", không phải xác nhận Transparency được triển khai xuất sắc.
        for _it in trust:
            if _it["code"] == "TRANSPARENCY" and _it["level"] < LEVEL_PASS_THRESHOLD:
                _it["level"] = LEVEL_PASS_THRESHOLD
                _it["level_label"] = LEVEL_LABELS[LEVEL_PASS_THRESHOLD]
                _it["status"] = "pass"
                _it["evidence"] = (
                    "Không có bằng chứng minh bạch rõ trong văn bản — Transparency chủ yếu thể hiện qua "
                    "hình ảnh/hậu trường khi lên video, nên kịch bản dạng câu chữ mặc định tính đạt."
                )
                if not _it["reasoning"]:
                    _it["reasoning"] = (
                        "Minh bạch cần production (hình ảnh hậu trường) để thể hiện đầy đủ — không thể "
                        "đòi hỏi từ một kịch bản chỉ có câu chữ."
                    )
                break

        stick_text = normalize_criteria_group("stick", STICK_TEXT_DETECTABLE_CRITERIA)
        stick_na = [
            {
                "code": defn["code"],
                "name_en": defn["name_en"],
                "name_vi": defn["name_vi"],
                "status": "na",
                "level": None,
                "level_label": None,
                "evidence": "Cần production — không detect được từ text thuần.",
                "reasoning": "",
            }
            for defn in STICK_PRODUCTION_ONLY_CRITERIA
        ]
        stick = stick_text + stick_na

        video_realism_raw = raw.get("video_realism") or {}
        overall_feasibility = video_realism_raw.get("overall_feasibility")
        if overall_feasibility not in ("realistic", "needs-adjustment", "high-risk"):
            overall_feasibility = "needs-adjustment"
        video_realism = {
            "opening_beat": (video_realism_raw.get("opening_beat") or "").strip(),
            "pacing_note": (video_realism_raw.get("pacing_note") or "").strip(),
            "show_vs_tell": (video_realism_raw.get("show_vs_tell") or "").strip(),
            "payoff_note": (video_realism_raw.get("payoff_note") or "").strip(),
            "overall_feasibility": overall_feasibility,
        }

        return {
            "prefer": prefer,
            "action": action,
            "acknowledge": acknowledge,
            "stick": stick,
            "trust": trust,
            "video_realism": video_realism,
        }

    # ------------------------------------------------------------------
    # Score computation — thuần Python, không qua LLM (business doc §2, §4, §7)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_scores(classification: Dict[str, Any]) -> Dict[str, Any]:
        """
        Tính điểm 0-100 theo mô hình chấm GRADED (patch v3, thay pass/miss nhị phân của v2.1).
        Trọng số 5 lớp GIỮ NGUYÊN như v2.1: Prefer 25 / Action 25 / Acknowledge 20 / Stick 15 /
        Trust 15 — phản ánh đúng mức nhấn mạnh trong ghi chú training gốc (đổi insight = fail,
        hook quyết định có được xem hết hay không); tài liệu nghiệp vụ PAAST đầy đủ hơn (patch v3)
        đề xuất 20 đều nhau nhưng không phủ nhận lý do trọng số lệch này nên vẫn giữ.

        Action/Acknowledge/Stick/Trust: mỗi tiêu chí giờ có "level" 0-5 (không còn chỉ pass/miss)
        — điểm lớp = điểm tối đa × (tổng level đã chấm / tổng level tối đa có thể đạt). Đây chính
        là cách vận hành hoá "Quy tắc 2 — Có nhưng yếu ≠ điểm cao" của tài liệu nghiệp vụ: 6 tiêu
        chí đều chỉ đạt mức "Khá" (level 3/5) giờ chỉ được 60% điểm lớp, KHÔNG còn tự động full
        điểm như khi hệ pass/miss cũ coi "Khá" và "Xuất sắc" là cùng 1 kết quả "pass".

        Prefer GIỮ NGUYÊN cơ chế "1 chủ + 1 phụ + coherence hard-gate" của v2.1 (không chuyển
        sang chấm 6 tiêu chí độc lập như tài liệu nghiệp vụ mô tả chung cho các lớp khác — quyết
        định giữ cơ chế này vì nó vận hành hoá đúng cảnh báo "đổi insight = fail" rất cụ thể của
        ghi chú training gốc PAAST, mà tài liệu nghiệp vụ không hề phủ nhận, chỉ là không lặp lại).
        Nhưng giờ điểm trong 2 "khe" chủ đạo/phụ trợ không còn full 12.5đ máy móc chỉ vì ĐƯỢC CHỌN
        làm primary/secondary — điểm mỗi khe tỷ lệ theo LEVEL của chính insight đó:
          - Hard rule KHÔNG ĐỔI: coherence.is_coherent = false ⇒ Prefer = 0/25 NGAY LẬP TỨC (lỗi
            nền tảng "đổi insight", không phải tiêu chí phụ có thể bù bằng cái khác).
          - Coherent: prefer_raw = 25 × [(primary.level/5 nếu primary_count==1 else 0) +
            (secondary.level/5 nếu secondary_count==1 else 0)] / 2. Ví dụ: primary level=5 +
            secondary level=5 ⇒ 25/25 (như cũ); nhưng primary level=3 (chỉ "Khá", vẫn đủ ngưỡng để
            được CHỌN làm primary) + không có secondary ⇒ chỉ 25×(3/5)/2 ≈ 7.5/25, thay vì 12.5/25
            cứng như công thức v2.1 cũ.

        Tổng điểm được cộng từ giá trị CHƯA làm tròn của từng lớp rồi mới làm tròn 1 lần duy nhất
        ở cuối — làm tròn từng lớp trước rồi cộng lại sẽ lệch. Điểm hiển thị từng lớp (`score`)
        vẫn làm tròn 1 chữ số thập phân riêng để trình bày.
        """
        prefer = classification["prefer"]
        primary_count = prefer["primary_count"]
        secondary_count = prefer["secondary_count"]
        is_coherent = prefer["coherence"]["is_coherent"]

        if not is_coherent:
            prefer_raw = 0.0
        else:
            primary_item = next((it for it in prefer["insights"] if it["status"] == "primary"), None)
            secondary_item = next((it for it in prefer["insights"] if it["status"] == "secondary"), None)
            primary_term = (primary_item["level"] / 5.0) if (primary_count == 1 and primary_item) else 0.0
            secondary_term = (secondary_item["level"] / 5.0) if (secondary_count == 1 and secondary_item) else 0.0
            prefer_raw = 25.0 * (primary_term + secondary_term) / 2

        def level_sum_of(items: List[Dict[str, Any]]) -> int:
            return sum(it["level"] for it in items)

        def pass_count_of(items: List[Dict[str, Any]]) -> int:
            return sum(1 for it in items if it["status"] == "pass")

        action_items = classification["action"]
        acknowledge_items = classification["acknowledge"]
        trust_items = classification["trust"]

        action_raw = (level_sum_of(action_items) / (len(action_items) * 5)) * 25
        acknowledge_raw = (level_sum_of(acknowledge_items) / (len(acknowledge_items) * 5)) * 20
        trust_raw = (level_sum_of(trust_items) / (len(trust_items) * 5)) * 15

        action_pass = pass_count_of(action_items)
        acknowledge_pass = pass_count_of(acknowledge_items)
        trust_pass = pass_count_of(trust_items)

        stick_text_detectable = [it for it in classification["stick"] if it["status"] != "na"]
        stick_pass = sum(1 for it in stick_text_detectable if it["status"] == "pass")
        stick_raw = (
            (level_sum_of(stick_text_detectable) / (len(stick_text_detectable) * 5)) * 15
            if stick_text_detectable else 0.0
        )

        total = round(prefer_raw + action_raw + acknowledge_raw + stick_raw + trust_raw)
        band = (
            "ready" if total >= 90 else
            "close" if total >= 70 else
            "needs-work" if total >= 50 else
            "not-ready"
        )

        return {
            "prefer": {
                "score": round(prefer_raw, 1), "max": 25,
                "primary_count": primary_count, "secondary_count": secondary_count,
                "is_coherent": is_coherent,
            },
            "action": {"score": round(action_raw, 1), "max": 25, "pass_count": action_pass},
            "acknowledge": {"score": round(acknowledge_raw, 1), "max": 20, "pass_count": acknowledge_pass},
            "stick": {"score": round(stick_raw, 1), "max": 15, "pass_count": stick_pass, "text_detectable_count": len(stick_text_detectable)},
            "trust": {"score": round(trust_raw, 1), "max": 15, "pass_count": trust_pass},
            "total_score": total,
            "band": band,
        }

    # ------------------------------------------------------------------
    # Verdict — đạt/chưa đạt chuẩn PAAST (business doc §1.3, §5.2/§5.3)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_verdict(scores: Dict[str, Any]) -> Dict[str, Any]:
        """
        Đạt chuẩn PAAST khi CẢ 5 lớp đều có ít nhất 1 tiêu chí đạt — KHÔNG dùng ngưỡng điểm
        tổng, vì điểm cao vẫn có thể do dồn hết vào vài lớp trong khi bỏ trắng hẳn 1 lớp khác
        (business doc §1.3/§5.2).

        Riêng Prefer (patch v2.1, §3.1/§6): đòi hỏi ĐÚNG 1 insight `primary` VÀ
        `coherence.is_coherent === true`. Không còn chấp nhận `primary_count >= 1` như bản
        trước — 2 primary cùng lúc nghĩa là content không hội tụ về 1 trọng tâm (đúng lỗi
        "đổi insight = fail" mà ghi chú training gốc cảnh báo), nên phải fail dù có primary.
        """
        passed_layers: List[str] = []
        missing_layers: List[str] = []

        if scores["prefer"]["primary_count"] == 1 and scores["prefer"]["is_coherent"]:
            passed_layers.append("prefer")
        else:
            missing_layers.append("prefer")

        for layer in ("action", "acknowledge", "trust"):
            if scores[layer]["pass_count"] > 0:
                passed_layers.append(layer)
            else:
                missing_layers.append(layer)

        # Nếu Stick không có tiêu chí nào detect được từ text (text_detectable_count == 0),
        # không thể chấm — không tính lớp này là lý do "chưa đạt" (spec §5.2).
        stick_detectable = scores["stick"]["text_detectable_count"]
        stick_pass = scores["stick"]["pass_count"]
        if stick_detectable == 0 or stick_pass > 0:
            passed_layers.append("stick")
        else:
            missing_layers.append("stick")

        return {
            "passed": len(missing_layers) == 0,
            "passed_layers": passed_layers,
            "missing_layers": missing_layers,
        }

    # ------------------------------------------------------------------
    # CTA compliance — regex thuần, không qua LLM (business doc §9)
    # ------------------------------------------------------------------

    @staticmethod
    def check_cta_compliance(text: str) -> Dict[str, Any]:
        matches = []
        for pattern in CTA_VIOLATION_PATTERNS:
            found = re.findall(pattern, text, flags=re.IGNORECASE)
            if found:
                matches.append(pattern)
        return {"detected": len(matches) > 0, "matches": matches}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _inner_budget_s(timeout_seconds: Optional[int]) -> int:
        """
        Quy đổi timeout NGOÀI (BE cho phép cả request) thành ngân sách TRONG cho DeepSeek,
        luôn nhỏ hơn ít nhất DEEPSEEK_TIMEOUT_MARGIN_S để Django còn kịp normalize + serialize
        + trả response trước khi axios của BE bung.
        """
        outer = timeout_seconds if timeout_seconds and timeout_seconds > 0 else DEFAULT_ANALYZE_TIMEOUT_S
        return max(MIN_RETRY_BUDGET_S, outer - DEEPSEEK_TIMEOUT_MARGIN_S)

    def analyze(self, content: str, timeout_seconds: Optional[int] = None) -> Dict[str, Any]:
        """`timeout_seconds` = timeout NGOÀI mà BE cho phép; biên an toàn trừ ở _inner_budget_s."""
        return self._analyze_inner(content, self._inner_budget_s(timeout_seconds))

    def _analyze_inner(self, content: str, inner_budget_s: int) -> Dict[str, Any]:
        """Nhận thẳng ngân sách TRONG (đã trừ biên) — để `upgrade` chia budget cho 2 lượt gọi
        nối tiếp mà không bị trừ biên an toàn hai lần."""
        raw_classification = self._classify(content, inner_budget_s)
        classification = self._normalize_classification(raw_classification)
        scores = self.compute_scores(classification)
        verdict = self.compute_verdict(scores)
        cta = self.check_cta_compliance(content)

        return {
            "layers": {
                "prefer": {
                    "score": scores["prefer"]["score"],
                    "max": scores["prefer"]["max"],
                    "primary_count": scores["prefer"]["primary_count"],
                    "secondary_count": scores["prefer"]["secondary_count"],
                    "insights": classification["prefer"]["insights"],
                    "takeaway_statement": classification["prefer"]["takeaway_statement"],
                    "wow_strength": classification["prefer"]["wow_strength"],
                    "coherence": classification["prefer"]["coherence"],
                },
                "action": {**scores["action"], "criteria": classification["action"]},
                "acknowledge": {**scores["acknowledge"], "criteria": classification["acknowledge"]},
                "stick": {**scores["stick"], "criteria": classification["stick"]},
                "trust": {**scores["trust"], "criteria": classification["trust"]},
            },
            # Video Realism Check (patch v2.1, §4) — LUÔN xuất hiện, độc lập với verdict 5 lớp:
            # 1 content có thể "Đạt chuẩn PAAST" nhưng vẫn "chết" khi quay thành video thật.
            "video_realism": classification["video_realism"],
            "total_score": scores["total_score"],
            "score_band": scores["band"],
            "verdict": verdict,
            "cta_warning": cta,
            # BE ghi thẳng vào cột model_used. Trả từ đây vì AI mới là bên quyết model —
            # BE tự đoán thì bảng lịch sử ghi sai như trước.
            "model_used": DEEPSEEK_DEFAULT_MODEL,
        }

    def _write_upgrade_content(self, prompt: str, system_msg: str, budget_s: int) -> Dict[str, Any]:
        """
        Viết bản nâng cấp cho PAAST Analyzer độc lập (khác `_write_scripted_upgrade` — dùng cho
        content-transform), tự thử lại TẠI CHỖ tối đa MAX_SCRIPTED_WRITE_ATTEMPTS lượt — cùng
        nguyên tắc với `_classify_group`/`_write_scripted_upgrade`.

        TRƯỚC ĐÂY dùng `_call_deepseek_raw` (1 lượt DUY NHẤT, nuốt MỌI loại lỗi — timeout/429/
        5xx/JSON hỏng — thành cùng 1 câu "DeepSeek không phản hồi", KHÔNG thử lại). Đối chiếu
        lịch sử `paast_analysis_histories` thực tế: 100% lượt nâng cấp hỏng trong nhiều ngày,
        đều dừng ở ~40s = ĐÚNG BẰNG `write_budget` khi BE không gửi `timeout_seconds` (Django rơi
        về DEFAULT_ANALYZE_TIMEOUT_S=120s → write chỉ được 40% = 40s). Đây là TIMEOUT thật của 1
        lệnh reasoning-enabled + max_tokens=16000 — không có lượt thử lại nào nên hỏng là hỏng
        hẳn. Sửa 2 phía: BE tăng timeout + gửi đúng `timeout_seconds` (xem `paast.service.ts`),
        VÀ ở đây thêm thử lại — vì dù ngân sách đã đủ, timeout/429/5xx đơn lẻ vẫn có thể xảy ra
        (đúng lý do `_call_deepseek_checked` + retry tồn tại ở mọi chỗ gọi PAAST khác).
        """
        deadline = time.monotonic() + budget_s
        last_err: Optional[str] = None

        for attempt in range(1, MAX_SCRIPTED_WRITE_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_RETRY_BUDGET_S:
                break

            try:
                raw = self._gen._call_deepseek_checked(
                    prompt=prompt,
                    system_msg=system_msg,
                    temperature=0.4,
                    # KHÔNG tắt suy luận (khác phần phân loại): viết lại content là tác vụ sáng
                    # tạo, suy luận có giá trị thật. max_tokens=16000 khớp writeContentWithRetry
                    # của content-transform (cùng loại tác vụ).
                    max_tokens=16000,
                    timeout=int(remaining),
                    log_prefix=f"PAAST upgrade - viết lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS} (DeepSeek)",
                )
            except DeepSeekError as e:
                last_err = str(e)
                if e.kind in ('client', 'no_key'):
                    raise RuntimeError(f"viết bản nâng cấp PAAST: {e}") from e
                self.logger.warning(
                    f"PAAST upgrade - viết: lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS} hỏng "
                    f"(kind={e.kind}): {e}"
                )
                continue

            parsed = self._gen._extract_json_dict(raw)
            if not parsed or not parsed.get("upgraded_content"):
                last_err = "không parse được JSON kết quả nâng cấp từ phản hồi LLM"
                self.logger.warning(
                    f"PAAST upgrade - viết: lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS} — {last_err}"
                )
                continue

            if attempt > 1:
                self.logger.warning(f"PAAST upgrade - viết: thành công ở lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS}")
            return parsed

        raise RuntimeError(f"Không nâng cấp được content PAAST: {last_err or 'hết ngân sách thời gian'}")

    def upgrade(
        self,
        original_content: str,
        missing_elements: List[Dict[str, str]],
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        missing_elements: [{"layer": "acknowledge", "criterion": "STORY", "suggestion": "..."}]
        Caller (BE) chịu trách nhiệm loại bỏ các tiêu chí `na` của Stick trước khi gọi hàm này
        (business doc §11.2 — không thể "nâng cấp" phần cần production bằng cách sửa text).

        Endpoint này tốn 2 lượt gọi LLM NỐI TIẾP nhau (viết bản nâng cấp → chấm lại từ đầu bản
        mới), nên `timeout_seconds` được CHIA ĐÔI theo tỷ lệ chứ không cấp trọn cho từng lượt:
        trước đây bước viết cứng 60s + bước chấm cứng 120s = tệ nhất 180s trong khi BE chỉ chờ
        90s, tức đường này gần như không thể thành công với input nặng.
        """
        inner_total = self._inner_budget_s(timeout_seconds)
        # Bước viết thường nhanh hơn bước chấm (1 lệnh gọi so với 5 lệnh song song có retry).
        write_budget = max(MIN_RETRY_BUDGET_S, int(inner_total * 0.4))
        analyze_budget = max(MIN_RETRY_BUDGET_S, inner_total - write_budget)

        if not missing_elements:
            # Không có gì để thêm ⇒ bỏ qua bước viết, dồn trọn ngân sách cho bước chấm.
            new_analysis = self._analyze_inner(original_content, inner_total)
            return {
                "original": original_content,
                "upgraded": original_content,
                "changes_added": [],
                "new_analysis": new_analysis,
            }

        def _describe_missing(m: Dict[str, str]) -> str:
            defn = ALL_CRITERIA_BY_CODE.get(m.get("criterion", ""))
            name_vi = defn["name_vi"] if defn else m.get("criterion", "")
            signal = defn.get("signal", "") if defn else ""
            suggestion = (m.get("suggestion") or "").strip()
            line = f"- [{m.get('layer', '')}] {m.get('criterion', '')} — {name_vi}"
            if signal:
                line += f"\n  Bản chất tiêu chí (đọc để hiểu ĐÚNG tinh thần, không chỉ để nhét từ khoá): {signal}"
            if suggestion:
                line += f"\n  Gợi ý cụ thể lấy từ chính content gốc: {suggestion}"
            return line

        missing_list_text = "\n".join(_describe_missing(m) for m in missing_elements)
        prompt = f"""Bạn là copywriter giỏi, nâng cấp đoạn content sau để đạt đủ các tiêu chí PAAST còn thiếu.

Content gốc:
\"\"\"
{original_content}
\"\"\"

Các tiêu chí còn THIẾU cần bổ sung:
{missing_list_text}

YÊU CẦU CHẤT LƯỢNG (bắt buộc, quan trọng hơn việc nhét đủ ý):
1. Mỗi phần thêm là 1-3 câu VĂN THẬT — có hình ảnh/chi tiết/cảm xúc cụ thể lấy từ chính content gốc,
   không phải câu khẩu hiệu sáo rỗng chung chung ("chúng tôi luôn tận tâm...", "chất lượng tốt nhất...").
   TUYỆT ĐỐI không viết dạng liệt kê/gạch đầu dòng — phải hoà thành văn xuôi tự nhiên.
2. Chèn từng phần thêm vào ĐÚNG vị trí hợp lý trong mạch chuyện gốc (không dồn hết xuống cuối bài) —
   đọc lên phải liền mạch như được viết cùng một lúc với phần còn lại, không lộ vết ghép.
3. Giữ nguyên 100% văn phong, ngôi xưng, nhịp câu, độ dài câu trung bình của tác giả gốc.
4. Chỉ bổ sung — không viết lại, không rút gọn, không xoá câu nào của bản gốc.
5. Nếu 2 tiêu chí có thể lồng tự nhiên vào chung 1 câu/đoạn, hãy gộp lại — không cần mỗi tiêu chí
   một câu riêng nếu việc tách ra làm đoạn văn rời rạc, liệt kê máy móc.
6. Trước khi trả lời, tự kiểm tra lại: mỗi câu vừa thêm có thật sự khớp với "bản chất tiêu chí" ở trên
   không, hay chỉ đang nói chung chung cho có — nếu chưa khớp, viết lại câu đó.

Trả về DUY NHẤT một JSON object, không thêm text ngoài JSON:
{{
  "upgraded_content": "toàn bộ content mới, các đoạn/câu vừa thêm được bọc trong <add>...</add>",
  "changes_added": [ {{"layer": "acknowledge", "criterion": "STORY", "text": "tóm tắt ngắn gọn phần vừa thêm"}}, ... ]
}}"""
        system_msg = (
            "Bạn là copywriter giỏi, nâng cấp content theo khung PAAST. Ưu tiên chất lượng văn chương thật — "
            "câu văn tự nhiên, cụ thể, có cảm xúc, hoà vào mạch gốc — hơn là chèn đủ ý cho có. "
            "Chỉ bổ sung phần thiếu, không viết lại toàn bộ. "
            "Chỉ trả JSON hợp lệ, không markdown fence, không giải thích ngoài JSON."
        )
        parsed = self._write_upgrade_content(prompt, system_msg, write_budget)
        upgraded_content = parsed["upgraded_content"]
        changes_added = parsed.get("changes_added", [])

        # Không giả định điểm chắc chắn tăng — luôn phân tích lại bản đã nâng cấp (business doc §11.1).
        stripped_for_analysis = re.sub(r"</?add>", "", upgraded_content)
        new_analysis = self._analyze_inner(stripped_for_analysis, analyze_budget)

        return {
            "original": original_content,
            "upgraded": upgraded_content,
            "changes_added": changes_added,
            "new_analysis": new_analysis,
        }

    # ------------------------------------------------------------------
    # Nâng cấp cho content-transform (giữ giọng nhân vật) — KHÁC upgrade() ở trên
    # ------------------------------------------------------------------

    def _write_scripted_upgrade(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        budget_s: int,
    ) -> str:
        """
        Viết lại kịch bản (giữ nguyên giọng nhân vật — system_prompt/user_prompt đã được BE dựng
        sẵn qua buildPaastUpgradeSystemPrompt/buildPaastUpgradeUserPrompt), tự thử lại TẠI CHỖ
        tối đa MAX_SCRIPTED_WRITE_ATTEMPTS lượt — cùng nguyên tắc với _classify_group ở trên: lỗi
        thường là timeout/lỗi mạng ngẫu nhiên của model reasoning, thử lại ngay trong worker này
        rẻ hơn nhiều so với để BE gọi lại nguyên request (vốn sẽ chạy lại luôn cả bước chấm điểm
        nếu gộp retry ở tầng ngoài).

        Ném RuntimeError nếu hết ngân sách hoặc gặp lỗi tất định — khi đó KHÔNG có output_text
        nào được tạo ra, nên caller (view) mất trắng là đúng, không có gì để giữ lại.
        """
        deadline = time.monotonic() + budget_s
        last_err: Optional[str] = None

        for attempt in range(1, MAX_SCRIPTED_WRITE_ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining < MIN_RETRY_BUDGET_S:
                break

            try:
                raw = self._gen._call_deepseek_checked(
                    prompt=user_prompt,
                    system_msg=system_prompt,
                    max_tokens=max_tokens,
                    timeout=int(remaining),
                    log_prefix=f"Content transform upgrade - viết lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS} (DeepSeek)",
                )
            except DeepSeekError as e:
                last_err = str(e)
                if e.kind in ('client', 'no_key'):
                    raise RuntimeError(f"viết lại kịch bản nâng cấp: {e}") from e
                self.logger.warning(
                    f"Content transform upgrade - viết: lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS} "
                    f"hỏng (kind={e.kind}): {e}"
                )
                continue

            if not raw:
                last_err = "DeepSeek không phản hồi"
                continue

            if attempt > 1:
                self.logger.warning(
                    f"Content transform upgrade - viết: thành công ở lượt {attempt}/{MAX_SCRIPTED_WRITE_ATTEMPTS}"
                )
            return raw

        raise RuntimeError(f"viết lại kịch bản nâng cấp: {last_err or 'hết ngân sách thời gian'}")

    def upgrade_scripted(
        self,
        write_system_prompt: str,
        write_user_prompt: str,
        max_tokens: int = 16000,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Nâng cấp cho luồng "Chuyển đổi nội dung" (content-transform) — khác `upgrade()` ở trên:
          - `upgrade()` (PAAST Analyzer độc lập) TỰ build prompt trung tính + yêu cầu LLM trả JSON
            {upgraded_content, changes_added}.
          - Ở đây BE đã tự dựng `write_system_prompt`/`write_user_prompt` (giữ đúng giọng nhân vật
            — xem paast-upgrade.util.ts), và bước viết trả THẲNG văn bản kịch bản (cùng shape với
            /api/ai/transform-content/), không phải JSON.

        Gộp 2 lượt gọi LLM nối tiếp (viết lại rồi chấm PAAST bản mới) vào 1 request Django DUY
        NHẤT — trước đây BE tự gọi 2 request HTTP tuần tự (mỗi request lại tự retry riêng, tối đa
        6 round-trip cho 1 lần bấm nút). `timeout_seconds` (ngân sách NGOÀI mà BE cho phép) được
        chia theo cùng tỷ lệ 40% viết / 60% chấm như `upgrade()` — viết thường nhanh hơn (1 lệnh so
        với 5 lệnh song song có retry).

        QUAN TRỌNG — lỗi ở bước CHẤM không được làm mất kịch bản vừa viết (đúng hành vi cũ ở BE):
        bắt lỗi cục bộ quanh bước chấm, trả về `new_analysis=None` kèm `score_error` thay vì raise,
        để output_text vẫn được trả về nguyên vẹn cho caller lưu lại. Lỗi ở bước VIẾT thì raise
        thẳng — chưa có gì được tạo ra nên không có gì để giữ.
        """
        inner_total = self._inner_budget_s(timeout_seconds)
        write_budget = max(MIN_RETRY_BUDGET_S, int(inner_total * 0.4))
        analyze_budget = max(MIN_RETRY_BUDGET_S, inner_total - write_budget)

        new_output_text = self._write_scripted_upgrade(write_system_prompt, write_user_prompt, max_tokens, write_budget)

        new_analysis: Optional[Dict[str, Any]] = None
        score_error: Optional[str] = None
        try:
            new_analysis = self._analyze_inner(new_output_text, analyze_budget)
        except Exception as e:
            self.logger.error(f"Content transform upgrade - chấm điểm bản mới thất bại: {e}")
            score_error = str(e)

        return {
            "output_text": new_output_text,
            "new_analysis": new_analysis,
            "score_error": score_error,
        }


# ===========================================================================
# BẢN 2 — dành cho video kênh nội bộ
#
# Khác bản 1 ở ba điểm, đối chiếu trực tiếp với API của paast.vercel.app:
#   1. BỎ thang điểm 0–100. Bản mới chỉ đếm số element đạt (`elementCount`) và
#      kết luận đạt/chưa đạt. Trong phản hồi thật của họ không hề có `score`,
#      `totalScore` hay `total_score`.
#   2. THÊM 16 hook gợi ý thay thế, nằm trong `layers.action.hookSuggestions`.
#   3. Khoá JSON viết kiểu camelCase.
#
# Cố ý KHÔNG sửa `analyze()` cũ: task-auto đang chạy trên schema đó (snake_case
# + total_score). Hai bản sống song song, khi nào task-auto chuyển sang thì mới bỏ bản cũ.
# ===========================================================================

HOOK_GROUPS = [
    {"group": "Tò mò", "formula": "Có một chi tiết mà đa số bỏ qua…"},
    {"group": "Người nổi tiếng", "formula": "[Nhân vật] + hành động bất thường + khoảng trống"},
    {"group": "Sốc có dữ kiện", "formula": "Con số thật + đối tượng quen thuộc"},
    {"group": "Độc lạ", "formula": "Vật liệu/ý tưởng không thường đi cùng nhau"},
    {"group": "Giá trị thật", "formula": "Thứ đắt/quan trọng nhất không phải X mà là Y"},
    {"group": "Hiểu lầm", "formula": "Đa số nghĩ X, nhưng thực tế Y"},
    {"group": "Nghịch lý", "formula": "Hai đặc tính đối lập cùng tồn tại"},
    {"group": "Bí mật kỹ thuật", "formula": "Hiệu ứng nhìn thấy + kỹ thuật ẩn sau"},
    {"group": "Con số", "formula": "Số cụ thể + câu hỏi ý nghĩa"},
    {"group": "Tranh luận", "formula": "Hai cách nhìn đều có lý"},
    {"group": "Tâm lý", "formula": "Hành vi con người + nguyên nhân ẩn"},
    {"group": "Thử thách", "formula": "Yêu cầu quan sát/đoán từ người xem"},
    {"group": "Story", "formula": "Bắt đầu bằng khoảnh khắc quyết định"},
    {"group": "Quan điểm mạnh", "formula": "Lập trường + lý do sẽ chứng minh"},
    {"group": "So sánh", "formula": "Hai đối tượng dễ nhầm/đối lập"},
    {"group": "Hệ quả", "formula": "Nếu hiểu chi tiết này, cách nhìn sẽ thay đổi"},
]


class PaastAnalysisServiceV2(PaastAnalysisService):
    """Bản 2 — dùng lại toàn bộ phần phân loại của bản 1, chỉ đổi cách quy đổi và thêm hook."""

    def _build_hook_prompt(self, content: str) -> str:
        cong_thuc = "\n".join(
            f'{i + 1}. {h["group"]} — công thức: {h["formula"]}' for i, h in enumerate(HOOK_GROUPS)
        )
        return f"""Đây là kịch bản một video ngắn:

\"\"\"{content}\"\"\"

Viết lại câu HOOK MỞ ĐẦU cho video này theo ĐỦ 16 nhóm dưới đây. Mỗi nhóm đúng MỘT câu hook.

{cong_thuc}

Yêu cầu bắt buộc:
- Mỗi hook là một câu tiếng Việt có dấu, tối đa 25 từ, đọc lên nghe tự nhiên.
- Hook phải bám vào NỘI DUNG THẬT của kịch bản trên, không bịa thông tin không có.
- Đúng tinh thần công thức của nhóm đó.
- Giữ nguyên thứ tự và tên nhóm.

Trả về DUY NHẤT JSON, không markdown, không giải thích:
{{"hooks": [{{"group": "Tò mò", "example": "..."}}, ... đủ 16 phần tử ...]}}"""

    def _generate_hooks(self, content: str) -> List[Dict[str, str]]:
        """
        Sinh 16 hook. Hỏng thì trả về danh sách công thức KHÔNG kèm ví dụ thay vì ném lỗi —
        hook chỉ là phần gợi ý thêm, không đáng để làm hỏng cả bản phân tích đã chấm xong.
        """
        try:
            raw = self._gen._call_deepseek_raw(
                prompt=self._build_hook_prompt(content),
                system_msg=(
                    "Bạn là copywriter chuyên viết hook cho video ngắn. "
                    "Chỉ trả JSON hợp lệ, không markdown fence, không lời giải thích."
                ),
                temperature=0.8,  # cao hơn phần phân loại: đây là việc sáng tạo, cần đa dạng
                max_tokens=2048,
                timeout=60,
                log_prefix="PAAST v2/hooks (DeepSeek)",
            )
            parsed = self._gen._extract_json_dict(raw) if raw else None
            theo_nhom = {
                h.get("group"): (h.get("example") or "").strip()
                for h in (parsed or {}).get("hooks", [])
                if isinstance(h, dict)
            }
        except Exception as e:
            logger.warning("[PAAST v2] Sinh hook lỗi: %s", e)
            theo_nhom = {}

        return [
            {"group": h["group"], "formula": h["formula"], "example": theo_nhom.get(h["group"], "")}
            for h in HOOK_GROUPS
        ]

    @staticmethod
    def _sang_camel(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        doi = {"name_en": "nameEn", "name_vi": "nameVi", "evidence_sentences": "evidenceSentences", "level_label": "levelLabel"}
        return [{doi.get(k, k): v for k, v in it.items()} for it in items]

    @staticmethod
    def _dem_dat(items: List[Dict[str, Any]]) -> int:
        """Số tiêu chí ĐẠT. 'na' (không áp dụng cho văn bản) không tính là đạt lẫn không đạt."""
        return sum(1 for it in items if it.get("status") == "pass")

    def analyze_v2(self, content: str) -> Dict[str, Any]:
        # `_classify` cần timeout_s — dùng ngân sách mặc định vì bản 2 không nhận timeout_seconds
        # riêng từ caller (xem analyze_content_v2 view).
        classification = self._normalize_classification(self._classify(content, self._inner_budget_s(None)))

        # Patch v2.1 đổi classification["prefer"] từ mảng insight trần thành 1 object
        # {insights, primary_count, ...} (business §3.1) — bản 2 chỉ cần danh sách insight thô,
        # lấy từ "insights" thay vì coi cả object là mảng.
        prefer = classification["prefer"]["insights"]
        chu_dao = classification["prefer"]["primary_count"]
        phu_tro = classification["prefer"]["secondary_count"]

        # Đạt chuẩn khi cả 5 lớp đều có ít nhất một element, và Prefer phải có element CHỦ ĐẠO
        # — giữ đúng quy tắc của compute_verdict() bản 1, chỉ đổi cách diễn đạt ra ngoài.
        du_lop = (
            chu_dao >= 1
            and all(self._dem_dat(classification[k]) >= 1 for k in ("action", "acknowledge", "stick", "trust"))
        )

        return {
            "phien_ban": 2,
            "verdict": {
                "passed": du_lop,
                "title": "Đạt chuẩn PAAST" if du_lop else "Chưa đủ chuẩn PAAST",
                "subtitle": (
                    "Content có element ở cả 5 lớp — sẵn sàng publish."
                    if du_lop
                    else "Content còn thiếu element ở một số lớp — xem gợi ý bên dưới."
                ),
            },
            "layers": {
                "prefer": {
                    "leadParagraph": (content or "").strip()[:400],
                    "insights": self._sang_camel(prefer),
                    "primaryCount": chu_dao,
                    "secondaryCount": phu_tro,
                },
                "action": {
                    "elementCount": self._dem_dat(classification["action"]),
                    "criteria": self._sang_camel(classification["action"]),
                    "hookSuggestions": self._generate_hooks(content),
                },
                "acknowledge": {
                    "elementCount": self._dem_dat(classification["acknowledge"]),
                    "criteria": self._sang_camel(classification["acknowledge"]),
                },
                "stick": {
                    "elementCount": self._dem_dat(classification["stick"]),
                    "textDetectableCount": sum(
                        1 for it in classification["stick"] if it.get("status") != "na"
                    ),
                    "criteria": self._sang_camel(classification["stick"]),
                },
                "trust": {
                    "elementCount": self._dem_dat(classification["trust"]),
                    "criteria": self._sang_camel(classification["trust"]),
                },
            },
            "ctaWarning": self.check_cta_compliance(content),
        }
