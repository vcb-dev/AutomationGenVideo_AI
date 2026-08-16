"""Kéo Instagram nội bộ qua Graph API — miễn phí, thay đường TikHub tính tiền.

── Vì sao làm được ─────────────────────────────────────────────────────────────
Instagram Business nối với Facebook Page thì đọc được bằng CHÍNH page token đang dùng cho
Facebook, không cần khoá mới. Đo thật 16/08/2026 trên 25 fanpage nhiều follower nhất:
14 page có `instagram_business_account`, gọi `/{ig-id}/media` trả HTTP 200 kèm loại bài
(REELS/FEED), thời gian đăng, caption, like, comment, permalink.

── Chỗ chưa lấy được: lượt xem ─────────────────────────────────────────────────
`/{media-id}/insights?metric=views` trả `(#10) Application does not have permission`. Đã thử
cả 4 đường không cần quyền mới — `fields=video_views`, `fields=views`, `fields=play_count`,
insight ở mức tài khoản — Graph API đều báo field không tồn tại. Meta chỉ cho lượt xem qua
`insights`, mà `insights` đòi quyền `instagram_manage_insights`.

Nên phần insight KHÔNG được để thiếu quyền làm hỏng cả lượt: media vẫn về đủ, `view_count`
trả None và phía BE giữ nguyên số cũ (cùng luật với resolve-view-count.ts của Facebook —
None nghĩa là "không lấy được", KHÁC 0 nghĩa là "thật sự không ai xem"). Ngày token được cấp
lại kèm quyền, cùng đoạn code đó tự có số mà không phải sửa gì.

Chạy: python manage.py test tests.test_instagram_graph_service
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from video_management.services.instagram_graph_service import (
    InstagramGraphService,
    extract_hashtags,
    extract_shortcode,
)

IG_ID = '17841400000000000'
TOKEN = 'page-token-gia'


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    return r


class ExtractHelperTests(SimpleTestCase):
    """Hai hàm thuần — bảng ScraperInstagramReel có cột shortcode và hashtags[] bắt buộc."""

    def test_lay_shortcode_tu_permalink(self):
        self.assertEqual(extract_shortcode('https://www.instagram.com/reel/DAbc123xyz/'), 'DAbc123xyz')
        self.assertEqual(extract_shortcode('https://www.instagram.com/p/C9xYz-1aB2c/'), 'C9xYz-1aB2c')

    def test_permalink_la_khong_doc_duoc_thi_tra_rong_chu_khong_no(self):
        self.assertEqual(extract_shortcode(''), '')
        self.assertEqual(extract_shortcode('https://instagram.com/'), '')

    def test_lay_hashtag_tu_caption(self):
        self.assertEqual(
            extract_hashtags('Nhẫn kim hoa #N0036 #K101 #a4'),
            ['N0036', 'K101', 'a4'],
        )

    def test_hashtag_co_dau_tieng_viet_van_lay_duoc(self):
        # Caption kênh nội bộ hay có #vàng, #bạc — bỏ sót là mất hẳn nhóm hashtag tiếng Việt.
        self.assertEqual(extract_hashtags('Dành hơi nhiều tâm sức #vàng #bạc925'), ['vàng', 'bạc925'])

    def test_khong_co_hashtag_thi_tra_danh_sach_rong(self):
        self.assertEqual(extract_hashtags('Không có thẻ nào'), [])
        self.assertEqual(extract_hashtags(''), [])


class FetchOwnedAccountsTests(SimpleTestCase):
    def test_tra_ve_tai_khoan_ig_gan_voi_page(self):
        payload = {
            'instagram_business_account': {
                'id': IG_ID,
                'username': 'huyk_xuongchetac',
                'followers_count': 5812,
                'media_count': 3493,
                'profile_picture_url': 'https://cdn/avatar.jpg',
            }
        }
        with patch('requests.get', return_value=_resp(200, payload)):
            acc = InstagramGraphService().fetch_owned_account('12345', TOKEN)

        self.assertEqual(acc['instagram_id'], IG_ID)
        self.assertEqual(acc['username'], 'huyk_xuongchetac')
        self.assertEqual(acc['followers_count'], 5812)

    def test_page_khong_noi_instagram_thi_tra_none_chu_khong_no(self):
        """11/25 page không nối IG — đây là chuyện bình thường, không phải lỗi."""
        with patch('requests.get', return_value=_resp(200, {'id': '12345'})):
            self.assertIsNone(InstagramGraphService().fetch_owned_account('12345', TOKEN))

    def test_graph_tra_loi_thi_tra_none(self):
        with patch('requests.get', return_value=_resp(400, {'error': {'message': 'token hỏng'}})):
            self.assertIsNone(InstagramGraphService().fetch_owned_account('12345', TOKEN))


class FetchMediaTests(SimpleTestCase):
    MEDIA = {
        'data': [
            {
                'id': '17900000000000001',
                'media_type': 'VIDEO',
                'media_product_type': 'REELS',
                'permalink': 'https://www.instagram.com/reel/DAbc123xyz/',
                'timestamp': '2026-08-15T03:00:00+0000',
                'caption': 'Rơi mất là sang chấn tâm lý luôn #K101 #a4',
                'like_count': 12,
                'comments_count': 3,
                'thumbnail_url': 'https://cdn/thumb.jpg',
            }
        ]
    }

    def test_parse_du_truong_cho_bang_ScraperInstagramReel(self):
        with patch('requests.get', return_value=_resp(200, self.MEDIA)):
            items = InstagramGraphService().fetch_media(IG_ID, TOKEN, limit=25)

        m = items[0]
        self.assertEqual(m['post_id'], '17900000000000001')
        self.assertEqual(m['shortcode'], 'DAbc123xyz')
        self.assertEqual(m['likes_count'], 12)
        self.assertEqual(m['comments_count'], 3)
        self.assertEqual(m['hashtags'], ['K101', 'a4'])
        self.assertTrue(m['date_posted'].startswith('2026-08-15'))

    def test_thieu_quyen_insight_thi_view_la_None_chu_khong_phai_0(self):
        """Điểm mấu chốt. 0 nghĩa là "không ai xem" — ghi 0 lên video đang có view thật là
        đúng sự cố 27/07–09/08/2026 của Facebook. Không lấy được thì phải nói là KHÔNG BIẾT."""
        def _gia(url, **kwargs):
            if '/insights' in url:
                return _resp(400, {'error': {'code': 10, 'message': 'Application does not have permission'}})
            return _resp(200, self.MEDIA)

        with patch('requests.get', side_effect=_gia):
            items = InstagramGraphService().fetch_media(IG_ID, TOKEN, limit=25)

        self.assertIsNone(items[0]['view_count'])

    def test_co_quyen_insight_thi_lay_duoc_view(self):
        def _gia(url, **kwargs):
            if '/insights' in url:
                return _resp(200, {'data': [{'name': 'views', 'values': [{'value': 40790}]}]})
            return _resp(200, self.MEDIA)

        with patch('requests.get', side_effect=_gia):
            items = InstagramGraphService().fetch_media(IG_ID, TOKEN, limit=25)

        self.assertEqual(items[0]['view_count'], 40790)

    def test_bai_anh_khong_goi_insight_video(self):
        """Ảnh không có lượt xem — gọi insight video cho nó chỉ tốn một lượt và nhận lỗi."""
        anh = {'data': [dict(self.MEDIA['data'][0], media_type='IMAGE', media_product_type='FEED')]}
        goi = []

        def _gia(url, **kwargs):
            goi.append(url)
            return _resp(200, anh)

        with patch('requests.get', side_effect=_gia):
            InstagramGraphService().fetch_media(IG_ID, TOKEN, limit=25)

        self.assertEqual([u for u in goi if '/insights' in u], [])
