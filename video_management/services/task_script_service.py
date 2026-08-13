"""
Task-Auto Video Script Service.

Adapt một content đã test "win" cho sản phẩm mới (task-auto flow của BE).
Toàn bộ phần đọc file nguồn (Google Docs/Drive/PDF) + build prompt + gọi DeepSeek
được chuyển từ BE (NestJS) sang đây để BE không phải xử lý gọi model AI nữa —
BE chỉ gọi endpoint này để lấy kết quả rồi tự lưu (cache) vào DB của BE.
"""
import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# URL/model đọc từ settings LÚC GỌI (không phải lúc import) — theo mẫu _tikhub_base():
# đổi .env là đổi được, và override_settings trong test cũng có tác dụng.
def _deepseek_base() -> str:
    return getattr(settings, 'DEEPSEEK_API_BASE_URL', 'https://api.deepseek.com')


def _deepseek_model() -> str:
    # DeepSeek đã bỏ tên "deepseek-chat" — chỉ còn deepseek-v4-pro / deepseek-v4-flash.
    return getattr(settings, 'DEEPSEEK_MODEL', 'deepseek-v4-flash')

# MIME types đọc được thành text để đưa vào prompt (DeepSeek chỉ nhận text)
TEXT_MIMES = {
    "text/plain",
    "text/html",
    "text/csv",
    "text/markdown",
}

