"""
Trích text thuần từ file nguồn (Google Docs / Google Drive / PDF / Word / text).

Tách từ task_script_service.py để dùng lại cho nhiều luồng: sinh kịch bản task,
chấm điểm PAAST khi nội dung nằm trong file thay vì được dán thẳng, v.v.

Chỉ fetch tới domain của Google (docs.google.com / drive.google.com) — id file/doc
được bóc bằng regex rồi tự dựng URL export/download, KHÔNG request thẳng URL người
dùng đưa vào, nên không mở đường SSRF.
"""
import logging
import re
import zipfile
from io import BytesIO
from typing import Optional
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

# MIME types đọc được thành text (model chỉ nhận text)
TEXT_MIMES = {
    "text/plain",
    "text/html",
    "text/csv",
    "text/markdown",
}

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Namespace của WordprocessingML — phần thân file .docx nằm trong word/document.xml
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# File lớn hơn mức này thì bỏ qua (kịch bản content không bao giờ tới cỡ này)
MAX_FILE_BYTES = 10 * 1024 * 1024


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

    # ZIP container (50 4B 03 04) — có thể là .docx (Word). Tin header nếu nó nói rõ là docx,
    # còn lại trả "application/zip" rồi để _extract_docx_text tự xác minh word/document.xml.
    if head[:4] == b"PK\x03\x04":
        return DOCX_MIME if header_mime == DOCX_MIME else "application/zip"

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


def _extract_docx_text(content: bytes) -> Optional[str]:
    """Đọc text từ file .docx (Word) bằng thư viện chuẩn — .docx là 1 file ZIP chứa
    word/document.xml, lấy chữ trong các thẻ <w:t>, mỗi <w:p> là 1 đoạn xuống dòng.
    Trả None nếu ZIP không phải .docx (vd .xlsx/.pptx/zip thường)."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            with zf.open("word/document.xml") as doc:
                tree = ElementTree.parse(doc)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as err:
        logger.warning(f"Không đọc được .docx: {err}")
        return None

    lines = []
    for para in tree.iter(f"{_W_NS}p"):
        parts = []
        for node in para.iter():
            tag = node.tag
            if tag == f"{_W_NS}t" and node.text:
                parts.append(node.text)
            elif tag == f"{_W_NS}tab":
                parts.append("\t")
            elif tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                parts.append("\n")
        lines.append("".join(parts))

    text = "\n".join(lines).strip()
    return text or None


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:512].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def read_drive_file(file_url: str) -> Optional[str]:
    """Đọc nội dung file nguồn (Google Docs export txt, hoặc Google Drive file: pdf/docx/text)."""
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
            # Doc không mở quyền "bất kỳ ai có link" → Google trả trang đăng nhập HTML kèm 200
            if not text or _looks_like_html(text):
                return None
            return text

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
        if len(content) > MAX_FILE_BYTES:
            return None

        mime_type = _detect_mime_from_bytes(content, content_type)

        if mime_type == "application/pdf":
            return _extract_pdf_text(content)

        if mime_type in (DOCX_MIME, "application/zip"):
            return _extract_docx_text(content)

        if mime_type in TEXT_MIMES:
            text = content.decode("utf-8", errors="ignore").strip()
            return text or None

        # Loại file không đọc được thành text (model chỉ nhận text) → bỏ qua
        return None
    except Exception:  # noqa: BLE001
        return None
