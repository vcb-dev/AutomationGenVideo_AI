"""Chức năng: view transform_content tự chọn model, không dùng model suy luận mặc định.

Vì sao đáng một file test riêng: `DEEPSEEK_MODEL` toàn cục đang là `deepseek-v4-flash` — model
SUY LUẬN. Token suy luận bị trừ THẲNG vào `max_tokens`, và với prompt hệ của nhân vật (đo được
29.424 ký tự) nó suy luận không dứt. Đo thật ngày 13/08/2026, cùng một prompt hệ đó:

    v4-flash  max_tokens=2048   → 19s,  reasoning=2048,  content RỖNG
    v4-flash  max_tokens=4096   → 39s,  reasoning=4096,  content RỖNG
    v4-flash  max_tokens=8192   → 66s,  reasoning=8192,  content RỖNG
    v4-flash  max_tokens=16384  → 105s, reasoning=11336, content 1508 ký tự
    deepseek-chat max_tokens=2048 → 10s, finish=stop,    content 1911 ký tự

Nâng ngân sách KHÔNG cứu được: model ăn trọn bao nhiêu cũng hết, chỉ chậm thêm. Ngưỡng thật sự
dùng được là 16384 token và 105 giây — vượt cả timeout của AI (60s) lẫn của BE (30s), nên tính
năng hỏng 100%. Viết lại kịch bản theo giọng nhân vật là việc văn phong, không phải việc suy
luận: model thường cho ra kết quả DÀI HƠN trong 1/10 thời gian và 1/6 số token.

Test khoá đúng một điều: view phải tự nói mình dùng model nào. Bỏ dòng đó ra là lại rơi vào
`DEEPSEEK_DEFAULT_MODEL` và chết lại y như cũ, mà không có gì báo.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from unittest.mock import patch  # noqa: E402
from django.test import SimpleTestCase  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402

from video_management.views.content_generation_views import (  # noqa: E402
    CONTENT_TRANSFORM_MODEL,
    transform_content,
)

# Dòng model suy luận của DeepSeek — đã đo là không dùng được cho prompt hệ cỡ này.
REASONING_MODEL_MARKERS = ('v4-flash', 'reasoner')


def call_transform_view():
    """Gọi view với DeepSeek đã bị thay, trả về kwargs mà view truyền xuống DeepSeek."""
    factory = APIRequestFactory()
    request = factory.post(
        '/api/ai/transform-content/',
        {'system_prompt': 'Viết lại cho hay hơn', 'input_text': 'Nhẫn vàng 18k giảm 20%.'},
        format='json',
    )
    target = 'video_management.views.content_generation_views.ContentGenerationService'
    with patch(target) as Service:
        instance = Service.return_value
        instance._call_deepseek_checked.return_value = 'Kịch bản đã viết lại.'
        transform_content(request)
        return instance._call_deepseek_checked.call_args.kwargs


class TransformTuChonModel(SimpleTestCase):
    def test_noi_ro_model_chu_khong_roi_vao_mac_dinh_toan_cuc(self):
        kwargs = call_transform_view()

        self.assertIn(
            'model', kwargs,
            'view phải tự chọn model; để trống là nhận DEEPSEEK_MODEL toàn cục (đang là model suy luận)',
        )
        self.assertEqual(kwargs['model'], CONTENT_TRANSFORM_MODEL)

    def test_model_mac_dinh_khong_phai_dong_suy_luan(self):
        for marker in REASONING_MODEL_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, CONTENT_TRANSFORM_MODEL)
