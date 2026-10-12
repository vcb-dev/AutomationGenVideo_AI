"""
Nhận dạng giọng nói chạy TẠI MÁY bằng faster-whisper — không gọi API, không tốn tiền.

── Vì sao cần ──────────────────────────────────────────────────────────────────
Whisper của OpenAI đã hết credit (`credit_balance_exhausted`), Gemini hết hạn mức gói miễn
phí. Trong khi đó ~2/3 video nội bộ không có phụ đề tự sinh của Facebook nên bắt buộc phải
tự bóc lời thoại. Chạy tại máy thì không phụ thuộc hoá đơn của ai.

── Ngôn ngữ ────────────────────────────────────────────────────────────────────
Chỉ ép `language="vi"` VÀ nhét bộ từ điển khi phía gọi chắc video nói tiếng Việt.

Kênh nội bộ có cả trang tiếng Thái, Nhật, Trung. Nhét bộ từ điển tiếng Việt vào một video
tiếng Thái thì bản chép ra rác lẫn lộn nhiều thứ tiếng — đo được trên video thật:
"防ทธิภาพ สอนวิที Crit display แยกเงินแถยกับเงินปลอมที่บ้够คับ ... beitet เล่ aura véhic".
Không có từ điển thì Whisper tự nhận dạng và chép sạch hơn nhiều.

── Vì sao phải chạy trong TIẾN TRÌNH RIÊNG ─────────────────────────────────────
ctranslate2 (lõi của faster-whisper) tự `import torch`, mà torch và ctranslate2 mỗi bên
mang một bản libiomp5 riêng. Nạp chung một tiến trình là chết ngay:

    OMP: Error #15: Initializing libiomp5.dylib, but found libiomp5.dylib already initialized

Đặt KMP_DUPLICATE_LIB_OK=TRUE thì chạy được nhưng chính Intel cảnh báo có thể cho kết quả
sai âm thầm. Nên tách hẳn ra tiến trình con: cờ đó chỉ có hiệu lực trong tiến trình con,
tiến trình Django chính không bị đụng tới, và model được giải phóng ngay sau khi xong.

── Đo trên máy này (Intel i9-9980HK) ───────────────────────────────────────────
Model `small`, video 33,8 giây: nạp model 1,9s + nhận dạng 17,3s. Model `medium` cho chất
lượng cao hơn nhưng phải tải 1,5 GB và chậm gấp mấy lần — không đáng cho việc chấm PAAST
vốn chỉ cần hiểu ý, không cần từng chữ chính xác.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

# Bộ từ giữ nguyên — thiếu nó thì "Huy Ca" bị nghe thành "Vika", đo được trên video thật.
GLOSSARY = (
    "Huy Ca, HuyK, Viễn Chí Bảo, bạc 925, bạc S925, moissanite, kim cương, CZ, "
    "nhẫn, dây chuyền, lắc tay, bông tai, vàng 18K, vàng 24K, thợ kim hoàn, trang sức."
)

MODEL_SIZE = os.environ.get('LOCAL_WHISPER_MODEL', 'small')

# Script chạy trong tiến trình con. Giữ nguyên một chuỗi ở đây thay vì tạo file riêng để
# module này tự chứa, khỏi lệch phiên bản giữa hai file.
_RUNNER = r'''
import sys, json
from faster_whisper import WhisperModel
duong_dan, kich_co, tu_dien = sys.argv[1], sys.argv[2], sys.argv[3]
m = WhisperModel(kich_co, device="cpu", compute_type="int8")
# tu_dien rỗng nghĩa là KHÔNG chắc video nói tiếng Việt — xem ghi chú ở đầu module.
kw = {"beam_size": 5}
if tu_dien:
    kw["initial_prompt"] = tu_dien
    kw["language"] = "vi"
segs, info = m.transcribe(duong_dan, **kw)
txt = " ".join(s.text.strip() for s in segs).strip()
print(json.dumps({"text": txt, "language": info.language}, ensure_ascii=False))
'''


def san_sang() -> bool:
    """Có cài faster-whisper chưa — để phía gọi biết mà báo lỗi cho tử tế."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def boc_loi_thoai(duong_dan_audio: str, timeout: int = 600, tieng_viet: bool = True) -> tuple[str, str]:
    """
    Trả về lời thoại, hoặc chuỗi rỗng nếu hỏng. KHÔNG ném lỗi — phía gọi còn nguồn dự phòng
    khác (phụ đề Facebook), làm hỏng cả luồng vì một lần nhận dạng thất bại là không đáng.
    """
    if not os.path.exists(duong_dan_audio):
        logger.warning('[LocalWhisper] Không thấy file audio: %s', duong_dan_audio)
        return '', ''

    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(_RUNNER)
        runner = f.name

    moi_truong = {
        **os.environ,
        # Chỉ đặt trong tiến trình CON — xem ghi chú đầu file về xung đột OpenMP.
        'KMP_DUPLICATE_LIB_OK': 'TRUE',
        # Không thả hết lõi: máy còn phải chạy Django, Postgres và dev server của FE.
        'OMP_NUM_THREADS': os.environ.get('LOCAL_WHISPER_THREADS', '4'),
    }

    try:
        p = subprocess.run(
            [sys.executable, runner, duong_dan_audio, MODEL_SIZE, GLOSSARY if tieng_viet else ''],
            capture_output=True, text=True, timeout=timeout, env=moi_truong,
        )
        # Tiến trình con in cả log của thư viện ra stdout, nên lấy DÒNG CUỐI parse được JSON
        # chứ không parse cả khối.
        for dong in reversed((p.stdout or '').strip().splitlines()):
            dong = dong.strip()
            if dong.startswith('{'):
                kq = json.loads(dong)
                return (kq.get('text') or '').strip(), (kq.get('language') or '')
        logger.warning('[LocalWhisper] Không đọc được JSON. stderr: %s', (p.stderr or '')[-300:])
        return '', ''
    except subprocess.TimeoutExpired:
        logger.warning('[LocalWhisper] Quá %ss cho %s', timeout, duong_dan_audio)
        return '', ''
    except Exception as e:
        logger.warning('[LocalWhisper] Lỗi: %s', e)
        return '', ''
    finally:
        try:
            os.remove(runner)
        except Exception:
            pass
