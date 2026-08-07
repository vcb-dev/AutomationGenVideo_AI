"""Tự gia hạn User Access Token của Facebook.

Facebook KHÔNG có refresh_token: cách duy nhất giữ token sống là gọi
`fb_exchange_token` khi token CÒN hạn. Token đã chết (code 190) thì không cứu được
bằng code — phải người thật đăng nhập lại. Test này khoá lại đúng ranh giới đó:
gia hạn được thì ghi đè, không gia hạn được thì GIỮ token cũ (ghi đè bằng chuỗi
rỗng là mất luôn manh mối để chẩn đoán).
"""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from video_management.services import facebook_token_store as store


def _hoi_dap(body, status=200):
    class R:
        status_code = status
        ok = status == 200
        text = json.dumps(body)

        def json(self):
            return body

    return R()


class FacebookTokenStoreTest(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(store, 'STATE_PATH', Path(self._tmp.name) / '.fb_token.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_doc_token_tu_state_file_khi_da_tung_gia_han(self):
        store.save_token('EAA_moi', expires_in=5_184_000)

        self.assertEqual(store.get_token(), 'EAA_moi')

    def test_chua_co_state_file_thi_lay_token_goc_trong_env(self):
        with patch.object(store.settings, 'FACEBOOK_ACCESS_TOKEN', 'EAA_env', create=True):
            self.assertEqual(store.get_token(), 'EAA_env')

    def test_gia_han_thanh_cong_thi_luu_token_moi(self):
        store.save_token('EAA_sap_het', expires_in=3 * 86_400)

        with patch.object(store, 'requests') as req:
            req.get.return_value = _hoi_dap({'access_token': 'EAA_vua_gia_han', 'expires_in': 5_184_000})
            ket_qua = store.refresh_user_token()

        self.assertEqual(ket_qua['status'], 'refreshed')
        self.assertEqual(store.get_token(), 'EAA_vua_gia_han')

    def test_token_con_xa_han_thi_khong_goi_facebook(self):
        store.save_token('EAA_con_lau_moi_het', expires_in=50 * 86_400)

        with patch.object(store, 'requests') as req:
            ket_qua = store.refresh_user_token()

        self.assertEqual(ket_qua['status'], 'ok')
        req.get.assert_not_called()

    def test_graph_service_dung_ngay_token_vua_gia_han(self):
        """Không có bước này thì gia hạn xong vẫn cào bằng token cũ tới lần restart Django."""
        from video_management.services.facebook_graph_service import FacebookGraphService

        store.save_token('EAA_vua_gia_han', expires_in=5_184_000)

        self.assertEqual(FacebookGraphService().access_token, 'EAA_vua_gia_han')

    def test_token_da_chet_thi_giu_nguyen_token_cu(self):
        store.save_token('EAA_da_chet', expires_in=3 * 86_400)

        loi = {'error': {'message': 'The session is invalid because the user logged out.', 'code': 190}}
        with patch.object(store, 'requests') as req:
            req.get.return_value = _hoi_dap(loi, status=400)
            ket_qua = store.refresh_user_token()

        self.assertEqual(ket_qua['status'], 'invalid')
        self.assertEqual(store.get_token(), 'EAA_da_chet')


class FacebookTokenRefreshEndpointTest(SimpleTestCase):
    """BE không gọi được hàm Python, nên phải có endpoint cho cron bên BE gọi sang."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(store, 'STATE_PATH', Path(self._tmp.name) / '.fb_token.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_endpoint_tra_ve_ket_qua_gia_han(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from video_management.views.facebook_fetch_views import fetch_token_refresh

        store.save_token('EAA_sap_het', expires_in=3 * 86_400)

        request = APIRequestFactory().post('/api/facebook/fetch/token-refresh/', {}, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1))

        with patch.object(store, 'requests') as req:
            req.get.return_value = _hoi_dap({'access_token': 'EAA_moi', 'expires_in': 5_184_000})
            response = fetch_token_refresh(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'refreshed')
