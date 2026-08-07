"""Tạo giọng clone: mọi thứ có thể từ chối phải được kiểm TRƯỚC khi gọi MiniMax.

Đây là chốt tiền. Thứ tự trong clone_voice_start_api là: gọi MiniMax (tính phí,
chiếm một slot giọng) rồi mới ghi bản ghi Voice. Nên bất kỳ dữ liệu nào hợp lệ
với view mà DB lại từ chối sẽ cho ra kết cục tệ nhất có thể: người dùng bị trừ
tiền, giọng nằm lại bên MiniMax, DB không có dòng nào để trỏ tới nó mà xoá — đúng
loại "giọng mồ côi" mà luồng xoá đã cẩn thận tránh (xem test_xoa_giong_clone).
Người dùng chỉ thấy "clone thất bại" nên nhiều khả năng bấm lại, mất phí lần nữa.

Cột name giới hạn 255 ký tự, gender 20 — trước đây không tầng nào kiểm (ô nhập
FE không có maxLength, BE chỉ kiểm rỗng), nên tên dài là rơi đúng vào kịch bản trên.

Chạy: python manage.py test tests.test_tao_giong_clone
"""

from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import voice_views


class TaoGiongClone(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _goi(self, **fields):
        du_lieu = {
            'file': _file_audio(),
            'voice_name': 'KOC Lan',
            'gender': 'female',
        }
        du_lieu.update(fields)
        request = self.factory.post('/api/voice/clone/start/', du_lieu, format='multipart')
        return voice_views.clone_voice_start_api(request)

    def test_ten_dai_hon_255_bi_chan_TRUOC_khi_goi_minimax(self):
        """Chốt chính: chặn ở view, không để DB chặn sau khi đã mất tiền."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading') as threading:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi(voice_name='A' * 256)

        self.assertEqual(res.status_code, 400)
        self.assertIn('255', str(res.data['error']))
        threading.Thread.assert_not_called()

    def test_ten_dung_255_ky_tu_van_chay(self):
        """Chặn đúng ở ngưỡng cột, không chặn nhầm tên hợp lệ."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading') as threading:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi(voice_name='A' * 255)

        self.assertEqual(res.status_code, 200)
        threading.Thread.assert_called_once()

    def test_ten_toan_khoang_trang_bi_chan(self):
        """'   ' là chuỗi truthy trong Python nên lọt qua kiểm 'not voice_name'."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading') as threading:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi(voice_name='   ')

        self.assertEqual(res.status_code, 400)
        threading.Thread.assert_not_called()

    def test_gioi_tinh_la_bi_chan(self):
        """Cột gender chỉ chứa 20 ký tự; giá trị lạ cũng làm hỏng bộ lọc của FE."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading') as threading:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi(gender='x' * 40)

        self.assertEqual(res.status_code, 400)
        threading.Thread.assert_not_called()

    def test_ten_duoc_trim_truoc_khi_luu(self):
        """FE đã trim, nhưng view là nơi cuối cùng còn chặn được — tên lưu vào DB
        phải khớp đúng tên vừa đem đi so trùng, nếu không luật chặn trùng vô nghĩa."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading'), \
                patch.object(voice_views, 'progress_set') as progress_set:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi(voice_name='  KOC Lan  ')

        self.assertEqual(res.status_code, 200)
        self.assertEqual(progress_set.call_args[0][1]['voice_name'], 'KOC Lan')


def _file_audio():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile('mau.mp3', b'ID3fake-audio-bytes', content_type='audio/mpeg')
