"""Chức năng: hết ngân sách token được báo là lỗi ngân sách, không đổ cho nhà cung cấp.

Vì sao đáng một file test riêng: khi model suy luận tiêu hết `max_tokens` vào phần suy luận nội
bộ, DeepSeek trả HTTP 200 với `content` rỗng và `finish_reason='length'`. Bản cũ gộp ca này vào
kind `'server'`, tức là câu người dùng cuối đọc được thành "DeepSeek đang lỗi ở phía nhà cung
cấp. Thử lại sau ít phút."

Câu đó sai ở cả hai vế. Nhà cung cấp KHÔNG lỗi — nó trả 200 và tính tiền đủ số token đã tiêu.
Và thử lại cũng vô ích: cùng model, cùng prompt, cùng ngân sách thì ra đúng cùng kết quả — đo
ngày 13/08/2026, ba lượt liên tiếp cho ra reasoning_tokens giống hệt nhau tới từng con số.

Sự cố thật ngày 13/08/2026: tính năng Chuyển đổi nội dung chết 100%, log AI nói rõ nguyên nhân
là ngân sách token, nhưng toast lại bảo đi chờ nhà cung cấp — đủ để mất cả buổi tìm nhầm chỗ.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from unittest.mock import Mock, patch  # noqa: E402
from django.test import SimpleTestCase  # noqa: E402

from video_management.services.content_generation_service import (  # noqa: E402
    ContentGenerationService,
    DeepSeekError,
)
from video_management.views.content_generation_views import DEEPSEEK_ERROR_MESSAGES  # noqa: E402


def deepseek_reply(finish_reason: str, content: str = ''):
    """Dựng đúng shape phản hồi 200 của DeepSeek khi content rỗng."""
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        'choices': [{'message': {'content': content}, 'finish_reason': finish_reason}],
        'usage': {
            'completion_tokens': 2048,
            'completion_tokens_details': {'reasoning_tokens': 2048},
        },
    }
    return response


def call_deepseek(response):
    service = ContentGenerationService()
    service.deepseek_key = 'khoa-gia-de-test'
    with patch('video_management.services.content_generation_service.requests.post',
               return_value=response):
        return service._call_deepseek_checked(prompt='xin chào', system_msg='viết lại')


class HetNganSachTokenBaoDungLoai(SimpleTestCase):
    def test_content_rong_vi_length_khong_con_bi_do_cho_nha_cung_cap(self):
        with self.assertRaises(DeepSeekError) as bat:
            call_deepseek(deepseek_reply('length'))

        # 'server' là chỗ bản cũ nói sai — nó dẫn thẳng tới câu "lỗi ở phía nhà cung cấp".
        self.assertNotEqual(bat.exception.kind, 'server')
        self.assertEqual(bat.exception.kind, 'token_budget')

    def test_van_thu_lai_duoc_nhung_phai_doi_tham_so(self):
        """Thử lại Y NGUYÊN thì vô ích, nhưng caller đổi max_tokens/tắt suy luận là qua được —
        nên vẫn nằm trong RETRIABLE_KINDS. Câu báo phải nói rõ cần đổi cái gì."""
        with self.assertRaises(DeepSeekError) as bat:
            call_deepseek(deepseek_reply('length'))

        self.assertTrue(bat.exception.retriable)

    def test_content_rong_khong_phai_do_length_van_giu_loai_cu(self):
        """Chỉ ca 'length' mới là lỗi ngân sách; rỗng vì lý do khác vẫn là dữ liệu hỏng."""
        with self.assertRaises(DeepSeekError) as bat:
            call_deepseek(deepseek_reply('stop'))

        self.assertEqual(bat.exception.kind, 'parse')

    def test_cau_bao_chi_dung_cho_nguoi_van_hanh_can_sua_gi(self):
        message = DEEPSEEK_ERROR_MESSAGES['token_budget']

        self.assertNotIn('nhà cung cấp', message.lower())
        # Phải nêu được thứ cần chỉnh, nếu không thì vẫn là câu vô nghĩa với người đọc.
        self.assertTrue(
            'token' in message.lower() or 'model' in message.lower(),
            f'câu báo phải chỉ ra chỗ cần sửa, đang là: {message}',
        )
