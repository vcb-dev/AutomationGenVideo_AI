"""Danh sách metric lấy lượt xem chỉ được chứa metric Facebook còn nhận.

Gốc sự cố 27/07–09/08/2026, đã tái hiện được bằng page token thật ngày 09/08/2026:

    post_video_views                 ✅ sống, trả 486 / 902 / 649 / 98 / 585 trên 5 bài
    post_video_reels_organic_plays   ❌ (#100) The value must be a valid insights metric

Code cũ gọi CẢ HAI trong một request `insights.metric(a,b)`. Facebook từ chối nguyên request
khi có một metric không hợp lệ, nên `post_video_views` — vốn vẫn chạy tốt — cũng không lấy được.
Lượt xem thật vẫn nằm sẵn ở Facebook suốt thời gian đó, chỉ bị một metric chết chặn đường.

Bài học đóng lại thành test: gộp nhiều metric vào một request nghĩa là một metric bị khai tử
kéo sập tất cả. Test này chốt danh sách metric để lần sau ai thêm vào phải cân nhắc.

Chạy: python manage.py test tests.test_facebook_view_metric_names
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from video_management.services.facebook_graph_service import (
    VIEW_METRICS,
    FacebookGraphService,
)

POST = '111_222'

ENGAGEMENT_OK = {
    POST: {
        'reactions': {'summary': {'total_count': 19}},
        'comments': {'summary': {'total_count': 2}},
        'shares': {'count': 1},
    }
}


def _ok(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _service():
    with patch(
        'video_management.services.facebook_graph_service.get_token', return_value='tok'
    ):
        return FacebookGraphService()


class ViewMetricNamesTests(SimpleTestCase):
    def test_khong_con_metric_reels_da_bi_khai_tu(self):
        """Đo được ngày 09/08/2026: Facebook trả (#100) cho metric này."""
        self.assertNotIn('post_video_reels_organic_plays', VIEW_METRICS)

    def test_van_giu_post_video_views(self):
        """Metric này vẫn sống — kiểm chứng bằng page token thật trên 5 bài."""
        self.assertIn('post_video_views', VIEW_METRICS)

    def test_khong_rong(self):
        self.assertTrue(VIEW_METRICS, 'Rỗng thì mọi bài đều thành view=0')

    def test_request_gui_dung_metric_trong_hang_so(self):
        """Đổi VIEW_METRICS phải đổi luôn request thật — không được gõ cứng ở hai nơi."""
        service = _service()
        with patch(
            'video_management.services.facebook_graph_service.requests.get'
        ) as mock_get:
            mock_get.side_effect = [_ok(ENGAGEMENT_OK), _ok({POST: {}})]
            service.update_video_views_batch([POST])

            fields = mock_get.call_args_list[1].kwargs['params']['fields']

        for metric in VIEW_METRICS:
            self.assertIn(metric, fields)
        self.assertNotIn('post_video_reels_organic_plays', fields)

    def test_cong_don_moi_metric_tra_ve(self):
        """Nhiều metric thì cộng dồn — giữ nguyên hành vi cũ để sau này thêm metric vẫn đúng."""
        service = _service()
        insights = {
            POST: {
                'insights': {
                    'data': [{'name': m, 'values': [{'value': 100}]} for m in VIEW_METRICS]
                }
            }
        }
        with patch(
            'video_management.services.facebook_graph_service.requests.get'
        ) as mock_get:
            mock_get.side_effect = [_ok(ENGAGEMENT_OK), _ok(insights)]
            result = service.update_video_views_batch([POST])

        self.assertEqual(result[POST]['view_count'], 100 * len(VIEW_METRICS))

    def test_bo_qua_metric_la_khong_nam_trong_hang_so(self):
        """Facebook trả thêm metric khác thì không được cộng nhầm vào lượt xem."""
        service = _service()
        insights = {
            POST: {
                'insights': {
                    'data': [
                        {'name': 'post_video_views', 'values': [{'value': 486}]},
                        {'name': 'post_clicks', 'values': [{'value': 9}]},
                    ]
                }
            }
        }
        with patch(
            'video_management.services.facebook_graph_service.requests.get'
        ) as mock_get:
            mock_get.side_effect = [_ok(ENGAGEMENT_OK), _ok(insights)]
            result = service.update_video_views_batch([POST])

        self.assertEqual(result[POST]['view_count'], 486)