# Mô tả định hướng & chiến lược theo từng tuyến nội dung
CONTENT_LINE_GUIDE: Dict[str, Dict[str, str]] = {
    "A1": {
        "goal": "Kéo view tối đa & khắc sâu nhận diện thương hiệu",
        "strategy": (
            "Ưu tiên yếu tố giải trí, xu hướng (trend), cảm xúc mạnh hoặc yếu tố bất ngờ để người xem dừng scroll.\n"
            "Sản phẩm xuất hiện tự nhiên, không ép buộc — thương hiệu được lặp lại nhẹ nhàng 1–2 lần.\n"
            "Nhịp video nhanh, hình ảnh động, âm thanh bắt tai. Không cần bán hàng trực tiếp — mục tiêu là lượt xem & nhớ brand."
        ),
        "hookStyle": "Gây sốc nhẹ, kể chuyện nhanh, câu hỏi khiêu khích, hoặc dùng trend đang viral — phải khiến người xem tò mò ngay lập tức.",
        "ctaStyle": "Follow để xem thêm / Lưu lại video này / Tag người bạn cần xem cái này",
    },
    "A2": {
        "goal": "Khẳng định vị thế thương hiệu và xây dựng hình ảnh chuyên gia trong ngành",
        "strategy": (
            "Nội dung thể hiện sự am hiểu sâu về sản phẩm, ngành hàng, hoặc nhu cầu khách hàng.\n"
            "Giọng điệu tự tin, chuyên nghiệp nhưng gần gũi. Có thể chia sẻ góc nhìn chuyên gia, behind-the-scenes, hoặc quá trình tạo ra sản phẩm.\n"
            "Tránh quảng cáo lộ liễu — tập trung vào giá trị & câu chuyện thương hiệu."
        ),
        "hookStyle": 'Câu mở đầu định vị chuyên gia: "Sau X năm trong ngành...", "Sai lầm mà 90% người dùng mắc phải...", "Bí quyết mà ít ai biết..."',
        "ctaStyle": "Follow để học thêm / Xem series tiếp theo / Bình luận câu hỏi của bạn",
    },
    "A3": {
        "goal": "Xây dựng niềm tin vững chắc với khách hàng tiềm năng",
        "strategy": (
            "Dùng bằng chứng xã hội (social proof): review thật, trước/sau, số liệu cụ thể, chứng nhận, câu chuyện khách hàng thực tế.\n"
            "Mỗi claim đều có dẫn chứng — tránh lời hứa suông. Giọng chân thực, không \"quá đà\".\n"
            "Có thể dùng format: vấn đề → giải pháp → kết quả thực tế → cam kết thương hiệu."
        ),
        "hookStyle": 'Nêu một nỗi lo thật của khách hàng hoặc một kết quả ấn tượng có thật: "Khách hàng của chúng tôi đã...", "Tôi đã thử sản phẩm này trong X tuần và..."',
        "ctaStyle": "Đọc đánh giá thật tại link bio / Nhắn tin để nhận tư vấn miễn phí / Xem thêm phản hồi khách hàng",
    },
    "A4": {
        "goal": "Chuyển đổi trực tiếp — thúc đẩy người xem mua hàng ngay",
        "strategy": (
            "Nội dung tập trung 100% vào lý do MUA NGAY: giá tốt, ưu đãi có hạn, lợi ích rõ ràng, so sánh giá trị.\n"
            "Hook ngay lập tức nêu lợi ích/ưu đãi. Body nhấn mạnh pain point và giải pháp. CTA mạnh với yếu tố khan hiếm/khẩn cấp.\n"
            "Mỗi cảnh phục vụ mục đích bán hàng: demo sản phẩm, giá, ưu đãi, cam kết đổi trả."
        ),
        "hookStyle": 'Nêu ưu đãi hoặc lợi ích cụ thể ngay: "Chỉ hôm nay giảm X%...", "Tại sao X triệu người chọn sản phẩm này?", "Bí quyết [kết quả] chỉ với [giá]..."',
        "ctaStyle": "Mua ngay hôm nay — link trong bio / Ưu đãi kết thúc lúc [giờ] / Inbox để đặt hàng ngay",
    },
    "A5": {
        "goal": "Cung cấp giá trị kiến thức thực tiễn, mẹo hay — tạo engagement cao và giữ chân follower",
        "strategy": (
            "Format \"X tips/mẹo/bước\" hoặc \"Bạn có biết...\" rất hiệu quả cho tuyến này.\n"
            "Nội dung phải thật sự hữu ích, actionable — người xem áp dụng được ngay.\n"
            "Sản phẩm được lồng ghép tự nhiên như \"công cụ/giải pháp\" giúp thực hiện mẹo đó, không phải trọng tâm bán hàng.\n"
            "Kết thúc bằng một takeaway đáng nhớ để người xem lưu/chia sẻ."
        ),
        "hookStyle": 'Câu hỏi khơi gợi nhu cầu học hỏi: "Bạn có đang làm sai điều này?", "X mẹo [chủ đề] mà ai cũng nên biết", "[Con số] sai lầm khiến bạn [hậu quả]..."',
        "ctaStyle": "Lưu video để dùng sau / Follow để nhận thêm mẹo mỗi ngày / Bình luận bạn đang dùng cách nào",
    },
}


def detect_content_line_type(content_line: Optional[str]) -> Optional[str]:
    if not content_line:
        return None
    upper = content_line.upper().strip()
    for line_type in ("A1", "A2", "A3", "A4", "A5"):
        if upper.startswith(line_type):
            return line_type
    return None


def _extract_doc_id(url: str) -> Optional[str]:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _extract_drive_id(url: str) -> Optional[str]:
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _detect_mime_from_bytes(content: bytes, header_content_type: str) -> str:
    head = content[:8]
    # PDF: bắt đầu bằng %PDF (25 50 44 46)
    if head[:4] == b"%PDF":
        return "application/pdf"

    header_mime = (header_content_type or "").split(";")[0].strip()
    if header_mime and header_mime != "application/octet-stream":
        return header_mime

    return "application/octet-stream"


