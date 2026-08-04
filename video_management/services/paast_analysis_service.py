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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from video_management.services.content_generation_service import ContentGenerationService

logger = logging.getLogger(__name__)


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

# 5 nhóm phân loại — mỗi nhóm là 1 lệnh gọi LLM riêng, chạy song song (xem _classify).
# Tách nhỏ thay vì 1 prompt gộp cả 30 tiêu chí giúp giảm độ trễ (thời gian chờ = lệnh
# chậm nhất trong 5 lệnh chạy song song, không phải tổng cả 5) và giảm rủi ro response
# bị cắt cụt do quá dài (mỗi lệnh giờ chỉ cần trả tối đa 6 tiêu chí thay vì cả 30+6).
CLASSIFICATION_GROUPS = [
    ("prefer", "NHÓM PREFER (đánh giá TỔNG THỂ toàn bài, không phải câu-by-câu)", PREFER_INSIGHTS, "primary | secondary | off"),
    ("action", "NHÓM ACTION — S-FACES (đánh giá từng câu/đoạn cụ thể)", ACTION_CRITERIA, "pass | miss"),
    ("acknowledge", "NHÓM ACKNOWLEDGE — BRANDS (đánh giá từng câu/đoạn cụ thể)", ACKNOWLEDGE_CRITERIA, "pass | miss"),
    ("stick", "NHÓM STICK text-detectable (chỉ 2 tiêu chí này detect được từ text thuần)", STICK_TEXT_DETECTABLE_CRITERIA, "pass | miss"),
    ("trust", "NHÓM TRUST — TRUSTS (đánh giá từng câu/đoạn cụ thể)", TRUST_CRITERIA, "pass | miss"),
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

    def _classify_group(self, content: str, group_key: str, group_label: str, items: List[Dict[str, str]], status_options: str) -> List[Dict[str, Any]]:
        system_msg = (
            "Bạn là chuyên gia phân tích content marketing theo khung PAAST. "
            "Chỉ trả JSON hợp lệ theo đúng shape được yêu cầu, không thêm markdown fence, không thêm lời giải thích."
        )
        raw = self._gen._call_deepseek_raw(
            prompt=self._build_group_prompt(content, group_key, group_label, items, status_options),
            system_msg=system_msg,
            temperature=0.2,
            max_tokens=2048,
            timeout=45,
            log_prefix=f"PAAST analyze/{group_key} (DeepSeek)",
        )
        if not raw:
            raise RuntimeError(f"nhóm {group_key}: DeepSeek không phản hồi")

        parsed = self._gen._extract_json_dict(raw)
        if not parsed:
            raise RuntimeError(f"nhóm {group_key}: không parse được JSON từ phản hồi LLM")
        return parsed.get(group_key, [])

    def _classify(self, content: str) -> Dict[str, Any]:
        """Chạy 5 lệnh gọi LLM (1 lệnh/lớp) song song thay vì 1 lệnh gộp cả 30+6 tiêu chí —
        thời gian chờ = lệnh chậm nhất trong 5, không phải tổng cộng dồn (xem CLASSIFICATION_GROUPS)."""
        result: Dict[str, Any] = {}
        errors: List[str] = []
        with ThreadPoolExecutor(max_workers=len(CLASSIFICATION_GROUPS)) as executor:
            future_to_key = {
                executor.submit(self._classify_group, content, key, label, items, status_options): key
                for key, label, items, status_options in CLASSIFICATION_GROUPS
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

    def analyze(self, content: str) -> Dict[str, Any]:
        raw_classification = self._classify(content)
        classification = self._normalize_classification(raw_classification)
        scores = self.compute_scores(classification)
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
            "cta_warning": cta,
        }

    def upgrade(self, original_content: str, missing_elements: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        missing_elements: [{"layer": "acknowledge", "criterion": "STORY", "suggestion": "..."}]
        Caller (BE) chịu trách nhiệm loại bỏ các tiêu chí `na` của Stick trước khi gọi hàm này
        (business doc §11.2 — không thể "nâng cấp" phần cần production bằng cách sửa text).
        """
        if not missing_elements:
            new_analysis = self.analyze(original_content)
            return {
                "original": original_content,
                "upgraded": original_content,
                "changes_added": [],
                "new_analysis": new_analysis,
            }

        missing_list_text = "\n".join(
            f"- [{m['layer']}] {m['criterion']}: {m.get('suggestion', '')}" for m in missing_elements
        )
        prompt = f"""Nâng cấp content sau bằng cách THÊM các element còn thiếu theo hướng dẫn, giữ nguyên tone và
văn phong tác giả gốc, không phá vỡ mạch chuyện, chỉ bổ sung — không viết lại toàn bộ.

Content gốc:
\"\"\"
{original_content}
\"\"\"

Cần thêm:
{missing_list_text}

Trả về DUY NHẤT một JSON object, không thêm text ngoài JSON:
{{
  "upgraded_content": "toàn bộ content mới, các đoạn/câu vừa thêm được bọc trong <add>...</add>",
  "changes_added": [ {{"layer": "acknowledge", "criterion": "STORY", "text": "tóm tắt ngắn gọn phần vừa thêm"}}, ... ]
}}"""
        system_msg = (
            "Bạn là copywriter nâng cấp content theo khung PAAST. Chỉ bổ sung phần thiếu, không viết lại toàn bộ. "
            "Chỉ trả JSON hợp lệ, không markdown fence, không giải thích ngoài JSON."
        )
        raw = self._gen._call_deepseek_raw(
            prompt=prompt,
            system_msg=system_msg,
            temperature=0.4,
            max_tokens=4096,
            timeout=60,
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
        new_analysis = self.analyze(stripped_for_analysis)

        return {
            "original": original_content,
            "upgraded": upgraded_content,
            "changes_added": changes_added,
            "new_analysis": new_analysis,
        }
