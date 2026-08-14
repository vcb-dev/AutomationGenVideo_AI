"""
PAAST Content Analyzer — engine phân tích & chấm điểm.

Nguồn nghiệp vụ: PAAST_Business_Logic_Rules v1.1 (thang điểm 100, mỗi lớp tối đa 20).
Nguồn kỹ thuật/rubric detect: PAAST_Analyzer_Spec.md v1.0 (mục 5.1, 5.4, 5.5).

Thiết kế: LLM chỉ làm nhiệm vụ PHÂN LOẠI (status + evidence quote) cho từng tiêu chí.
Việc quy đổi phân loại đó thành điểm số (`compute_scores`) là hàm thuần Python,
không qua LLM — để công thức chấm điểm có một nguồn duy nhất, không lệch giữa
lần phân tích đầu và lần phân tích lại sau khi nâng cấp nội dung.
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
     "signal": "Nỗi đau/ước mơ, con số sốc, tình huống nguy hiểm, cảm hứng"},
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
     "signal": "Đoạn chạm nỗi đau/ước mơ/trải nghiệm → người đọc muốn thả like"},
    {"code": "ANSWER", "name_en": "Answer", "name_vi": "Đối thoại (Comment)",
     "signal": "Câu hỏi mở, mời chia sẻ quan điểm, gợi tranh luận"},
    {"code": "CONNECT", "name_en": "Connect", "name_vi": "Kết nối (Share)",
     "signal": "Câu chốt súc tích dễ trích, thay lời một nhóm, checklist share được"},
    {"code": "ENGAGE", "name_en": "Engage", "name_vi": "Gắn bó (Save)",
     "signal": "Tips/formula/checklist đủ dense để save"},
    {"code": "SEE_AGAIN", "name_en": "See Again", "name_vi": "Xem lại (Rewatch)",
     "signal": "Nhiều lớp info, chi tiết ẩn, câu chốt sâu đáng đọc lại"},
]

ACKNOWLEDGE_CRITERIA = [
    {"code": "BASICS", "name_en": "Basics", "name_vi": "Nền tảng cốt lõi",
     "signal": "\"Mình là ai, mình làm gì\" tự nhiên trong story"},
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
     "signal": "Chia sẻ hậu trường, quy trình, khó khăn thật"},
    {"code": "RESPONSIBILITY", "name_en": "Responsibility", "name_vi": "Trách nhiệm xã hội",
     "signal": "Cam kết môi trường, cộng đồng"},
    {"code": "UNBIASED_AUTHORITY", "name_en": "Unbiased Authority", "name_vi": "Chứng thực chuyên gia",
     "signal": "Chuyên gia, KOL, chứng chỉ ngành"},
    {"code": "SOCIAL_PROOF", "name_en": "Social Proof", "name_vi": "Xã hội chứng thực",
     "signal": "Feedback thật, số lượng KH, case study"},
    {"code": "TANGIBLE_EVIDENCE", "name_en": "Tangible Evidence", "name_vi": "Thực chứng",
     "signal": "Số liệu, giải thưởng, chứng nhận"},
    {"code": "STORYTELLING_HUMAN_TOUCH", "name_en": "Storytelling Human Touch", "name_vi": "Nhân hoá",
     "signal": "Câu chuyện founder / nhân viên / KH thật"},
]

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
GROUP_MAX_TOKENS_6_CRITERIA = 4000
GROUP_MAX_TOKENS_STICK = 2000

CLASSIFICATION_GROUPS = [
    ("prefer", "NHÓM PREFER (đánh giá TỔNG THỂ toàn bài, không phải câu-by-câu)", PREFER_INSIGHTS, "primary | secondary | off", GROUP_MAX_TOKENS_6_CRITERIA),
    ("action", "NHÓM ACTION — S-FACES (đánh giá từng câu/đoạn cụ thể)", ACTION_CRITERIA, "pass | miss", GROUP_MAX_TOKENS_6_CRITERIA),
    ("acknowledge", "NHÓM ACKNOWLEDGE — BRANDS (đánh giá từng câu/đoạn cụ thể)", ACKNOWLEDGE_CRITERIA, "pass | miss", GROUP_MAX_TOKENS_6_CRITERIA),
    ("stick", "NHÓM STICK text-detectable (chỉ 2 tiêu chí này detect được từ text thuần)", STICK_TEXT_DETECTABLE_CRITERIA, "pass | miss", GROUP_MAX_TOKENS_STICK),
    ("trust", "NHÓM TRUST — TRUSTS (đánh giá từng câu/đoạn cụ thể)", TRUST_CRITERIA, "pass | miss", GROUP_MAX_TOKENS_6_CRITERIA),
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
        lines = [f"{group_label} (trạng thái hợp lệ: {status_options}):"]
        for it in items:
            lines.append(f"- {it['code']} | {it['name_en']} ({it['name_vi']}) — dấu hiệu: {it['signal']}")
        group_desc = "\n".join(lines)

        if group_key == "prefer":
            rule_text = (
                'Quy tắc: primary = có ít nhất 3 câu bằng chứng VÀ là chủ đề xuyên suốt bài; '
                'secondary = có 1-2 câu bằng chứng VÀ không phải chủ đề chính; off = không có câu bằng chứng nào. '
                'Với primary/secondary, liệt kê TẤT CẢ câu bằng chứng tìm được (mảng evidence_sentences), không giới hạn số lượng. '
                'Với off, để "description" và "evidence_sentences" rỗng.'
            )
            shape = (
                '{ "prefer": [ {"code": "C", "status": "primary|secondary|off", '
                '"description": "1 câu mô tả ý nghĩa insight này với content này", "evidence_sentences": ["...", "..."]}, '
                '... đủ 6 code C,R,A,V,E,S ] }'
            )
        else:
            rule_text = (
                'Với "pass": field "evidence" = quote nguyên văn câu trong content. '
                'Với "miss": field "evidence" = gợi ý CỤ THỂ nên thêm gì (không viết chung chung như "cần cải thiện").'
            )
            codes = ",".join(it["code"] for it in items)
            shape = (
                f'{{ "{group_key}": [ {{"code": "...", "status": "pass|miss", "evidence": "..."}}, '
                f'... đủ {len(items)} code {codes} ] }}'
            )

        return f"""Phân tích kịch bản content dưới đây theo khung PAAST — CHỈ nhóm tiêu chí sau đây. Với MỖI tiêu chí,
