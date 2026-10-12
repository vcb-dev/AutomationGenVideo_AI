"""Access token không bao giờ được lọt vào log.

Phát hiện ngày 09/08/2026 khi tái hiện sự cố lượt xem tại local: `str(e)` của requests nhúng
NGUYÊN URL kèm query string, nên mỗi lần request Graph API hỏng là token Facebook bị ghi thẳng
vào log dưới dạng chữ thường:

    ❌ Thất bại khi lấy reactions/comments/shares: 400 Client Error: Bad Request for url:
    https://graph.facebook.com/v25.0?ids=...&access_token=EAAfjNs8RNPABRr7JzniAkdvy...

Log production đã chứa token đầy đủ suốt 13 ngày sự cố. Token Page/User của Facebook đọc được
toàn bộ dữ liệu 106 fanpage, ai đọc được log là dùng được luôn.

Vì sao lọc ở tầng logger chứ không sửa từng lời gọi: service này có 47 lời gọi log, phần lớn
kèm str(e). Sửa tay thì lần sau thêm một dòng log mới là hở lại, mà không ai nhớ nổi quy tắc.

Chạy: python manage.py test tests.test_facebook_log_token_redaction
"""

import logging

from django.test import SimpleTestCase

from video_management.services.facebook_graph_service import _scrub_tokens

TOKEN = 'EAAfjNs8RNPABRr7JzniAkdvy5XzYtHFlEP0pA8RmraaUpmZC8DOKyrA7rORZB7KPNo9pqb2gLFQ'


class ScrubTokenTests(SimpleTestCase):
    def test_che_token_trong_url_cua_thong_bao_loi(self):
        raw = (
            f'400 Client Error: Bad Request for url: '
            f'https://graph.facebook.com/v25.0?ids=111_222&fields=shares&access_token={TOKEN}'
        )
        self.assertNotIn(TOKEN, _scrub_tokens(raw))

    def test_giu_nguyen_phan_con_lai_cua_thong_bao(self):
        """Che token nhưng vẫn phải đọc được lỗi là gì — nếu không thì log thành vô dụng."""
        raw = f'400 Client Error for url: https://x/?ids=111_222&access_token={TOKEN}&fields=shares'
        scrubbed = _scrub_tokens(raw)
        self.assertIn('400 Client Error', scrubbed)
        self.assertIn('ids=111_222', scrubbed)
        self.assertIn('fields=shares', scrubbed)

    def test_che_ca_khi_token_nam_cuoi_chuoi(self):
        """Không có & phía sau — regex phải dừng đúng ở cuối chuỗi."""
        self.assertNotIn(TOKEN, _scrub_tokens(f'url?access_token={TOKEN}'))

    def test_che_moi_lan_xuat_hien(self):
        raw = f'a access_token={TOKEN}&b access_token={TOKEN} c'
        self.assertNotIn(TOKEN, _scrub_tokens(raw))

    def test_chuoi_khong_co_token_thi_khong_doi(self):
        raw = 'Không lấy được views cho 2 bài'
        self.assertEqual(_scrub_tokens(raw), raw)


class LoggerFilterTests(SimpleTestCase):
    """Bộ lọc phải chạy ở tầng logger — đây mới là thứ bảo vệ 47 lời gọi log sẵn có."""

    def test_logger_cua_service_khong_ghi_ra_token(self):
        logger_name = 'video_management.services.facebook_graph_service'
        service_logger = logging.getLogger(logger_name)

        with self.assertLogs(logger_name, level='ERROR') as logs:
            service_logger.error(
                f'❌ Thất bại: 400 Client Error for url: https://g/?a=1&access_token={TOKEN}'
            )

        duoc_ghi = '\n'.join(logs.output)
        self.assertNotIn(TOKEN, duoc_ghi)
        self.assertIn('Thất bại', duoc_ghi)

    def test_loc_ca_khi_token_di_qua_tham_so_dinh_dang(self):
        """logger.error('%s', url) — token nằm trong args chứ không phải msg."""
        logger_name = 'video_management.services.facebook_graph_service'
        service_logger = logging.getLogger(logger_name)

        with self.assertLogs(logger_name, level='ERROR') as logs:
            service_logger.error('lỗi: %s', f'https://g/?access_token={TOKEN}')

        self.assertNotIn(TOKEN, '\n'.join(logs.output))
