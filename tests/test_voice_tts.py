"""POST /api/voice/tts/ — view phải gọi service bằng đúng bộ tham số service nhận.

Vì sao có file này: ngày 10/09/2026 toàn bộ TTS trên production trả 500 với

    MinimaxTTSService.generate_audio() got an unexpected keyword argument 'model'

Commit thêm `model=model` vào chỗ gọi đã lên main, còn commit thêm tham số `model`
vào chữ ký `generate_audio()` thì nằm ở nhánh khác chưa merge. Lỗi ném ra sau 0.12s,
MiniMax chưa hề được gọi — nghĩa là không có mã lỗi nào của nhà cung cấp để lần theo,
người vận hành chỉ thấy "500" và đi kiểm tra nhầm sang credit MiniMax.

Không test nào chạm tới `voice_tts_api` nên CI vẫn xanh; lỗi chỉ lộ khi người dùng bấm nút.

ĐIỂM MẤU CHỐT: KHÔNG mock `generate_audio`. Mock nó bằng MagicMock thì mọi kwarg đều
lọt và đúng loại bug này bị giấu lại lần nữa. Chỉ chặn ở tầng `requests.post` ra ngoài
MiniMax, còn việc khớp tham số giữa view và service để cho Python thật kiểm.

Chạy: python manage.py test tests.test_voice_tts
"""

import base64
import inspect
import tempfile
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.services import minimax_tts_service
from video_management.services.minimax_tts_service import MinimaxTTSService
from video_management.views import voice_views

# Nội dung không quan trọng, chỉ cần decode được từ hex rồi ghi ra file như mp3 thật.
FAKE_MP3 = b'ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-bytes'

# Bộ tham số view đang gửi. Thêm/đổi tham số ở view thì phải sửa cả đây lẫn chữ ký
# service — đó chính là điều bug 10/09/2026 bỏ sót.
VIEW_KWARGS = (
    'text', 'voice_id', 'speed', 'vol', 'pitch',
    'emotion', 'language_boost', 'output_path', 'model',
)


def _minimax_ok_response():
    """Khuôn phản hồi thật của MiniMax t2a_v2: audio là chuỗi hex nằm trong data.audio."""
    res = MagicMock()
    res.status_code = 200
    res.json.return_value = {
        'data': {'audio': FAKE_MP3.hex(), 'status': 2},
        'extra_info': {'audio_length': 1645, 'usage_characters': 15},
        'trace_id': 'test-trace-id',
        'base_resp': {'status_code': 0, 'status_msg': 'success'},
    }
    return res


class VoiceTts(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)

    def _call(self, **overrides):
        """Gọi view với đúng payload BE gửi. Trả về (response, mock của requests.post)."""
        payload = {
            'text': 'huyk test voice',
            'voice_id': 'HuyK_8a4ca57b',
            'speed': 1,
            'pitch': 0,
            'volume': 100,
            'language': 'Vietnamese',
            'emotion': 'calm',
        }
        payload.update(overrides)
        request = self.factory.post(
            '/api/voice/tts/', payload, format='json',
            HTTP_X_MINIMAX_KEY='sk-api-test-key',
        )

        storage = MagicMock()
        storage.location = self.media_dir.name

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'default_storage', storage), \
                patch.object(minimax_tts_service.requests, 'post',
                             return_value=_minimax_ok_response()) as post:
            Voice.objects.filter.return_value.first.return_value = None
            response = voice_views.voice_tts_api(request)

        return response, post

    def test_service_signature_accepts_every_kwarg_the_view_passes(self):
        """Chốt hợp đồng view↔service. Đây là test vỡ trước nhất khi tái diễn bug cũ,
        và câu báo lỗi của nó chỉ thẳng tham số nào lệch."""
        params = inspect.signature(MinimaxTTSService.generate_audio).parameters
        for kwarg in VIEW_KWARGS:
            self.assertIn(
                kwarg, params,
                f"voice_tts_api gửi '{kwarg}=' nhưng generate_audio() không nhận — "
                f"mọi request TTS sẽ trả 500 TypeError trước khi kịp gọi MiniMax",
            )

    def test_tts_returns_audio_for_the_payload_BE_sends(self):
        """Đường đi đủ: BE → view → service → MiniMax. Lệch một kwarg là 500 ở đây."""
        response, _ = self._call()

        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        self.assertTrue(response.data['success'])
        # BE upload thẳng bytes này lên Drive — rỗng thì FE không phát được gì.
        self.assertEqual(base64.b64decode(response.data['audio_base64']), FAKE_MP3)
        self.assertEqual(response.data['usage_characters'], 15)

    def test_model_override_reaches_the_minimax_payload(self):
        """Tham số `model` phải thực sự đi tới MiniMax, không chỉ được nhận rồi bỏ quên."""
        _, post = self._call(model='speech-2.8-turbo')

        self.assertEqual(post.call_args.kwargs['json']['model'], 'speech-2.8-turbo')

    def test_default_model_used_when_request_omits_model(self):
        """BE không gửi `model` — phải rơi về MINIMAX_TTS_MODEL chứ không phải None."""
        _, post = self._call()

        self.assertEqual(post.call_args.kwargs['json']['model'], settings.MINIMAX_TTS_MODEL)
