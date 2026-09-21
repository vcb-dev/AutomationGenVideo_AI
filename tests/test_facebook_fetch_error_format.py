import unittest
from unittest.mock import MagicMock
from django.test import SimpleTestCase
from video_management.views.facebook_fetch_views import _format_facebook_error


class TestFacebookFetchErrorFormat(SimpleTestCase):
    """Kiểm tra hàm format lỗi từ Facebook Graph API thành thông báo dễ hiểu cho người dùng."""

    def test_permission_error_code_100(self):
        err = Exception("Graph error")
        response_mock = MagicMock()
        response_mock.json.return_value = {
            'error': {'code': 100, 'message': 'Unsupported get request. Object with ID does not exist.'}
        }
        err.response = response_mock

        formatted = _format_facebook_error(err)
        self.assertIn("Tài khoản Meta chưa cấp quyền cho Page này", formatted)

    def test_expired_token_code_190(self):
        err = Exception("Graph error")
        response_mock = MagicMock()
        response_mock.json.return_value = {
            'error': {'code': 190, 'message': 'Error validating access token: Session has expired.'}
        }
        err.response = response_mock

        formatted = _format_facebook_error(err)
        self.assertIn("Access token Facebook đã hết hạn hoặc bị thu hồi", formatted)

    def test_rate_limit_code_4(self):
        err = Exception("Graph error")
        response_mock = MagicMock()
        response_mock.json.return_value = {
            'error': {'code': 4, 'message': 'Application request limit reached'}
        }
        err.response = response_mock

        formatted = _format_facebook_error(err)
        self.assertIn("Facebook API bị giới hạn tần suất (Rate limit)", formatted)

    def test_generic_permission_error_code_200(self):
        err = Exception("Graph error")
        response_mock = MagicMock()
        response_mock.json.return_value = {
            'error': {'code': 200, 'message': 'Requires pages_read_user_content permission'}
        }
        err.response = response_mock

        formatted = _format_facebook_error(err)
        self.assertIn("Thiếu quyền truy cập Facebook", formatted)

    def test_fallback_regular_exception(self):
        err = Exception("Network timeout connecting to Graph API")
        formatted = _format_facebook_error(err)
        self.assertEqual(formatted, "Network timeout connecting to Graph API")


if __name__ == '__main__':
    unittest.main()
