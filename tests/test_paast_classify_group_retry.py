"""`_classify_group` phải tự thử lại TẠI CHỖ tối đa MAX_GROUP_ATTEMPTS lượt, đúng nhóm hỏng,
thay vì để BE chạy lại cả 5 nhóm phân tích PAAST (2c8c744).

Retry ở BE (tầng gọi HTTP) vứt bỏ cả 4 nhóm vừa thành công rồi tung lại xúc xắc 5 mặt — lỗi ở
đây thường là lỗi ngẫu nhiên của riêng 1 nhóm (429, JSON hỏng), thử lại đúng nhóm đó rẻ hơn
nhiều. Test này khoá: lỗi ngẫu nhiên (timeout/429/5xx/mạng/parse JSON) phải thử lại; lỗi tất
định (client/no_key) phải NÉM NGAY không tốn lượt thử; và sau khi thử hết MAX_GROUP_ATTEMPTS
lượt, phải báo lỗi cuối cùng thay vì lỗi đầu tiên.

Chạy: python manage.py test tests.test_paast_classify_group_retry
"""

import time
from unittest.mock import patch

from django.test import SimpleTestCase

from video_management.services.content_generation_service import DeepSeekError
from video_management.services.paast_analysis_service import (
    MAX_GROUP_ATTEMPTS,
    PaastAnalysisService,
)


ITEMS = [{'code': 'STOP', 'name_en': 'Stop', 'name_vi': 'Dừng lại', 'signal': 'hook mạnh'}]
GROUP_ARGS = dict(
    content='nội dung mẫu',
    group_key='action',
    group_label='Action',
    items=ITEMS,
    status_options='pass|miss',
    max_tokens=2048,
)


def _far_deadline():
    return time.monotonic() + 120  # dư ngân sách, không chạm nhánh "hết thời gian"


class ClassifyGroupRetryTests(SimpleTestCase):
    def setUp(self):
        self.service = PaastAnalysisService()

    def test_loi_ngau_nhien_thu_lai_roi_thanh_cong_khong_nem_len_tren(self):
        """timeout ở lượt 1, thành công ở lượt 2 — kết quả phải là kết quả THÀNH CÔNG, không
        phải lỗi của lượt 1."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'DeepSeek không trả lời trong 45s'),
                ('{"action": [{"code": "STOP", "status": "pass", "evidence": "..."}]}',
                 {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}),
            ]
            mock_extract.return_value = {'action': [{'code': 'STOP', 'status': 'pass', 'evidence': '...'}]}

            items, usage = self.service._classify_group(deadline=_far_deadline(), **GROUP_ARGS)

        self.assertEqual(items, [{'code': 'STOP', 'status': 'pass', 'evidence': '...'}])
        self.assertEqual(usage, {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})
        self.assertEqual(mock_call.call_count, 2)

    def test_loi_client_nem_ngay_khong_thu_lai(self):
        """4xx (request sai)/thiếu API key là lỗi TẤT ĐỊNH — thử lại chỉ tốn ngân sách của 4
        nhóm còn lại mà kết quả không đổi."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('client', 'DeepSeek từ chối request (400)', 400)

            with self.assertRaises(RuntimeError):
                self.service._classify_group(deadline=_far_deadline(), **GROUP_ARGS)

        self.assertEqual(mock_call.call_count, 1)

    def test_loi_no_key_nem_ngay_khong_thu_lai(self):
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = DeepSeekError('no_key', 'Chưa cấu hình DEEPSEEK_API_KEY')

            with self.assertRaises(RuntimeError):
                self.service._classify_group(deadline=_far_deadline(), **GROUP_ARGS)

        self.assertEqual(mock_call.call_count, 1)

    def test_json_hong_khong_tat_dinh_duoc_thu_lai(self):
        """LLM trả text không phải JSON hợp lệ — lỗi ngẫu nhiên (lượt sau thường ổn), phải
        được coi như lỗi retriable, không phải lỗi tất định."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call, \
                patch.object(self.service._gen, '_extract_json_dict') as mock_extract:
            mock_call.side_effect = [('not json trước', {}), ('{"action": [...]}', {})]
            mock_extract.side_effect = [{}, {'action': [{'code': 'STOP', 'status': 'pass', 'evidence': 'e'}]}]

            items, _usage = self.service._classify_group(deadline=_far_deadline(), **GROUP_ARGS)

        self.assertEqual(items, [{'code': 'STOP', 'status': 'pass', 'evidence': 'e'}])
        self.assertEqual(mock_call.call_count, 2)

    def test_het_MAX_GROUP_ATTEMPTS_luot_thi_nem_loi_CUOI_CUNG(self):
        """Ưu tiên lỗi gần nhất — dễ chẩn đoán hơn lỗi của lượt đầu tiên đã lỗi thời."""
        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            mock_call.side_effect = [
                DeepSeekError('timeout', 'lỗi lượt 1'),
                DeepSeekError('server', 'lỗi lượt 2'),
                DeepSeekError('network', 'lỗi lượt cuối'),
            ]

            with self.assertRaises(RuntimeError) as ctx:
                self.service._classify_group(deadline=_far_deadline(), **GROUP_ARGS)

        self.assertEqual(mock_call.call_count, MAX_GROUP_ATTEMPTS)
        self.assertIn('lỗi lượt cuối', str(ctx.exception))
        self.assertNotIn('lỗi lượt 1', str(ctx.exception))

    def test_khong_con_ngan_sach_thi_dung_ngay_khong_goi_them(self):
        """Deadline đã qua trước cả lượt đầu — không được cố gọi thêm 1 lượt vô ích."""
        past_deadline = time.monotonic() - 1

        with patch.object(self.service._gen, '_call_deepseek_checked') as mock_call:
            with self.assertRaises(RuntimeError) as ctx:
                self.service._classify_group(deadline=past_deadline, **GROUP_ARGS)

        mock_call.assert_not_called()
        self.assertIn('hết ngân sách thời gian', str(ctx.exception))
