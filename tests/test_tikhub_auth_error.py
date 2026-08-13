"""TikHub từ chối xác thực thì phải NÉM lỗi, không được trả danh sách rỗng.

Đo trên hệ thống thật ngày 12/08/2026: token TikHub hết hạn, mọi endpoint trả
403 `API token has expired`. Các service nuốt lỗi rồi `break`, trả `[]`, nên view
đáp 200 với danh sách rỗng và BE kết luận "Không tìm thấy posts cho username này"
— rồi XOÁ luôn profile vừa tạo. Người dùng thấy "kênh không tồn tại" trong khi
thật ra là hết hạn token; cả 7 nền tảng cùng chẩn đoán sai một kiểu.

Khác biệt cốt lõi: "kênh không có video" và "khoá API chết" là HAI việc khác nhau,
không được cùng biểu diễn bằng `[]`. Mất phân biệt ở tầng này thì không tầng nào
phía trên khôi phục lại được.

Lỗi mạng thoáng qua (timeout, 500, đứt kết nối) vẫn giữ nguyên nết cũ: trả về
những gì đã lấy được. Chỉ 401/403/429 mới ném — đó là nhóm "người vận hành phải
ra tay", không phải nhóm tự khỏi khi thử lại.

Chạy: python manage.py test tests.test_tikhub_auth_error
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings


def _resp(status: int, body: dict | None = None):
    """Phản hồi giả của `requests` — raise_for_status ném HTTPError như hàng thật."""
    r = MagicMock()
    r.status_code = status
    r.ok = status < 400
    r.json.return_value = body if body is not None else {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f'{status} Client Error', response=r)
    else:
        r.raise_for_status.return_value = None
    return r


TOKEN_HET_HAN = {
    'detail': {
        'code': 403,
        'message': 'API token has expired, see the docs for more information',
    }
}


@override_settings(TIKHUB_API_KEY='khoa-test', TIKHUB_API_BASE_URL='https://tikhub.test')
class TikTokProfileAuthError(SimpleTestCase):
    def test_403_nem_loi_thay_vi_tra_rong(self):
        from video_management.services import tikhub_tiktok_profile as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_user_posts('vtv24news', count=5)

    def test_thong_bao_noi_ro_la_van_de_khoa_api(self):
        """Thông báo phải đủ để người trực nhận ra cần gia hạn token, không phải đi sửa kênh."""
        from video_management.services import tikhub_tiktok_profile as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError) as bat:
                m.fetch_user_posts('vtv24news', count=5)

        self.assertIn('TikHub', str(bat.exception))

    def test_loi_mang_thoang_qua_van_tra_ve_binh_thuong(self):
        """500 là lỗi tự khỏi khi thử lại — ném ở đây sẽ biến sự cố nhỏ thành sập cào."""
        from video_management.services import tikhub_tiktok_profile as m

        with patch.object(m.requests, 'get', return_value=_resp(500)):
            self.assertEqual(m.fetch_user_posts('vtv24news', count=5), [])


@override_settings(TIKHUB_API_KEY='khoa-test', TIKHUB_API_BASE_URL='https://tikhub.test')
class CacNenTangKhacAuthError(SimpleTestCase):
    """Bảy nền tảng dùng chung một khoá TikHub — hết hạn là chết cả bảy, nên cả bảy phải ném."""

    def test_douyin(self):
        from video_management.services import tikhub_douyin as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_douyin_user_videos('MS4wLjABAAAA-sec-uid', count=5)

    def test_instagram(self):
        from video_management.services import tikhub_instagram as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_user_info('vtv24news')

    def test_youtube(self):
        from video_management.services import tikhub_youtube as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_channel_info('UCtest')

    def test_kuaishou(self):
        from video_management.services import tikhub_kuaishou as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_one_user_v2('3xsu5fmuvtupp4i')

    def test_bilibili(self):
        from video_management.services import tikhub_bilibili as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_user_info('1140672573')

    def test_xiaohongshu(self):
        from video_management.services import tikhub_xiaohongshu as m
        from video_management.services.tikhub_errors import TikHubAuthError

        with patch.object(m.requests, 'get', return_value=_resp(403, TOKEN_HET_HAN)):
            with self.assertRaises(TikHubAuthError):
                m.fetch_xhs_user_video_notes('5f3e4c3b0000000001005b0e', count=5)


class TraLoiHttpChoBE(SimpleTestCase):
    """Ném thôi chưa đủ — BE phải ĐỌC được lý do, nếu không lại quay về đoán mò.

    Không có handler thì DRF biến ngoại lệ lạ thành 500 kèm trang HTML; axios bên BE
    chỉ thấy "Request failed with status code 500" và người dùng vẫn không biết vì sao.
    502 Bad Gateway là mã đúng nghĩa: lỗi nằm ở nhà cung cấp phía sau, không phải ở
    yêu cầu người dùng gửi lên.
    """

    def test_tikhub_auth_error_thanh_502_kem_ly_do(self):
        from rest_framework.exceptions import APIException

        from core.exception_handler import xu_ly_ngoai_le
        from video_management.services.tikhub_errors import TikHubAuthError

        resp = xu_ly_ngoai_le(TikHubAuthError('TikHub từ chối yêu cầu (403)', status=403), {})

        self.assertIsNotNone(resp, 'Handler phải tự đáp, không được trả None cho DRF dựng 500')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('TikHub', resp.data['error'])

    def test_ngoai_le_khac_van_do_drf_xu_ly(self):
        """Handler chỉ nhận đúng phần việc của mình — không nuốt lỗi của người khác."""
        from core.exception_handler import xu_ly_ngoai_le

        self.assertIsNone(xu_ly_ngoai_le(ValueError('chuyện khác'), {}))
