"""Cào bài theo cộng đồng Reddit (subreddit) — POST /api/scraper/reddit/fetch/subreddit/.

Lọc theo cộng đồng bằng chính cú pháp tìm kiếm của Reddit "subreddit:<tên>" (kiểm chứng thật
2026-10-01: "subreddit:jewelry" trả 7/7 bài của r/jewelry), nên dùng chung parser với tìm từ khoá.
Thông tin cộng đồng lấy ngay trong post.subreddit; chỉ gọi fetch_subreddit_info khi không ra bài.

Chạy: python manage.py test tests.test_reddit_subreddit
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services.tikhub_reddit import normalize_subreddit_name, parse_subreddit
from video_management.views import reddit_fetch_views

from tests.test_reddit_search import SUBREDDIT, make_post


class NormalizeSubredditNameTests(SimpleTestCase):
    def test_accepts_common_inputs(self):
        """r/tên, /r/tên/, link (kể cả link chia sẻ /s/ của app), tên trần — không phân biệt hoa thường."""
        cases = {
            'r/Jewelry': 'jewelry',
            '/r/jewelry/': 'jewelry',
            'https://www.reddit.com/r/jewelry/top/?t=month': 'jewelry',
            'https://www.reddit.com/r/EngagementRings/s/AbCdEf': 'engagementrings',
            'https://old.reddit.com/r/jewelry/comments/1wj5qbf/x/': 'jewelry',
            'jewelry': 'jewelry',
        }
        for raw, name in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_subreddit_name(raw), name)

    def test_rejects_invalid(self):
        """Link người dùng, tên quá ngắn / ký tự lạ → ''."""
        for raw in ('u/someone', 'https://example.com', 'r/a', 'trang sức', '', None):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_subreddit_name(raw), '')


class ParseSubredditTests(SimpleTestCase):
    def test_maps_info(self):
        info = parse_subreddit({**SUBREDDIT, 'publicDescriptionText': 'Welcome to r/Jewelry!'})
        self.assertEqual(info, {
            'name': 'jewelry', 'display_name': 'r/jewelry', 'title': 'JEWELRY (Jewellery)',
            'description': 'Welcome to r/Jewelry!', 'icon_url': 'https://styles.redditmedia.com/icon.png',
            'subscribers_count': 382635, 'is_nsfw': False,
        })

    def test_missing_name(self):
        self.assertIsNone(parse_subreddit({}))
        self.assertIsNone(parse_subreddit(None))


class SubredditViewTests(SimpleTestCase):
    BODY = {'subreddit': 'https://www.reddit.com/r/Jewelry/', 'sort': 'HOT', 'time_range': 'all', 'count': 20}

    def _call(self, body):
        request = APIRequestFactory().post('/api/scraper/reddit/fetch/subreddit/', body, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1, id=1))
        return reddit_fetch_views.fetch_reddit_subreddit(request)

    def test_searches_with_subreddit_filter(self):
        """Lọc bằng "subreddit:<tên>", bỏ bài crosspost của cộng đồng khác, info lấy từ bài."""
        other = make_post('x', subreddit={**SUBREDDIT, 'name': 'pics', 'prefixedName': 'r/pics'})
        with patch.object(reddit_fetch_views, 'search_posts', return_value=[make_post('a'), other]) as search, \
                patch.object(reddit_fetch_views, 'fetch_subreddit_info') as info:
            res = self._call(self.BODY)

        search.assert_called_once_with('subreddit:jewelry', 'HOT', 'all', 20)
        info.assert_not_called()
        self.assertEqual(res.status_code, 200)
        self.assertEqual([p['post_id'] for p in res.data['posts']], ['a'])
        self.assertEqual(res.data['subreddit']['name'], 'jewelry')

    def test_no_posts_uses_info_endpoint(self):
        """Cộng đồng không có bài trong khoảng thời gian → vẫn trả info để BE lưu kênh."""
        with patch.object(reddit_fetch_views, 'search_posts', return_value=[]), \
                patch.object(reddit_fetch_views, 'fetch_subreddit_info', return_value=SUBREDDIT):
            res = self._call(self.BODY)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['posts'], [])
        self.assertEqual(res.data['subreddit']['title'], 'JEWELRY (Jewellery)')

    def test_unknown_subreddit_returns_404(self):
        with patch.object(reddit_fetch_views, 'search_posts', return_value=[]), \
                patch.object(reddit_fetch_views, 'fetch_subreddit_info', return_value=None):
            res = self._call(self.BODY)
        self.assertEqual(res.status_code, 404)
        self.assertIn('r/jewelry', res.data['error'])

    def test_invalid_name_returns_400(self):
        with patch.object(reddit_fetch_views, 'search_posts') as search:
            res = self._call({**self.BODY, 'subreddit': 'u/someone'})
        self.assertEqual(res.status_code, 400)
        search.assert_not_called()
