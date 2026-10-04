"""Tìm bài Threads theo TAG CHỦ ĐỀ qua actor Apify — POST scraper/threads/fetch/search-tag/.

Threads chỉ cho xem bảng tin của tag khi đã đăng nhập, TikHub không có endpoint tag. Actor Apify
logical_scrapers/threads-hashtag-scraper với tag "trang sức" (kiểm chứng 2026-10-01): 19 bài, 7 bài
không có chữ "trang sức" trong nội dung, $0.0386 — tính phí theo bài nên luôn chặn maxItems.

Chạy: python manage.py test tests.test_threads_tag_search
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import apify_threads_tag
from video_management.services.apify_threads_tag import (
    ThreadsTagConfigError,
    fetch_tag_posts,
    normalize_tag,
    parse_tag_posts,
)
from video_management.views import threads_fetch_views

ACTOR = 'logical_scrapers/threads-hashtag-scraper'
APIFY_BASE = 'https://apify.test/'

# Item thật (rút gọn) từ actor Apify.
APIFY_ITEMS = [
    {
        'text': 'Ê ở Vn ko có shop nào bán kiểu lọ mù trang sức như', 'published_on': 1773936898,
        'id': '3856384667229985340_77894189185', 'pk': '3856384667229985340', 'code': 'DWEoWw_gZo8',
        'username': 'matnai_006', 'user_pic': 'https://cdn/avatar.jpg', 'user_verified': False,
        'reply_count': 73, 'like_count': 1543, 'images': ['https://cdn/a.jpg', 'https://cdn/b.jpg'],
        'image_count': 2, 'videos': [], 'url': 'https://www.threads.net/@matnai_006/post/DWEoWw_gZo8',
        'hashtag': 'trang sức',
    },
    {
        'text': 'Cơ địa hợp bạccc', 'published_on': 1788286629, 'id': '2_9', 'pk': '2', 'code': 'X2',
        'username': 'ph.anhhhhdayyy', 'user_pic': '', 'reply_count': 0, 'like_count': 2515,
        'images': [], 'image_count': 0, 'videos': [{'url': 'https://cdn/v.mp4'}],
        'url': 'https://www.threads.net/@ph.anhhhhdayyy/post/X2', 'hashtag': 'trang sức',
    },
    {
        'text': 'Mấy bà ơi tui muốn nhập trang sức bạc', 'published_on': 1780131894, 'id': '3_9', 'pk': '3',
        'code': 'X3', 'username': 'ticco_store', 'reply_count': 5, 'like_count': 3,
        'images': [], 'image_count': 0, 'videos': [], 'url': '', 'hashtag': 'trang sức',
    },
]


class NormalizeTagTests(SimpleTestCase):
    def test_normalize(self):
        """Bỏ #, gộp khoảng trắng, chữ thường."""
        self.assertEqual(normalize_tag('#Trang  Sức '), 'trang sức')
        self.assertEqual(normalize_tag(''), '')
        self.assertEqual(normalize_tag(None), '')


class FetchTagPostsTests(SimpleTestCase):
    @override_settings(APIFY_ACTOR_THREADS_TAG='', APIFY_API_TOKEN='tok', APIFY_API_BASE_URL=APIFY_BASE)
    def test_missing_actor_names_variable(self):
        """Thiếu APIFY_ACTOR_THREADS_TAG → lỗi nêu tên biến, không gọi Apify."""
        with patch.object(apify_threads_tag.requests, 'post') as post:
            with self.assertRaisesMessage(ThreadsTagConfigError, 'APIFY_ACTOR_THREADS_TAG'):
                fetch_tag_posts('trang sức', 20)
            post.assert_not_called()

    @override_settings(APIFY_ACTOR_THREADS_TAG=ACTOR, APIFY_API_TOKEN='', APIFY_API_BASE_URL=APIFY_BASE)
    def test_missing_token_names_variable(self):
        """Thiếu APIFY_API_TOKEN → lỗi nêu tên biến."""
        with self.assertRaisesMessage(ThreadsTagConfigError, 'APIFY_API_TOKEN'):
            fetch_tag_posts('trang sức', 20)

    @override_settings(APIFY_ACTOR_THREADS_TAG=ACTOR, APIFY_API_TOKEN='tok', APIFY_API_BASE_URL='')
    def test_missing_base_url_names_variable(self):
        """Gốc API Apify không gõ cứng — thiếu APIFY_API_BASE_URL thì báo tên biến."""
        with patch.object(apify_threads_tag.requests, 'post') as post:
            with self.assertRaisesMessage(ThreadsTagConfigError, 'APIFY_API_BASE_URL'):
                fetch_tag_posts('trang sức', 20)
            post.assert_not_called()

    @override_settings(APIFY_ACTOR_THREADS_TAG=ACTOR, APIFY_API_TOKEN='tok', APIFY_API_BASE_URL=APIFY_BASE)
    def test_caps_paid_items_with_max_items(self):
        """Actor tính phí theo bài → luôn chặn maxItems = số bài yêu cầu."""
        resp = MagicMock(ok=True)
        resp.json.return_value = APIFY_ITEMS
        with patch.object(apify_threads_tag.requests, 'post', return_value=resp) as post:
            items = fetch_tag_posts('trang sức', 2)

        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertEqual(url, 'https://apify.test/v2/acts/logical_scrapers~threads-hashtag-scraper/run-sync-get-dataset-items')
        self.assertEqual(kwargs['params']['maxItems'], 2)
        self.assertEqual(kwargs['json'], {'hashtags': ['trang sức'], 'maxPosts': 2})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok')
        self.assertNotIn('token', kwargs['params'])
        self.assertEqual(len(items), 2)


