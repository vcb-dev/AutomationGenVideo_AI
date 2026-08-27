"""`transcribe_upload` phải nhận ngân sách thời gian từ BE và neo nó tại LÚC VÀO VIEW.

Trước đây ngân sách (GEMINI_TOTAL_BUDGET = 55s) được neo tại `poll_started_at`, tức tính TỪ SAU
khi `genai.upload_file()` đã xong — trong khi chính upload_file mới là giai đoạn đắt nhất và
tăng theo dung lượng file. Timeout phía BE thì lại là đồng hồ treo tường phủ trọn mọi giai đoạn,
nên với video dài BE luôn tự huỷ trước ở giây 60 và để lại lỗi "timeout of 60000ms exceeded".
Đo thật với video thật: 76.5MB tốn 114.3s, 143.5MB tốn 109.8s, cùng file 76.5MB lần khác tốn
239.6s — không lần nào lọt nổi mốc 60s.

Test ở đây khoá phần thuần logic của bản sửa: ngân sách đọc từ `timeout_seconds` do BE gửi, số
rác/thiếu thì về mặc định, luôn bị kẹp vào [MIN, MAX] để client gọi thẳng không giữ worker bao
lâu tuỳ thích, và mặc định của Django khớp đúng mốc BE.

Phần còn lại của bản sửa (deadline cắt đúng giai đoạn nào, dọn file trên Gemini, bỏ qua lệnh
generate tính phí khi đã hết giờ) chỉ kiểm chứng được bằng file thật + Gemini thật nên đã kiểm
bằng tay, không dựng mock ở đây — mock lại chính đồng hồ và chính SDK đang là đối tượng cần
kiểm sẽ chỉ khẳng định lại giả định của người viết test.

Chạy: python manage.py test tests.test_transcribe_upload_budget
"""

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import transcribe_views


class ReadTranscribeBudgetTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _budget(self, **body):
        from rest_framework.request import Request
        from rest_framework.parsers import JSONParser

        raw = self.factory.post('/api/content/transcribe-upload/', body, format='json')
        return transcribe_views._read_transcribe_budget(Request(raw, parsers=[JSONParser()]))

    def test_thieu_timeout_seconds_thi_dung_mac_dinh(self):
        self.assertEqual(self._budget(), transcribe_views.TRANSCRIBE_TOTAL_BUDGET_DEFAULT)

    def test_gia_tri_rac_thi_dung_mac_dinh(self):
        self.assertEqual(self._budget(timeout_seconds='khong-phai-so'),
                         transcribe_views.TRANSCRIBE_TOTAL_BUDGET_DEFAULT)

    def test_dung_dung_gia_tri_BE_gui(self):
        self.assertEqual(self._budget(timeout_seconds=420), 420)

    def test_kep_vao_khoang_cho_phep(self):
        self.assertEqual(self._budget(timeout_seconds=1),
                         transcribe_views.TRANSCRIBE_TOTAL_BUDGET_MIN)
        self.assertEqual(self._budget(timeout_seconds=99999),
                         transcribe_views.TRANSCRIBE_TOTAL_BUDGET_MAX)

    def test_mac_dinh_khop_ngan_sach_BE(self):
        """Django phải mặc định đúng bằng CONTENT_TRANSFORM_TRANSCRIBE_TIMEOUT_MS của BE (420s).

        Hai phía lệch nhau là tái lập đúng loại lỗi cũ: bên này tưởng còn thời gian, bên kia đã
        bỏ cuộc.
        """
        self.assertEqual(transcribe_views.TRANSCRIBE_TOTAL_BUDGET_DEFAULT, 420)