def _extract_pdf_text(content: bytes) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf chưa được cài đặt, không thể trích xuất text từ PDF")
        return None

    try:
        reader = PdfReader(BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        text = text.strip()
        return text or None
    except Exception as err:  # noqa: BLE001
        logger.warning(f"Không trích xuất được text từ PDF: {err}")
        return None


def _read_drive_file(file_url: str) -> Optional[str]:
    """Đọc nội dung file nguồn (Google Docs export txt, hoặc Google Drive file: pdf/text)."""
    try:
        if "docs.google.com/document" in file_url:
            doc_id = _extract_doc_id(file_url)
            if not doc_id:
                return None
            res = requests.get(
                f"https://docs.google.com/document/d/{doc_id}/export?format=txt",
                timeout=12,
            )
            if not res.ok:
                return None
            text = res.text.strip()
            return text or None

        file_id = _extract_drive_id(file_url)
        if not file_id:
            return None
        res = requests.get(
            f"https://drive.google.com/uc?export=download&id={file_id}",
            timeout=20,
        )
        if not res.ok:
            return None

        content_type = res.headers.get("content-type", "")
        if "text/html" in content_type:
            return None

        content = res.content
        if len(content) > 10 * 1024 * 1024:
            return None

        mime_type = _detect_mime_from_bytes(content, content_type)

        if mime_type == "application/pdf":
            return _extract_pdf_text(content)

        if mime_type in TEXT_MIMES:
            text = content.decode("utf-8", errors="ignore").strip()
            return text or None

        # Loại file không đọc được thành text (DeepSeek chỉ nhận text) → bỏ qua
        return None
    except Exception:  # noqa: BLE001
        return None


def _build_prompt(p: Dict[str, Any]) -> str:
    lines = []
    line_type = detect_content_line_type(p.get("contentLine"))
    guide = CONTENT_LINE_GUIDE.get(line_type) if line_type else None
    file_text = p.get("fileText")
    script_text = p.get("scriptText")
    has_file_text = bool(file_text)
    has_script = bool(script_text)
    has_source = has_file_text or has_script
    target_market = p.get("contentMarket") or p.get("productMarket") or None

    lines.append("Bạn là chuyên gia adapt content mạng xã hội (TikTok/Reels) cho thị trường Việt Nam.")
    lines.append(
        'QUAN TRỌNG: Nhiệm vụ của bạn KHÔNG PHẢI là viết kịch bản quay video (không chia cảnh, không mô tả góc máy/hành động quay). '
        'Content bên dưới ĐÃ ĐƯỢC TEST THỰC TẾ VÀ CHỨNG MINH HIỆU QUẢ (content đã "win") cho một sản phẩm khác.'
    )
    lines.append(
        "Việc của bạn là ÁP DỤNG LẠI đúng phần lời/nội dung (copy) của content đã win đó cho sản phẩm mới được cung cấp — "
        "bám sát tối đa câu chữ, cấu trúc, nhịp điệu, giọng văn, hook, CTA của content gốc. CHỈ chỉnh sửa những chi tiết liên "
        "quan trực tiếp đến sản phẩm (tên, đặc điểm, chất liệu, giá, ưu đãi, USP...) để khớp với sản phẩm mới. Không sáng tạo "
        "thêm ý tưởng, không đổi cấu trúc, không đổi tông giọng nếu không bắt buộc. Output là MỘT ĐOẠN CONTENT LIỀN MẠCH "
        "(văn bản lời thoại/caption), không phải danh sách cảnh quay."
    )
    lines.append("")

    lines.append("═══ THÔNG TIN NỘI DUNG ═══")
    if p.get("contentTitle"):
        lines.append(f"Tiêu đề / Chủ đề: {p['contentTitle']}")
    if p.get("contentLine"):
        lines.append(f"Tuyến nội dung: {p['contentLine']}")
    if p.get("contentMarket"):
        lines.append(f"Thị trường mục tiêu: {p['contentMarket']}")

    if guide:
        lines.append("")
        lines.append(f"═══ BỐI CẢNH TUYẾN NỘI DUNG {line_type} (tham khảo để hiểu tone, KHÔNG dùng để viết lại từ đầu) ═══")
        lines.append(f"Mục tiêu của content gốc: {guide['goal']}")
        lines.append(f"Phong cách Hook đặc trưng: {guide['hookStyle']}")
        lines.append(f"Phong cách CTA đặc trưng: {guide['ctaStyle']}")

    lines.append("")
    lines.append("═══ CONTENT GỐC ĐÃ TEST WIN (BẮT BUỘC BÁM SÁT — đây là nền tảng, không phải gợi ý) ═══")
    if has_file_text:
        if has_script:
            lines.append(f"Script tóm tắt (tham khảo thêm): {script_text}")
        lines.append("")
        lines.append("TÀI LIỆU ĐÍNH KÈM (content gốc — giữ nguyên cấu trúc, chỉ thay chi tiết sản phẩm):")
        lines.append(file_text)
    elif has_script:
        lines.append("Script gốc đã win (giữ nguyên cấu trúc, chỉ thay chi tiết sản phẩm):")
        lines.append(script_text)
    else:
        lines.append(
            "(Không có content gốc được cung cấp — trường hợp này hãy sáng tác dựa trên tiêu đề, tuyến nội dung và "
            "thông tin sản phẩm bên dưới, vì không có gì để bám theo.)"
        )

    lines.append("")
    lines.append("═══ SẢN PHẨM MỚI CẦN ÁP DỤNG CONTENT VÀO ═══")
    if p.get("productName"):
        lines.append(f"Tên sản phẩm: {p['productName']}")
    if p.get("productSku"):
        lines.append(f"SKU: {p['productSku']}")
    if p.get("productPrice"):
        lines.append(f"Giá bán: {p['productPrice']}")
    if p.get("productMaterial"):
        lines.append(f"Chất liệu: {p['productMaterial']}")
    if p.get("productPriceSegment"):
        lines.append(f"Phân khúc giá: {p['productPriceSegment']}")
    if p.get("productLine"):
        lines.append(f"Dòng sản phẩm: {p['productLine']}")
    if p.get("productMarket"):
        lines.append(f"Thị trường sản phẩm: {p['productMarket']}")

    is_vietnam_market = False
    if target_market:
        parts = [m.strip().upper() for m in target_market.split(",") if m.strip()]
        is_vietnam_market = bool(parts) and all(m in ("VIETNAM", "VN") for m in parts)
    needs_translation = bool(target_market) and not is_vietnam_market

    if target_market:
        lines.append("")
        lines.append("═══ THỊ TRƯỜNG MỤC TIÊU & NGÔN NGỮ ═══")
        lines.append(f"Thị trường: {target_market}")
        lines.append(
            "Hãy điều chỉnh content (ví dụ: đơn vị tiền tệ, cách xưng hô, ví dụ văn hóa, mức độ trang trọng...) cho phù hợp "
            "với người tiêu dùng ở thị trường này, dựa trên nền content gốc đã test win — vẫn giữ nguyên cấu trúc và tinh "
            "thần bản gốc, chỉ bản địa hóa chi tiết cần thiết."
        )
        if needs_translation:
            lines.append(
                "QUAN TRỌNG — KHÔNG ĐƯỢC NHẦM LẪN: việc bản địa hóa ở trên (đơn vị tiền tệ, ví dụ văn hóa...) vẫn phải "
                'viết bằng TIẾNG VIỆT — trường "content" trong JSON output LUÔN LUÔN LÀ TIẾNG VIỆT, bất kể thị trường '
                'mục tiêu là gì. TUYỆT ĐỐI KHÔNG được viết trường "content" bằng ngôn ngữ của thị trường mục tiêu.'
            )
            lines.append(
                'Việc dịch sang ngôn ngữ khác CHỈ diễn ra ở một bước RIÊNG BIỆT: sau khi đã có "content" hoàn chỉnh bằng '
                'tiếng Việt, hãy dịch toàn bộ nội dung đó (và hashtags) sang ngôn ngữ chính thức/phổ biến được dùng trên '
                'mạng xã hội tại thị trường đó, rồi đặt kết quả dịch vào trường "translation" (không phải trường '
                '"content"). Hai trường "content" và "translation.content" PHẢI khác ngôn ngữ nhau — nếu giống hệt nhau '
                "nghĩa là bạn đã làm sai."
            )
        else:
            lines.append(
                'Thị trường mục tiêu là Việt Nam nên content ở trên ĐÃ LÀ tiếng Việt — KHÔNG được sinh thêm bản dịch nào, '
                'trường "translation" trong JSON output phải để là null.'
            )

    lines.append("")
    lines.append("═══ NHIỆM VỤ ═══")
    if has_source:
        lines.append(
            "Viết lại content gốc đã test win ở trên bằng tiếng Việt tự nhiên, ÁP DỤNG cho sản phẩm mới nêu trên. Đây "
            "KHÔNG phải kịch bản quay video — chỉ là phần lời/content hoàn chỉnh."
        )
        lines.append("")
        lines.append("YÊU CẦU BẮT BUỘC:")
        lines.append(
            "1. BÁM SÁT tinh thần, cấu trúc, thứ tự ý, nhịp điệu, phong cách hook và CTA của content gốc — nhưng ĐƯỢC PHÉP "
            "viết chi tiết và đầy đủ hơn bản gốc nếu bản gốc còn ngắn/sơ sài: mở rộng mô tả, thêm cảm xúc, ví dụ/hình ảnh "
            "cụ thể cho từng ý đã có, miễn không đổi thông điệp cốt lõi và không thêm ý hoàn toàn mới ngoài phạm vi bản gốc."
        )
        lines.append(
            "2. CHỈ thay thế phần liên quan đến sản phẩm: tên sản phẩm, đặc điểm/chất liệu, giá/ưu đãi, USP — sao cho khớp "
            "chính xác và tự nhiên với sản phẩm mới."
        )
        lines.append(
            "3. Nếu content gốc nhắc tên sản phẩm cũ, giá cũ, chất liệu cũ, đặc điểm cũ... phải thay hoàn toàn bằng thông "
            "tin sản phẩm mới, không được lẫn thông tin sản phẩm cũ."
        )
        lines.append(
            "4. Không thêm ý tưởng sáng tạo hoàn toàn mới ngoài phạm vi content gốc — không đổi giọng văn, không đổi "
            "thông điệp cốt lõi của content gốc."
        )
        lines.append(
            "5. Nếu content gốc thiếu thông tin mà sản phẩm mới cần nhấn mạnh (giá, chất liệu...), hãy bổ sung chi tiết, cụ "
            "thể (không chỉ liệt kê sơ sài) miễn không phá vỡ mạch của content gốc."
        )
        lines.append(
            "6. KHÔNG chia thành cảnh/scene, KHÔNG mô tả góc máy/hành động quay/text overlay — chỉ trả về đúng phần lời "
            "văn liền mạch, giữ các dấu xuống dòng như content gốc (nếu có) để phân đoạn ý."
        )
        lines.append(
            "7. Hashtags: thay hashtag đặc thù của sản phẩm/nội dung cũ bằng hashtag phù hợp sản phẩm mới; giữ hashtag "
            "ngành hàng/tuyến nội dung nếu vẫn còn đúng."
        )
        lines.append(
            "8. ƯU TIÊN viết chi tiết, giàu hình ảnh và cảm xúc hơn là ngắn gọn sơ sài — mỗi ý trong content gốc nên được "
            "diễn giải đầy đủ bằng ví dụ/mô tả cụ thể thay vì chỉ nêu ý chính trong một câu ngắn."
        )
    else:
        lines.append(
            "Viết content TikTok/Reels bằng tiếng Việt tự nhiên, sinh động. Đây KHÔNG phải kịch bản quay video — chỉ là "
            "phần lời/content hoàn chỉnh, KHÔNG chia cảnh, KHÔNG mô tả góc máy."
        )
        if guide:
            lines.append(f"Toàn bộ nội dung phải phục vụ đúng mục tiêu: {guide['goal']}")
        lines.append("")
        lines.append("YÊU CẦU BẮT BUỘC:")
        lines.append(
            "1. Hook (câu mở đầu): phải khiến người xem DỪNG SCROLL ngay lập tức — dùng yếu tố bất ngờ, câu hỏi, hoặc "
            "tuyên bố mạnh mẽ."
        )
        lines.append("2. Nội dung viết tự nhiên như người thật nói/viết, không nhồi nhét thông tin.")
        if line_type == "A4":
            lines.append("3. Nêu rõ giá / ưu đãi. Tạo cảm giác khan hiếm hoặc khẩn cấp.")
        elif line_type == "A3":
            lines.append("3. Có social proof cụ thể (số liệu, testimonial, chứng nhận).")
        elif line_type == "A5":
            lines.append("3. Cấu trúc rõ ràng: intro → từng tip/bước → kết luận. Đánh số tips nếu có nhiều tips.")
        cta_style = guide["ctaStyle"] if guide else "rõ ràng, tạo cảm giác cấp bách hoặc giá trị"
        lines.append(f"4. CTA cuối: {cta_style}")
        lines.append("5. Hashtags: 5–8 hashtag, mix giữa broad (ngành hàng) và specific (sản phẩm, tuyến nội dung).")
        lines.append(
            "6. Viết ĐẦY ĐỦ chi tiết, giàu hình ảnh cụ thể và cảm xúc — tránh viết ngắn gọn, sơ sài. Mỗi luận điểm cần có "
            "ví dụ/mô tả cụ thể thay vì chỉ nêu ý chính. Độ dài khuyến nghị khoảng 150–300 từ, đủ mở đầu — thân bài "
            "(2-3 luận điểm/tips) — kết thúc rõ ràng."
        )

    lines.append("")
    lines.append("CHỈ trả về JSON hợp lệ (không markdown, không giải thích), đúng format sau:")
    translation_schema = (
        """{
    "language": "tên ngôn ngữ/thị trường đã dịch sang, ví dụ 'Tiếng Anh (Mỹ)', 'Tiếng Thái', 'Bahasa Indonesia'",
    "content": "bản dịch đầy đủ của content sang ngôn ngữ đó, đã bản địa hóa phù hợp văn hóa/thị trường",
    "hashtags": ["#tag1", "#tag2", "..."]
  }"""
        if needs_translation
        else "null"
    )
    lines.append(
        "{\n"
        '  "content": "toàn bộ nội dung/lời văn hoàn chỉnh BẮT BUỘC bằng TIẾNG VIỆT (dù thị trường mục tiêu là nước nào '
        'cũng vậy — KHÔNG bao giờ viết trường này bằng ngôn ngữ khác), viết liền mạch (có thể xuống dòng để phân đoạn '
        'ý), KHÔNG chia cảnh, KHÔNG mô tả góc máy/hành động quay",\n'
        '  "hashtags": ["#tag1", "#tag2", "#tag3", "#tag4", "#tag5"],\n'
        f'  "translation": {translation_schema}\n'
        "}"
    )

    return "\n".join(lines)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass

    md_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except Exception:  # noqa: BLE001
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:  # noqa: BLE001
            pass

    return None


def _call_deepseek(prompt: str) -> Dict[str, Any]:
    deepseek_key = str(getattr(settings, "DEEPSEEK_API_KEY", "")).strip()
    if not deepseek_key:
        raise ValueError("DEEPSEEK_API_KEY chưa được cấu hình")

    res = requests.post(
        f"{_deepseek_base()}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {deepseek_key}",
        },
        json={
            "model": _deepseek_model(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )

    if not res.ok:
        try:
            err_body = res.json()
        except Exception:  # noqa: BLE001
            err_body = {}
        message = (err_body.get("error") or {}).get("message") or f"HTTP {res.status_code}"
        raise ValueError(f"DeepSeek API lỗi: {message}")

    data = res.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    script = _extract_json(raw)
    if not script:
        raise ValueError("Không parse được kết quả từ AI")
    return script


def generate_video_script(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    params (camelCase, giống GenerateScriptParams bên BE):
      fileUrl, scriptText, contentTitle, contentLine, contentMarket,
      productName, productSku, productPrice, productMaterial,
      productPriceSegment, productLine, productMarket
    """
    file_url = params.get("fileUrl")
    file_text = _read_drive_file(file_url) if file_url else None

    prompt = _build_prompt({**params, "fileText": file_text})
    return _call_deepseek(prompt)


def _build_translate_prompt(
    content: str,
    hashtags: list,
    language: Optional[str] = None,
    market: Optional[str] = None,
) -> str:
    hashtags_text = ", ".join(hashtags) if hashtags else "(không có)"

    if language:
        intro = f"Bạn là biên dịch viên chuyên nội dung mạng xã hội (TikTok/Reels), dịch sang ngôn ngữ: {language}."
        language_field = f'"{language}"'
    else:
        intro = (
            f"Bạn là biên dịch viên chuyên nội dung mạng xã hội (TikTok/Reels), chuẩn bị dịch nội dung cho thị "
            f"trường: {market}. Trước tiên, hãy tự xác định ngôn ngữ chính thức/phổ biến nhất được dùng trên mạng "
            f"xã hội tại thị trường này, rồi dịch sang đúng ngôn ngữ đó."
        )
        language_field = "tên ngôn ngữ bạn đã xác định cho thị trường trên, ví dụ 'Tiếng Anh (Mỹ)', 'Bahasa Indonesia'"

    lines = [
        intro,
        "Dịch TOÀN BỘ content bên dưới sang ngôn ngữ đó, bản địa hóa phù hợp văn hóa/thị trường tương ứng "
        "(đơn vị tiền tệ, cách xưng hô, ví dụ văn hóa, mức độ trang trọng...). "
        "GIỮ NGUYÊN cấu trúc, nhịp điệu, giọng văn, hook, CTA của bản gốc — chỉ dịch, không sáng tác thêm ý mới, "
        "không cắt bớt nội dung.",
        "",
        "═══ CONTENT GỐC (cần dịch) ═══",
        content,
        "",
        f"═══ HASHTAGS GỐC ═══\n{hashtags_text}",
        "",
        "CHỈ trả về JSON hợp lệ (không markdown, không giải thích), đúng format sau:",
        "{\n"
        f'  "language": {language_field},\n'
        '  "content": "toàn bộ nội dung đã dịch sang ngôn ngữ trên, giữ nguyên định dạng xuống dòng của bản gốc",\n'
        '  "hashtags": ["#tag1", "#tag2", "..."]\n'
        "}",
    ]
    return "\n".join(lines)


def translate_video_script(
    content: str,
    hashtags: list,
    language: Optional[str] = None,
    market: Optional[str] = None,
) -> Dict[str, Any]:
    """Dịch lại content/hashtags hiện có (vd sau khi user sửa tay), không đọc lại file nguồn,
    không sinh script mới — chỉ dịch. Cần truyền `language` (đã biết trước, vd lần dịch lại) hoặc
    `market` (lần dịch đầu tiên, chưa biết ngôn ngữ — để AI tự xác định)."""
    if not language and not market:
        raise ValueError("Cần truyền language hoặc market để xác định ngôn ngữ cần dịch")
    prompt = _build_translate_prompt(content, hashtags, language=language, market=market)
    return _call_deepseek(prompt)
