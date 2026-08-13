"""Chức năng: MiniMax từ chối thì báo đúng lý do và việc cần làm, không ném dữ liệu thô.

Vì sao đáng một file test riêng: chuỗi lỗi ở đây không nằm lại trong log. BE bọc nguyên văn vào
response rồi FE hiện thẳng trong toast, nên nó là câu người bán hàng đọc được. Bản cũ ném cả dict
Python ra màn hình:

    No audio in Minimax response: {'base_resp': {'status_code': 2053,
    'status_msg': 'insufficient credit. Please purchase top-up credits or upgrade your
    subscription plan'}}

Đo thật ngày 13/08/2026, cả hai chức năng chính của trang Clone giọng đều chết vì giới hạn tài
khoản, và không chỗ nào trong hệ thống cảnh báo trước:

    TTS   → 2053 insufficient credit      (hết tiền)
    Clone → 2052 insufficient voice slot  (hết chỗ chứa giọng)

Hai mã này là chuyện vận hành có người xử lý được (nạp tiền, xoá bớt giọng), khác hẳn lỗi kỹ
thuật — nên phải nói thành câu người vận hành hiểu. Mã lạ thì vẫn giữ nguyên số và nguyên văn
tiếng Anh để còn tra được, thà dài còn hơn nuốt mất thông tin.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.test import SimpleTestCase  # noqa: E402

from video_management.services.minimax_errors import (  # noqa: E402
    minimax_error_from_response,
    minimax_error_message,
)


class MinimaxBaoDungLyDo(SimpleTestCase):
    def test_het_tien_noi_ro_la_het_tien_va_phai_nap(self):
        message = minimax_error_message(2053, 'insufficient credit. Please purchase top-up credits')

        self.assertIn('hết', message.lower())
        self.assertIn('nạp', message.lower())
        # Không được ném nguyên văn tiếng Anh của nhà cung cấp ra cho người bán hàng đọc.
        self.assertNotIn('insufficient credit', message)

    def test_het_slot_giong_noi_ro_phai_xoa_bot_hoac_nang_goi(self):
        message = minimax_error_message(2052, 'insufficient voice slot')

        self.assertIn('slot', message.lower())
        self.assertTrue(
            'xoá' in message.lower() or 'xóa' in message.lower(),
            f'phải nói được cách giải phóng slot, đang là: {message}',
        )

    def test_hai_ma_nay_khong_bi_lan_lon_voi_nhau(self):
        """Hết tiền và hết chỗ là hai việc xử lý khác nhau — nạp tiền không giải phóng slot."""
        self.assertNotEqual(minimax_error_message(2053), minimax_error_message(2052))

    def test_giong_khong_ton_tai_bao_dung_thay_vi_do_cho_tai_khoan(self):
        message = minimax_error_message(2054, 'voice id not exist')

        self.assertIn('giọng', message.lower())
        self.assertNotIn('nạp', message.lower())

    def test_ma_la_van_giu_nguyen_so_va_nguyen_van_de_con_tra_duoc(self):
        message = minimax_error_message(9999, 'something entirely new')

        self.assertIn('9999', message)
        self.assertIn('something entirely new', message)

    def test_doc_duoc_ma_loi_nam_trong_base_resp_cua_phan_hoi_tts(self):
        """Đúng shape mà TTS nhận được khi MiniMax trả 200 nhưng không kèm audio."""
        data = {'base_resp': {'status_code': 2053, 'status_msg': 'insufficient credit'}}

        self.assertEqual(minimax_error_from_response(data), minimax_error_message(2053, 'insufficient credit'))

    def test_phan_hoi_khong_co_base_resp_van_tra_cau_gi_do_doc_duoc(self):
        message = minimax_error_from_response({'gì đó': 'lạ hoắc'})

        self.assertTrue(message)
        self.assertNotIn('base_resp', message)
