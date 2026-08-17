"""Content dài không còn bị chặn cứng 3000 ký tự ở tầng view PAAST.

Trước đây `analyze_content`/`analyze_content_v2` tự chặn content > MAX_CONTENT_LENGTH=3000
(bb0cd90, e55ac15). Bước viết kịch bản của content-transform chạy với max_tokens=16000 nên
output vượt 3000 ký tự là chuyện thường — endpoint /rescore truyền thẳng output_text vào đây
và ăn 400 CHẮC CHẮN xảy ra, trong khi BE vẫn retry đủ 3 lượt rồi thay message thật bằng
"có thể do timeout" (xem comment ở đầu paast_analysis_views.py). Test này khoá lại: content
dài bao nhiêu cũng phải đi tới service, không được view tự chặn.

Chạy: python manage.py test tests.test_paast_no_max_length_gate
"""

from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from video_management.views import paast_analysis_views


LONG_CONTENT = ("Nội dung kịch bản dài. " * 300).strip()  # ~6.900 ký tự, vượt xa mốc 3000 cũ


class NoMaxLengthGateTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def test_analyze_content_khong_chan_content_tren_3000_ky_tu(self):
        self.assertGreater(len(LONG_CONTENT), 3000)
        request = self.factory.post('/api/ai/paast/analyze/', {'content': LONG_CONTENT}, format='json')

        with patch.object(paast_analysis_views, 'PaastAnalysisService') as ServiceCls:
            ServiceCls.return_value.analyze.return_value = {'total_score': 80}
            response = paast_analysis_views.analyze_content(request)

        self.assertEqual(response.status_code, 200)
        ServiceCls.return_value.analyze.assert_called_once()
        called_content = ServiceCls.return_value.analyze.call_args.args[0]
        self.assertEqual(len(called_content), len(LONG_CONTENT))

    def test_analyze_content_v2_khong_chan_content_tren_3000_ky_tu(self):
        request = self.factory.post('/api/ai/paast/analyze-v2/', {'content': LONG_CONTENT}, format='json')

        with patch.object(paast_analysis_views, 'PaastAnalysisServiceV2') as ServiceCls:
            ServiceCls.return_value.analyze_v2.return_value = {'layers': {}}
            response = paast_analysis_views.analyze_content_v2(request)

        self.assertEqual(response.status_code, 200)
        ServiceCls.return_value.analyze_v2.assert_called_once_with(LONG_CONTENT)

    def test_analyze_content_van_giu_chan_duoi_MIN_CONTENT_LENGTH(self):
        """Chỉ bỏ trần trên — sàn dưới (100 ký tự) vẫn phải còn, tránh chấm content rỗng/vô nghĩa."""
        request = self.factory.post('/api/ai/paast/analyze/', {'content': 'quá ngắn'}, format='json')

        response = paast_analysis_views.analyze_content(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn('quá ngắn', str(response.data['error']))

    def test_analyze_content_v2_van_giu_chan_duoi_MIN_CONTENT_LENGTH(self):
        request = self.factory.post('/api/ai/paast/analyze-v2/', {'content': 'ngắn'}, format='json')

        response = paast_analysis_views.analyze_content_v2(request)

        self.assertEqual(response.status_code, 400)

    def test_module_khong_con_dinh_nghia_MAX_CONTENT_LENGTH(self):
        """Chốt luôn cả hằng số — tránh ai đó lỡ tay khai báo lại rồi quên gắn vào view."""
        self.assertFalse(hasattr(paast_analysis_views, 'MAX_CONTENT_LENGTH'))
