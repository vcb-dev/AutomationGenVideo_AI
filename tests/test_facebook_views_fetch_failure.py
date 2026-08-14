"""Không lấy được lượt xem thì phải trả None, TUYỆT ĐỐI không trả 0.

Vì sao đây là lỗi đáng sửa nhất trong sự cố 27/07–09/08/2026: request insights hỏng, code
cũ nuốt lỗi bằng logger.warning rồi ghi `view_count = 0` vào DB. Hậu quả là 13 ngày liền
dashboard vẽ đường lượt xem tụt về 0 mà trông y như dữ liệu thật — 1.169 video ngày 01→09/08
đều mang view_count = 0 trong khi like/comment/share vẫn về bình thường. Không ai báo động
vì bảng vẫn đầy số.

Ghi 0 khi KHÔNG LẤY ĐƯỢC là nói dối: nó không phân biệt được với 0 nghĩa là THẬT SỰ không
ai xem. Trả None thì phía BE (facebook-owned-pages.service.ts) đã có sẵn nhánh
`m.view_count ?? Number(v.view_count)` để giữ nguyên giá trị cũ — nhánh đó viết ra chính là
để dùng cho tình huống này, chỉ chưa bao giờ chạy được vì Python luôn gửi 0.

Chạy: python manage.py test tests.test_facebook_views_fetch_failure
"""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from video_management.services.facebook_graph_service import FacebookGraphService


POST_A = '111_222'
POST_B = '333_444'


def _ok_response(payload):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _http_error(status=400, body=''):
    """Phản hồi lỗi giống requests thật: raise_for_status() ném HTTPError có .response."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    err = requests.exceptions.HTTPError(f'{status} Client Error')
    err.response = resp
    resp.raise_for_status.side_effect = err
    return resp


# Request 1 (reactions/comments/shares) luôn thành công trong mọi test dưới đây — đúng như
# production: chỉ request insights hỏng, phần còn lại vẫn chạy.
ENGAGEMENT_OK = {
    POST_A: {
        'reactions': {'summary': {'total_count': 12}},
        'comments': {'summary': {'total_count': 3}},
        'shares': {'count': 5},
    },
    POST_B: {
        'reactions': {'summary': {'total_count': 7}},
        'comments': {'summary': {'total_count': 0}},
        'shares': {'count': 1},
    },
}


def _build_service():
    # Xem chú thích cùng loại ở test_facebook_view_metric_names: thiếu app id/secret thì
    # __init__ ném ValueError, test đỏ trên CI dù logic đang đúng.
    with override_settings(
        FACEBOOK_APP_ID='test-app-id', FACEBOOK_APP_SECRET='test-app-secret'
    ), patch(
        'video_management.services.facebook_graph_service.get_token',
        return_value='tok',
    ):
        return FacebookGraphService()


class ViewsFetchFailureTests(SimpleTestCase):
    def test_insights_hong_thi_view_count_la_None_chu_khong_phai_0(self):
        """Đây là điều kiện cốt lõi: hỏng ≠ bằng 0."""
        service = _build_service()

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [
                _ok_response(ENGAGEMENT_OK),
                _http_error(400, '{"error":{"message":"metric deprecated","code":100}}'),
            ]
            result = service.update_video_views_batch([POST_A, POST_B])

        self.assertIsNone(result[POST_A]['view_count'])
        self.assertIsNone(result[POST_B]['view_count'])

    def test_insights_hong_van_giu_duoc_like_comment_share(self):
        """Lỗi ở bước views không được phép làm mất số liệu đã lấy được ở bước 1."""
        service = _build_service()

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [
                _ok_response(ENGAGEMENT_OK),
                _http_error(400, '{"error":{"message":"metric deprecated"}}'),
            ]
            result = service.update_video_views_batch([POST_A, POST_B])

        self.assertEqual(result[POST_A]['like_count'], 12)
        self.assertEqual(result[POST_A]['comment_count'], 3)
        self.assertEqual(result[POST_A]['share_count'], 5)

    def test_insights_hong_thi_ghi_log_muc_ERROR_kem_nguyen_van_body(self):
        """warning bị chôn trong log. Sự cố vừa rồi âm thầm 13 ngày chính vì mức log quá nhẹ,
        và body của Facebook là thứ duy nhất nói được vì sao hỏng."""
        service = _build_service()
        body = '{"error":{"message":"(#100) post_video_views is deprecated","code":100}}'

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [_ok_response(ENGAGEMENT_OK), _http_error(400, body)]
            with self.assertLogs(
                'video_management.services.facebook_graph_service', level='ERROR'
            ) as logs:
                service.update_video_views_batch([POST_A, POST_B])

        duoc_ghi = '\n'.join(logs.output)
        self.assertIn('post_video_views is deprecated', duoc_ghi)

    def test_lay_duoc_thi_tra_dung_so(self):
        """Đường thành công vẫn phải trả đúng số.

        Trước đây test này cộng post_video_views + post_video_reels_organic_plays = 1234. Metric
        Reels đã bị Facebook khai tử (đo ngày 09/08/2026) và bị loại khỏi VIEW_METRICS, nên nếu
        nó còn lọt vào phản hồi thì cũng KHÔNG được cộng — xem test_facebook_view_metric_names.
        """
        service = _build_service()
        insights = {
            POST_A: {
                'insights': {
                    'data': [
                        {'name': 'post_video_views', 'values': [{'value': 1000}]},
                        {'name': 'post_video_reels_organic_plays', 'values': [{'value': 234}]},
                    ]
                }
            },
            POST_B: {'insights': {'data': [{'name': 'post_video_views', 'values': [{'value': 50}]}]}},
        }

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [_ok_response(ENGAGEMENT_OK), _ok_response(insights)]
            result = service.update_video_views_batch([POST_A, POST_B])

        self.assertEqual(result[POST_A]['view_count'], 1000)
        self.assertEqual(result[POST_B]['view_count'], 50)

    def test_bai_khong_phai_video_van_la_0_chu_khong_phai_None(self):
        """Phân biệt quan trọng: request THÀNH CÔNG mà bài không có khối insights nghĩa là bài
        đó thật sự không có video để đếm view — 0 là câu trả lời đúng, không phải 'không biết'."""
        service = _build_service()

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [
                _ok_response(ENGAGEMENT_OK),
                _ok_response({POST_A: {}, POST_B: {}}),  # thành công, nhưng không có insights
            ]
            result = service.update_video_views_batch([POST_A, POST_B])

        self.assertEqual(result[POST_A]['view_count'], 0)
        self.assertEqual(result[POST_B]['view_count'], 0)

    def test_request_dau_hong_thi_tra_dict_rong(self):
        """Giữ nguyên hành vi cũ: mất cả reactions/comments/shares thì không có gì để trả."""
        service = _build_service()

        with patch('video_management.services.facebook_graph_service.requests.get') as mock_get:
            mock_get.side_effect = [_http_error(500, 'boom')]
            result = service.update_video_views_batch([POST_A, POST_B])

        self.assertEqual(result, {})