class ParseTagPostsTests(SimpleTestCase):
    def setUp(self):
        self.posts = {p['post_id']: p for p in parse_tag_posts(APIFY_ITEMS, 'trang sức')}

    def test_keeps_tagged_post_without_keyword(self):
        """Đúng ý người dùng: bài gắn tag mà nội dung không có chữ đó vẫn được lấy."""
        self.assertIn('2', self.posts)
        self.assertEqual(self.posts['2']['topic_tag'], 'trang sức')

    def test_maps_fields_to_shared_shape(self):
        """Item Apify → cùng dạng bài với parse_threads_posts."""
        p = self.posts['3856384667229985340']
        self.assertEqual(p['author_username'], 'matnai_006')
        self.assertEqual(p['likes_count'], 1543)
        self.assertEqual(p['replies_count'], 73)
        self.assertEqual(p['media_type'], 'CAROUSEL')
        self.assertEqual(p['thumbnail_url'], 'https://cdn/a.jpg')
        self.assertEqual(p['date_posted'][:10], '2026-03-19')
        self.assertEqual(p['url'], 'https://www.threads.net/@matnai_006/post/DWEoWw_gZo8')
        self.assertTrue(p['is_vietnamese'])

    def test_video_and_fallback_url(self):
        """Video dạng {url}, bài thiếu url thì dựng từ username + code."""
        self.assertEqual(self.posts['2']['media_type'], 'VIDEO')
        self.assertEqual(self.posts['2']['video_url'], 'https://cdn/v.mp4')
        self.assertEqual(self.posts['3']['url'], 'https://www.threads.net/@ticco_store/post/X3')
        self.assertEqual(self.posts['3']['media_type'], 'TEXT')

    def test_drops_items_without_id_or_duplicated(self):
        """Bỏ item thiếu id, trùng hoặc không phải dict."""
        posts = parse_tag_posts(APIFY_ITEMS + [APIFY_ITEMS[0], {'text': 'x'}, 'rác'], 'trang sức')
        self.assertEqual(len(posts), 3)


class SearchTagViewTests(SimpleTestCase):
    def _call(self, body):
        request = APIRequestFactory().post('/api/scraper/threads/fetch/search-tag/', body, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1, id=1))
        return threads_fetch_views.fetch_threads_search_tag(request)

    def test_missing_tag(self):
        """Tag rỗng → 400."""
        self.assertEqual(self._call({'tag': '  ', 'count': 20}).status_code, 400)

    def test_invalid_count(self):
        """count thiếu / <= 0 / không phải số → 400 (không có số bài mặc định)."""
        for count in (None, 0, -3, 'abc'):
            with self.subTest(count=count):
                self.assertEqual(self._call({'tag': 'trang sức', 'count': count}).status_code, 400)

    @override_settings(APIFY_ACTOR_THREADS_TAG='', APIFY_API_TOKEN='tok')
    def test_missing_config_returns_400_with_variable_name(self):
        """Thiếu biến cấu hình → 400 kèm tên biến."""
        res = self._call({'tag': 'trang sức', 'count': 20})
        self.assertEqual(res.status_code, 400)
        self.assertIn('APIFY_ACTOR_THREADS_TAG', res.data['error'])

    def test_apify_failure_returns_502(self):
        """Apify lỗi mạng → 502 kèm lý do."""
        with patch.object(threads_fetch_views, 'fetch_tag_posts', side_effect=requests.ConnectionError('down')):
            res = self._call({'tag': 'trang sức', 'count': 20})
        self.assertEqual(res.status_code, 502)

    def test_success(self):
        """Thành công: chuẩn hoá tag rồi trả bài đã parse."""
        with patch.object(threads_fetch_views, 'fetch_tag_posts', return_value=APIFY_ITEMS) as fetch:
            res = self._call({'tag': '#Trang Sức', 'count': 20})
        fetch.assert_called_once_with('trang sức', 20)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['tag'], 'trang sức')
        self.assertEqual(len(res.data['posts']), 3)
