"""Danh sách giọng nói (GET /api/voice/list/) chỉ được trả những gì CÓ THẬT trong DB.

Endpoint này từng có một "fallback" bịa ra giọng HuyK khi bảng Voice rỗng: một
bản ghi id=-1, voice_id gõ cứng, is_cloned=True. Không ai dùng được nó — FE lọc
giọng hệ thống ra khỏi thư mục, TTS thì từ chối vì provider là heygen — nhưng
trang Tổng quan Tiện ích AI lại đếm `voices.filter(is_cloned)` nên báo "1 giọng
đã clone" trong khi thực tế không có giọng nào. Test này khoá lại: DB rỗng thì
danh sách rỗng.

Chạy: python manage.py test tests.test_voice_list
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import voice_views


def _voice(**over):
    defaults = dict(
        id=7,
        voice_id='KOC_Lan_a1b2c3d4',
        name='KOC Lan',
        language='vi',
        gender='female',
        provider='minimax',
        is_cloned=True,
        is_system=False,
        sample_audio_url=None,
    )
    defaults.update(over)
    v = MagicMock()
    for k, val in defaults.items():
        setattr(v, k, val)
    return v


class VoiceList(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _call(self):
        return voice_views.list_voices_api(self.factory.get('/api/voice/list/'))

    def test_empty_db_returns_empty_list(self):
        """Không bịa giọng: trang Tổng quan đếm is_cloned nên một giọng ma = số liệu sai."""
        with patch.object(voice_views, 'Voice') as Voice:
            Voice.objects.all.return_value = []
            res = self._call()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['voices'], [])
        self.assertEqual(res.data['count'], 0)

    def test_returns_fields_the_FE_relies_on(self):
        """FE lọc theo is_cloned/is_system/provider — thiếu trường nào là lọc sai."""
        with patch.object(voice_views, 'Voice') as Voice:
            Voice.objects.all.return_value = [_voice()]
            res = self._call()

        self.assertEqual(res.data['count'], 1)
        self.assertEqual(res.data['voices'][0], {
            'id': 7,
            'voice_id': 'KOC_Lan_a1b2c3d4',
            'name': 'KOC Lan',
            'language': 'vi',
            'gender': 'female',
            'provider': 'minimax',
            'is_cloned': True,
            'is_system': False,
            'sample_audio_url': None,
        })

    def test_missing_gender_defaults_to_female(self):
        """Bản ghi cũ không ghi cột gender — FE hiển thị thẳng chuỗi này dưới tên giọng."""
        with patch.object(voice_views, 'Voice') as Voice:
            Voice.objects.all.return_value = [_voice(gender=None)]
            res = self._call()

        self.assertEqual(res.data['voices'][0]['gender'], 'female')
