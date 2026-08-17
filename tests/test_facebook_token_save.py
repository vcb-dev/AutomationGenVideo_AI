"""Token mới cấp qua OAuth phải được LƯU LẠI, không được đánh rơi.

── Gốc sự cố, đo ngày 16/08/2026 ───────────────────────────────────────────────
Luồng OAuth bên BE (`oauth.service.ts::handleFacebookCallback`) đổi code → long-lived token,
dùng token đó gọi `/me/accounts` rồi BỎ LUÔN. Hệ quả: cấp thêm quyền `instagram_manage_insights`
xong, hệ thống vẫn chạy token cũ trong .env và vẫn nhận `(#10) Application does not have
permission`. Người cấp quyền tưởng đã xong, thực tế không có gì thay đổi.

Vì sao không có đường nào khác: quyền Facebook đóng cứng vào token lúc phát hành. App được
duyệt thêm quyền KHÔNG làm token cũ mạnh lên, và `fb_exchange_token` chỉ đổi HẠN chứ không thêm
quyền — đã đo: token vừa gia hạn 60 ngày vẫn thiếu đúng quyền đó. Đường duy nhất để có quyền
mới là qua màn hình đồng ý của người dùng, tức luồng OAuth. Nên đánh rơi token ở đó là làm hỏng
cách duy nhất còn lại.

AI giữ token store (.fb_token.json) nên BE gửi token sang đây thay vì tự ghi file — giữ đúng
ranh giới sẵn có giữa hai repo.

Chạy: python manage.py test tests.test_facebook_token_save
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

TOKEN = 'EAA-token-dai-han-moi'


class FetchTokenSaveTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='be-system', password='x')
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, payload):
        return self.client.post(
            '/api/facebook/fetch/token-save/',
            data=payload,
            content_type='application/json',
        )

    def test_save_token_to_token_store(self):
        with patch('video_management.services.facebook_token_store.save_token') as fake_save:
            r = self._post({'access_token': TOKEN})

        self.assertEqual(r.status_code, 200)
        fake_save.assert_called_once()
        self.assertEqual(fake_save.call_args.args[0], TOKEN)

    def test_default_expiration_when_expires_in_not_provided(self):
        """Default to 60 days when expires_in is not provided."""
        with patch('video_management.services.facebook_token_store.save_token') as fake_save:
            r = self._post({'access_token': TOKEN})

        self.assertEqual(fake_save.call_args.args[1], 5_184_000)
        self.assertEqual(r.json()['days'], 60)

    def test_respect_custom_expires_in_from_facebook(self):
        with patch('video_management.services.facebook_token_store.save_token') as fake_save:
            self._post({'access_token': TOKEN, 'expires_in': 86_400})

        self.assertEqual(fake_save.call_args.args[1], 86_400)

    def test_reject_empty_token_and_do_not_overwrite(self):
        """Reject empty token to protect active token."""
        with patch('video_management.services.facebook_token_store.save_token') as fake_save:
            r = self._post({'access_token': '   '})

        self.assertEqual(r.status_code, 400)
        fake_save.assert_not_called()

    def test_unauthenticated_request_is_rejected(self):
        """Require authentication to protect system token."""
        r = Client().post(
            '/api/facebook/fetch/token-save/',
            data={'access_token': TOKEN},
            content_type='application/json',
        )
        self.assertIn(r.status_code, (401, 403))
