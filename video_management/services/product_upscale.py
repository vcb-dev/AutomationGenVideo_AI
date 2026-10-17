"""
Phóng ảnh SP nhỏ trước khi tách nền — 0đ, onnxruntime CPU. Người dùng soát 15/10: ảnh lưu từ Google chỉ 192×256
(3KB), túi ~150px mà khi ghép phải phóng ~7 lần → vỡ hạt, nhoè. Real-ESRGAN general-x4v3 (BSD-3, xinntao/Real-ESRGAN,
trộn khử nhiễu 0,5 như mặc định của tác giả) phóng ×4 làm nét mép/quai/khoá; phóng trước rembg thì mặt nạ cũng nét theo.
Đo trên container AI (4 nhân): ảnh 192×256 ~1,8s, 547×365 ~8,5s.

Biến môi trường BẮT BUỘC (không mặc định — thiếu thì báo đúng tên biến):
  UPSCALE_MODEL_ENV  file ONNX (vào: RGB float 0–1 NCHW, ra: ×4) — xuất bằng
                     bag-detector-training/quality/scripts/export_realesrgan.py
"""
import os
import threading

import numpy as np
from PIL import Image

from video_management.services.product_image_service import ProductImageConfigError

UPSCALE_MODEL_ENV = 'PRODUCT_IMAGE_UPSCALE_MODEL_PATH'
# Ảnh có cạnh dài nhỏ hơn mốc này mới phóng: túi trong ảnh ~70% khung → dưới ~500px, mà khi ghép túi cao ~1100px
# (nền dọc 2560px × 0,43) → phải phóng > 2 lần. Ảnh lớn hơn phóng thường đã đủ nét; phóng bằng model thì tốn thời
# gian theo số điểm ảnh (700×700 ~20s) mà không nét hơn bao nhiêu.
UPSCALE_BELOW_SIDE_PX = 700
# Ảnh đưa vào model không quá cạnh dài này (thu nhỏ trước nếu lớn hơn): thời gian chạy theo số điểm ảnh — ảnh
# 680px mất ~16s chỉ riêng bước phóng, cả lượt tách nền ~27s (đo 15/10). 384×4 = 1536px vẫn dư cho túi khi ghép
# (~1100px), các bước sau (rembg, làm sạch) cũng nhẹ đi theo.
UPSCALE_MAX_INPUT_SIDE_PX = 384
MODEL_SCALE = 4

_sessions = {}
_sessions_lock = threading.Lock()


def read_upscale_model_path():
    path = (os.environ.get(UPSCALE_MODEL_ENV) or '').strip()
    if not path:
        raise ProductImageConfigError(
            f'{UPSCALE_MODEL_ENV} chưa được set trên AI Service (file ONNX Real-ESRGAN phóng ảnh SP nhỏ trước khi '
            f'tách nền).'
        )
    if not os.path.isfile(path):
        raise ProductImageConfigError(f'{UPSCALE_MODEL_ENV}={path} không tồn tại trên AI Service.')
    return path


def get_upscaler(path):
    """Session ONNX nạp một lần mỗi worker (như session rembg / model nhận diện)."""
    with _sessions_lock:
        session = _sessions.get(path)
        if session is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            try:
                session = ort.InferenceSession(path, sess_options=options, providers=['CPUExecutionProvider'])
            except Exception as e:
                raise ProductImageConfigError(f'{UPSCALE_MODEL_ENV}={path} không phải model ONNX hợp lệ: {e}')
            _sessions[path] = session
        return session


def upscale_if_small(session, image):
    """Ảnh RGB nhỏ (cạnh dài < UPSCALE_BELOW_SIDE_PX) → phóng ×4; ảnh khác giữ nguyên. Trả (ảnh, hệ số phóng)."""
    if max(image.size) >= UPSCALE_BELOW_SIDE_PX:
        return image, 1
    image = image.convert('RGB')
    if max(image.size) > UPSCALE_MAX_INPUT_SIDE_PX:
        ratio = UPSCALE_MAX_INPUT_SIDE_PX / max(image.size)
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)
    pixels = np.asarray(image, np.float32).transpose(2, 0, 1)[None] / 255
    output = session.run(None, {session.get_inputs()[0].name: pixels})[0][0]
    out = (np.clip(output, 0, 1) * 255 + 0.5).astype(np.uint8).transpose(1, 2, 0)
    return Image.fromarray(out, 'RGB'), MODEL_SCALE
