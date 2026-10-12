"""RapidAPI Facebook chỉ dùng MỘT key, hết hạn mức thì thay trên Railway.

Trước đây code gom key từ RAPIDAPI_FACEBOOK_KEY + RAPIDAPI_FACEBOOK_KEY_BACKUP, tách theo
dấu phẩy, rồi tự xoay vòng khi gặp 429/403. Bỏ cơ chế đó vì vận hành thực tế đơn giản hơn:
một key, hết thì lên Railway đổi.

Hai điều quan trọng khi bỏ xoay vòng:

  1. Biến môi trường hiện tại ĐANG chứa 3 key nối bằng dấu phẩy. Nếu lấy nguyên chuỗi làm
     key thì header gửi đi là "key1,key2,key3" — hỏng toàn bộ, mà lỗi trả về chỉ là 401/403
     chung chung nên rất khó lần ra. Phải lấy key đầu và cảnh báo.

  2. Không còn key dự phòng thì log phải NÓI RÕ PHẢI LÀM GÌ. Trước kia hết quota chỉ là
     "chuyển sang key tiếp theo"; giờ đó là điểm dừng, người trực phải biết đi thay key.

Chạy: python manage.py test tests.test_rapidapi_single_key
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from video_management.services import rapidapi_facebook as svc


def _response(status=200, text='', json_body=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body if json_body is not None else {}
    r.raise_for_status.return_value = None
    return r


class GetApiKeyTests(SimpleTestCase):
    @override_settings(RAPIDAPI_FACEBOOK_KEY='abc123')
    def test_tra_ve_key_da_cat_khoang_trang(self):
        self.assertEqual(svc._get_api_key(), 'abc123')

    @override_settings(RAPIDAPI_FACEBOOK_KEY='  abc123  ')
    def test_cat_khoang_trang_thua(self):
        self.assertEqual(svc._get_api_key(), 'abc123')

    @override_settings(RAPIDAPI_FACEBOOK_KEY='')
    def test_chua_cau_hinh_tra_chuoi_rong(self):
        self.assertEqual(svc._get_api_key(), '')

    @override_settings(RAPIDAPI_FACEBOOK_KEY='key_dau,key_hai,key_ba')
    def test_env_con_nhieu_key_thi_lay_key_dau(self):
        """Biến môi trường cũ còn 3 key nối bằng dấu phẩy.

        Lấy nguyên chuỗi sẽ gửi header "key_dau,key_hai,key_ba" và hỏng toàn bộ request,
        nên phải lấy phần đầu để hệ thống vẫn chạy sau khi deploy.
        """
        self.assertEqual(svc._get_api_key(), 'key_dau')


class RequestWithKeyTests(SimpleTestCase):
    @override_settings(RAPIDAPI_FACEBOOK_KEY='')
    def test_chua_cau_hinh_thi_khong_goi_mang(self):
        with patch.object(svc.requests, 'get') as mock_get:
            self.assertIsNone(svc._request_with_key('https://x/y', {}))
            mock_get.assert_not_called()

    @override_settings(RAPIDAPI_FACEBOOK_KEY='abc123')
    def test_chi_goi_dung_MOT_lan_khi_het_quota(self):
        """Không còn key dự phòng nên không được thử lại — thử lại chỉ tốn thêm lượt gọi."""
        resp = _response(status=429, text='You have exceeded the MONTHLY quota')
        with patch.object(svc.requests, 'get', return_value=resp) as mock_get:
            self.assertIsNone(svc._request_with_key('https://x/y', {}))
            self.assertEqual(mock_get.call_count, 1)

    @override_settings(RAPIDAPI_FACEBOOK_KEY='abc123')
    def test_gui_dung_key_trong_header(self):
        resp = _response(json_body=[{'ad_page_id': '1'}])
        with patch.object(svc.requests, 'get', return_value=resp) as mock_get:
            svc._request_with_key('https://x/y', {'link': 'z'})
            headers = mock_get.call_args.kwargs['headers']
            self.assertEqual(headers['x-rapidapi-key'], 'abc123')

    @override_settings(RAPIDAPI_FACEBOOK_KEY='abc123')
    def test_thanh_cong_thi_tra_ve_response(self):
        resp = _response(json_body=[{'ad_page_id': '1'}])
        with patch.object(svc.requests, 'get', return_value=resp):
            self.assertIs(svc._request_with_key('https://x/y', {}), resp)

    @override_settings(RAPIDAPI_FACEBOOK_KEY='abc123')
    def test_bat_loi_than_2xx_cua_api4(self):
        """api4 báo lỗi bằng HTTP 209 + {"success": false} — 2xx nên raise_for_status() bỏ qua."""
        resp = _response(status=209, json_body={'success': False, 'message': 'Access conflict'})
        with patch.object(svc.requests, 'get', return_value=resp):
            self.assertIsNone(svc._request_with_key('https://x/y', {}))
