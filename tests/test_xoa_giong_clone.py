"""Xoá giọng đã clone: MiniMax trước, DB sau.

Thứ tự đó không phải chi tiết vặt. MiniMax lỗi mà đã xoá bản ghi DB thì giọng
thành mồ côi: vẫn chiếm slot bên MiniMax nhưng không còn chỗ nào trong hệ thống
để xoá nó — muốn dọn phải vào tận trang MiniMax. Test này khoá lại đúng thứ tự đó,
cộng với hai chốt chặn: không xoá giọng hệ thống, và "giọng không còn trên MiniMax"
không phải lỗi.

Chạy: python manage.py test tests.test_xoa_giong_clone
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import voice_views


def _hoi_dap(body, status=200):
    class R:
        status_code = status
        text = str(body)

        def json(self):
            return body

    return R()


def _giong(name='KOC Lan', voice_id='KOC_Lan_a1b2c3d4', provider='minimax', is_cloned=True, is_system=False):
    v = MagicMock()
    v.name = name
    v.voice_id = voice_id
    v.provider = provider
    v.is_cloned = is_cloned
    v.is_system = is_system
    return v


class DeleteVoiceService(SimpleTestCase):
    """Tầng gọi API MiniMax."""

    def _service(self):
        from video_management.services.minimax_voice_clone_service import MinimaxVoiceCloneService
        return MinimaxVoiceCloneService(api_key='sk-api-test')

    def test_goi_dung_endpoint_va_payload(self):
        svc = self._service()
        with patch('video_management.services.minimax_voice_clone_service.requests.post',
                   return_value=_hoi_dap({'base_resp': {'status_code': 0, 'status_msg': 'success'}})) as goi:
            ket_qua = svc.delete_voice('KOC_Lan_a1b2c3d4')

        self.assertEqual(ket_qua, {'deleted': True})
        url = goi.call_args[0][0]
        self.assertTrue(url.endswith('/v1/delete_voice'), f'Gọi nhầm endpoint: {url}')
        # voice_type BẮT BUỘC phải là voice_cloning — thiếu/sai thì MiniMax báo invalid params
        self.assertEqual(goi.call_args[1]['json'],
                         {'voice_type': 'voice_cloning', 'voice_id': 'KOC_Lan_a1b2c3d4'})

    def test_giong_khong_ton_tai_khong_phai_loi(self):
        """MiniMax trả 2013 khi voice_id không còn (đã xoá tay / hết hạn 168h).

        Body dưới đây là body THẬT, đo ngày 2026-08-06 bằng cách gọi delete_voice
        trên một voice_id vừa bị xoá. Coi đó là lỗi thì bản ghi DB sẽ kẹt lại vĩnh
        viễn, người dùng không bao giờ dọn được giọng chết khỏi danh sách."""
        svc = self._service()
        that = {'voice_id': '', 'description': None, 'created_time': '',
                'base_resp': {'status_code': 2013, 'status_msg': 'invalid params, voice_id does not exist'}}
        with patch('video_management.services.minimax_voice_clone_service.requests.post',
                   return_value=_hoi_dap(that)):
            ket_qua = svc.delete_voice('da-bien-mat')

        self.assertFalse(ket_qua['deleted'])

    def test_loi_that_thi_raise(self):
        """Key sai / hết quyền (1004) phải raise — không được âm thầm cho qua rồi xoá DB."""
        svc = self._service()
        with patch('video_management.services.minimax_voice_clone_service.requests.post',
                   return_value=_hoi_dap({'base_resp': {'status_code': 1004, 'status_msg': 'token not match group'}})):
            with self.assertRaises(Exception) as ctx:
                svc.delete_voice('v1')

        self.assertIn('1004', str(ctx.exception))

    def test_http_khong_phai_200_thi_raise(self):
        svc = self._service()
        with patch('video_management.services.minimax_voice_clone_service.requests.post',
                   return_value=_hoi_dap({'msg': 'gateway'}, status=502)):
            with self.assertRaises(Exception) as ctx:
                svc.delete_voice('v1')

        self.assertIn('502', str(ctx.exception))


class DeleteVoiceApi(SimpleTestCase):
    """Tầng view: chốt chặn + thứ tự MiniMax-trước-DB."""

    def setUp(self):
        self.factory = APIRequestFactory()

    def _goi(self, voice_id='KOC_Lan_a1b2c3d4'):
        request = self.factory.delete(f'/api/voice/delete/{voice_id}/')
        return voice_views.delete_voice_api(request, voice_id)

    def test_khong_tim_thay_giong_tra_404(self):
        with patch.object(voice_views, 'Voice') as Voice:
            Voice.objects.filter.return_value.first.return_value = None
            res = self._goi('khong-co')

        self.assertEqual(res.status_code, 404)

    def test_tu_choi_xoa_giong_he_thong(self):
        """Giọng hệ thống (vd HuyK mặc định) dùng chung cho nhiều luồng khác."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service') as lay_svc:
            Voice.objects.filter.return_value.first.return_value = _giong(name='HuyK', is_system=True)
            res = self._goi('huyk')

        self.assertEqual(res.status_code, 400)
        lay_svc.assert_not_called()
        Voice.objects.filter.return_value.delete.assert_not_called()

    def test_xoa_thanh_cong_goi_minimax_roi_moi_xoa_db(self):
        thu_tu = []
        svc = MagicMock()
        svc.delete_voice.side_effect = lambda vid: thu_tu.append('minimax') or {'deleted': True}

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service', return_value=svc):
            Voice.objects.filter.return_value.first.return_value = _giong()
            Voice.objects.filter.return_value.delete.side_effect = lambda: thu_tu.append('db') or (1, {})
            res = self._goi()

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        self.assertTrue(res.data['minimax_deleted'])
        self.assertEqual(thu_tu, ['minimax', 'db'], 'Phải xoá trên MiniMax TRƯỚC khi xoá bản ghi DB.')

    def test_minimax_loi_thi_KHONG_xoa_ban_ghi_db(self):
        """Chốt chính của cả file: MiniMax lỗi → DB còn nguyên → người dùng bấm xoá lại được."""
        svc = MagicMock()
        svc.delete_voice.side_effect = Exception('Minimax API error (1004): token not match group')

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service', return_value=svc):
            Voice.objects.filter.return_value.first.return_value = _giong()
            res = self._goi()

        self.assertEqual(res.status_code, 500)
        Voice.objects.filter.return_value.delete.assert_not_called()

    def test_giong_da_bien_mat_ben_minimax_van_xoa_duoc_ban_ghi(self):
        svc = MagicMock()
        svc.delete_voice.return_value = {'deleted': False, 'reason': 'voice not found'}

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service', return_value=svc):
            Voice.objects.filter.return_value.first.return_value = _giong()
            Voice.objects.filter.return_value.delete.return_value = (1, {})
            res = self._goi()

        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['minimax_deleted'])
        Voice.objects.filter.return_value.delete.assert_called_once()

    def test_giong_provider_khac_khong_goi_api_minimax(self):
        """Giọng clone của HeyGen: delete_voice là API riêng của MiniMax, gọi sang là lỗi."""
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service') as lay_svc:
            Voice.objects.filter.return_value.first.return_value = _giong(provider='heygen')
            Voice.objects.filter.return_value.delete.return_value = (1, {})
            res = self._goi()

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data['minimax_deleted'])
        lay_svc.assert_not_called()

    def test_chuyen_tiep_key_minimax_tu_header(self):
        """Key MiniMax nằm ở .env của BE, gửi kèm từng request qua X-Minimax-Key."""
        svc = MagicMock()
        svc.delete_voice.return_value = {'deleted': True}

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service', return_value=svc) as lay_svc:
            Voice.objects.filter.return_value.first.return_value = _giong()
            Voice.objects.filter.return_value.delete.return_value = (1, {})
            request = self.factory.delete('/api/voice/delete/v1/', HTTP_X_MINIMAX_KEY='sk-api-abc')
            voice_views.delete_voice_api(request, 'v1')

        lay_svc.assert_called_once_with(api_key='sk-api-abc')