xác định trạng thái và trích dẫn NGUYÊN VĂN câu trong content làm bằng chứng (không diễn giải lại,
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
    ) -> List[Dict[str, Any]]:
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
            return parsed.get(group_key, [])

        raise RuntimeError(f"nhóm {group_key}: {last_err or 'hết ngân sách thời gian'}")

    def _classify(self, content: str, timeout_s: int) -> Dict[str, Any]:
        """Chạy 5 lệnh gọi LLM (1 lệnh/lớp) song song thay vì 1 lệnh gộp cả 30+6 tiêu chí —
        thời gian chờ = lệnh chậm nhất trong 5, không phải tổng cộng dồn (xem CLASSIFICATION_GROUPS).

        `timeout_s` là ngân sách cho phần gọi DeepSeek, đã trừ biên an toàn so với timeout mà BE
        cho phép. 5 nhóm chạy song song và dùng CHUNG một deadline nên tổng thời gian vẫn nằm
        trong ngân sách đó dù mỗi nhóm có thể thử lại vài lượt.

        KHÔNG degrade partial: 1 nhóm hỏng sau khi đã tự thử lại ⇒ hỏng cả lượt (quyết định
        nghiệp vụ — thà báo lỗi còn hơn trả điểm thiếu lớp, vì điểm thiếu lớp luôn thấp hơn
        thực tế mà người dùng không có cách nào biết).
        """
        deadline = time.monotonic() + timeout_s
        result: Dict[str, Any] = {}
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=len(CLASSIFICATION_GROUPS)) as executor:
            future_to_key = {
                executor.submit(
                    self._classify_group, content, key, label, items, status_options, max_tokens, deadline
                ): key
                for key, label, items, status_options, max_tokens in CLASSIFICATION_GROUPS
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    result[key] = future.result()
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

    def _normalize_classification(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Đảm bảo đủ đúng 30+6 tiêu chí, kể cả khi LLM trả thiếu — thiếu thì coi là miss/off."""
        prefer_by_code = self._index_by_code(raw.get("prefer"))
        prefer = []
        for defn in PREFER_INSIGHTS:
            item = prefer_by_code.get(defn["code"], {})
            status = item.get("status") if item.get("status") in ("primary", "secondary", "off") else "off"
            prefer.append({
                "code": defn["code"],
                "name_en": defn["name_en"],
                "name_vi": defn["name_vi"],
                "status": status,
                "description": item.get("description", "") if status != "off" else "",
                "evidence_sentences": item.get("evidence_sentences", []) if status != "off" else [],
            })

        def normalize_criteria_group(group_key: str, definitions: List[Dict[str, str]]) -> List[Dict[str, Any]]:
            by_code = self._index_by_code(raw.get(group_key))
            out = []
            for defn in definitions:
                item = by_code.get(defn["code"], {})
                status = item.get("status") if item.get("status") in ("pass", "miss") else "miss"
                evidence = item.get("evidence", "")
                if status == "miss" and not evidence:
                    evidence = f"Gợi ý — thêm yếu tố thể hiện \"{defn['name_vi']}\": {defn['signal']}"
                out.append({
                    "code": defn["code"],
                    "name_en": defn["name_en"],
                    "name_vi": defn["name_vi"],
                    "status": status,
                    "evidence": evidence,
                })
            return out

        action = normalize_criteria_group("action", ACTION_CRITERIA)
        acknowledge = normalize_criteria_group("acknowledge", ACKNOWLEDGE_CRITERIA)
        trust = normalize_criteria_group("trust", TRUST_CRITERIA)

        stick_text = normalize_criteria_group("stick", STICK_TEXT_DETECTABLE_CRITERIA)
        stick_na = [
            {
                "code": defn["code"],
                "name_en": defn["name_en"],
                "name_vi": defn["name_vi"],
                "status": "na",
                "evidence": "Cần production — không detect được từ text thuần.",
            }
            for defn in STICK_PRODUCTION_ONLY_CRITERIA
        ]
        stick = stick_text + stick_na

        return {
            "prefer": prefer,
            "action": action,
            "acknowledge": acknowledge,
            "stick": stick,
            "trust": trust,
        }

    # ------------------------------------------------------------------
    # Score computation — thuần Python, không qua LLM (business doc §2, §4, §7)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_scores(classification: Dict[str, Any]) -> Dict[str, Any]:
        """
        Tính điểm 0-100 theo business doc §2/§4/§7. Tổng điểm được cộng từ giá trị
        CHƯA làm tròn của từng lớp rồi mới làm tròn 1 lần duy nhất ở cuối — làm tròn
        từng lớp trước rồi cộng lại sẽ lệch so với ví dụ minh hoạ ở business doc §10
        (vd Case D: 1/6 + 1/6 pass ra tổng thật 6.667, không phải 3.3+3.3=6.6).
        Điểm hiển thị từng lớp (`score`) vẫn làm tròn 1 chữ số thập phân riêng để trình bày.
        """
        prefer_items = classification["prefer"]
        primary_count = sum(1 for it in prefer_items if it["status"] == "primary")
        secondary_count = sum(1 for it in prefer_items if it["status"] == "secondary")
        prefer_raw = float(min(20, primary_count * 10 + secondary_count * 2))

        def pass_count_of(items: List[Dict[str, Any]]) -> int:
            return sum(1 for it in items if it["status"] == "pass")

        action_pass = pass_count_of(classification["action"])
        acknowledge_pass = pass_count_of(classification["acknowledge"])
        trust_pass = pass_count_of(classification["trust"])

        action_raw = (action_pass / len(classification["action"])) * 20
        acknowledge_raw = (acknowledge_pass / len(classification["acknowledge"])) * 20
        trust_raw = (trust_pass / len(classification["trust"])) * 20

        stick_text_detectable = [it for it in classification["stick"] if it["status"] != "na"]
        stick_pass = sum(1 for it in stick_text_detectable if it["status"] == "pass")
        stick_raw = (stick_pass / len(stick_text_detectable)) * 20 if stick_text_detectable else 0.0

        total = round(prefer_raw + action_raw + acknowledge_raw + stick_raw + trust_raw)

        return {
            "prefer": {"score": round(prefer_raw, 1), "primary_count": primary_count, "secondary_count": secondary_count},
            "action": {"score": round(action_raw, 1), "pass_count": action_pass},
            "acknowledge": {"score": round(acknowledge_raw, 1), "pass_count": acknowledge_pass},
            "stick": {"score": round(stick_raw, 1), "pass_count": stick_pass, "text_detectable_count": len(stick_text_detectable)},
            "trust": {"score": round(trust_raw, 1), "pass_count": trust_pass},
            "total_score": total,
        }

    # ------------------------------------------------------------------
    # Verdict — đạt/chưa đạt chuẩn PAAST (business doc §1.3, §5.2/§5.3)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_verdict(scores: Dict[str, Any]) -> Dict[str, Any]:
        """
        Đạt chuẩn PAAST khi CẢ 5 lớp đều có ít nhất 1 tiêu chí đạt — KHÔNG dùng ngưỡng điểm
        tổng, vì điểm cao vẫn có thể do dồn hết vào vài lớp trong khi bỏ trắng hẳn 1 lớp khác
        (business doc §1.3/§5.2). Riêng Prefer đòi hỏi ≥1 insight `primary` — `secondary`
        không tính (business doc §5.3, nguyên tắc Chân-Thiện-Mỹ #3).
        """
        passed_layers: List[str] = []
        missing_layers: List[str] = []

        if scores["prefer"]["primary_count"] >= 1:
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
                "prefer": {**scores["prefer"], "insights": classification["prefer"]},
                "action": {**scores["action"], "criteria": classification["action"]},
                "acknowledge": {**scores["acknowledge"], "criteria": classification["acknowledge"]},
                "stick": {**scores["stick"], "criteria": classification["stick"]},
                "trust": {**scores["trust"], "criteria": classification["trust"]},
            },
            "total_score": scores["total_score"],
            "verdict": verdict,
            "cta_warning": cta,
            # BE ghi thẳng vào cột model_used. Trả từ đây vì AI mới là bên quyết model —
            # BE tự đoán thì bảng lịch sử ghi sai như trước.
            "model_used": DEEPSEEK_DEFAULT_MODEL,
        }

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
        raw = self._gen._call_deepseek_raw(
            prompt=prompt,
            system_msg=system_msg,
            temperature=0.4,
            # KHÔNG tắt suy luận ở đây (khác phần phân loại): viết lại content là tác vụ sáng
            # tạo, suy luận có giá trị thật. Nhưng phải cấp đủ headroom — 4096 là quá thấp cho
            # model reasoning: thực đo 3 lượt thì reasoning_tokens ăn trọn 4096 ở 2 lượt, trả
            # content rỗng, chỉ 1/3 lượt thành công. Dùng đúng mức 16000 mà bước viết kịch bản
            # của content-transform đang dùng (writeContentWithRetry) vì cùng loại tác vụ.
            max_tokens=16000,
            timeout=write_budget,
            log_prefix="PAAST upgrade (DeepSeek)",
        )
        if not raw:
            raise RuntimeError("Không gọi được LLM để nâng cấp content PAAST (DeepSeek không phản hồi).")

        parsed = self._gen._extract_json_dict(raw)
        if not parsed or not parsed.get("upgraded_content"):
            raise RuntimeError("Không parse được JSON kết quả nâng cấp PAAST từ phản hồi LLM.")

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
        doi = {"name_en": "nameEn", "name_vi": "nameVi", "evidence_sentences": "evidenceSentences"}
        return [{doi.get(k, k): v for k, v in it.items()} for it in items]

    @staticmethod
    def _dem_dat(items: List[Dict[str, Any]]) -> int:
        """Số tiêu chí ĐẠT. 'na' (không áp dụng cho văn bản) không tính là đạt lẫn không đạt."""
        return sum(1 for it in items if it.get("status") == "pass")

    def analyze_v2(self, content: str) -> Dict[str, Any]:
        classification = self._normalize_classification(self._classify(content))

        prefer = classification["prefer"]
        chu_dao = sum(1 for i in prefer if i.get("status") == "primary")
        phu_tro = sum(1 for i in prefer if i.get("status") == "secondary")

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
