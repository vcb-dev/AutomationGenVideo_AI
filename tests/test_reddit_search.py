"""Tìm bài Reddit theo từ khoá qua TikHub — POST /api/scraper/reddit/fetch/search/.

Kiểm chứng thật ngày 2026-10-01: reddit.com/*.json trả 403 khi không đăng nhập; TikHub
app/fetch_dynamic_search $0.001/lượt, ~7 bài/trang, phân trang bằng pageInfo.endCursor, và chỉ
need_format=false mới có nội dung bài (content.markdown). Dữ liệu mẫu dưới đây rút gọn từ hồi đáp
thật của câu "engagement ring" (sort TOP, time_range month).

Chạy: python manage.py test tests.test_reddit_search
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.services import tikhub_reddit
from video_management.services.tikhub_errors import TikHubAuthError
from video_management.services.tikhub_reddit import parse_reddit_posts, search_posts
from video_management.views import reddit_fetch_views

TIKHUB = {'TIKHUB_API_KEY': 'key', 'TIKHUB_API_BASE_URL': 'https://tikhub.test'}

SUBREDDIT = {
    'id': 't5_2qkpi', 'name': 'jewelry', 'prefixedName': 'r/jewelry', 'title': 'JEWELRY (Jewellery)',
    'subscribersCount': 382635, 'isNsfw': False, 'styles': {'icon': 'https://styles.redditmedia.com/icon.png'},
}


def make_post(pid, title='Ring', **extra):
    post = {
        '__typename': 'SubredditPost',
        'id': f't3_{pid}',
        'createdAt': '2026-09-12T07:25:47.291000+0000',
        'postTitle': title,
        'url': f'https://i.redd.it/{pid}.jpeg',
        'content': None,
        'score': 100,
        'commentCount': 10,
        'authorInfo': {'name': 'Sleepydotexe', 'iconSmall': {'url': 'https://preview.redd.it/a.png'}},
        'thumbnail': {'url': 'https://preview.redd.it/t.jpeg'},
        'media': None,
        'permalink': f'/r/jewelry/comments/{pid}/ring/',
        'isSelfPost': False,
        'postHint': None,
        'gallery': None,
        'subreddit': SUBREDDIT,
        'outboundLink': {'url': f'https://i.redd.it/{pid}.jpeg'},
        'upvoteRatio': 0.96,
        'isNsfw': False,
    }
    post.update(extra)
    return post


def page(posts, end_cursor='', has_next=False):
    """Hồi đáp fetch_dynamic_search need_format=false (đúng đường dẫn tới bài trong dữ liệu thật)."""
    return {
        'search': {'dynamic': {'components': {'main': {
            'edges': [{'node': {'children': [{'post': p} for p in posts]}}],
            'pageInfo': {'endCursor': end_cursor, 'hasNextPage': has_next},
        }}}},
    }


def http(data, status=200):
    resp = MagicMock(ok=status < 400, status_code=status)
    resp.json.return_value = {'code': status, 'data': data}
    resp.text = ''
    return resp


class ParseRedditPostsTests(SimpleTestCase):
    def test_maps_fields(self):
        """Bài ảnh: id bỏ tiền tố t3_, link permalink đầy đủ, tác giả, cộng đồng, điểm, bình luận."""
        p = parse_reddit_posts([make_post('1we5wfx', 'My engagement ring glows', postHint='IMAGE',
                                          media={'still': {'source': {'url': 'https://preview.redd.it/full.jpeg'}}})],
                               keyword='engagement ring')[0]
        self.assertEqual(p['post_id'], '1we5wfx')
        self.assertEqual(p['url'], 'https://www.reddit.com/r/jewelry/comments/1we5wfx/ring/')
        self.assertEqual(p['author'], 'Sleepydotexe')
        self.assertEqual(p['subreddit_name'], 'jewelry')
        self.assertEqual(p['subreddit']['subscribers_count'], 382635)
        self.assertEqual((p['score'], p['comments_count']), (100, 10))
        self.assertEqual(p['media_type'], 'IMAGE')
        self.assertEqual(p['thumbnail_url'], 'https://preview.redd.it/full.jpeg')
        self.assertEqual(p['date_posted'][:19], '2026-09-12T07:25:47')
        self.assertEqual(p['search_keyword'], 'engagement ring')

    def test_media_types(self):
        """Video (streaming.hlsUrl), album (gallery), bài chữ (self post) — đủ các loại gặp trong dữ liệu thật."""
        video = make_post('v', postHint='HOSTED_VIDEO', media={'streaming': {'hlsUrl': 'https://v.redd.it/x/HLS.m3u8'}})
        gallery = make_post('g', gallery={'items': [{'media': {'url': 'https://i.redd.it/g1.jpg'}}]}, thumbnail=None)
        text = make_post('t', isSelfPost=True, thumbnail=None, content={'markdown': 'Câu chuyện dài...'})
        by_id = {p['post_id']: p for p in parse_reddit_posts([video, gallery, text])}
        self.assertEqual((by_id['v']['media_type'], by_id['v']['video_url']), ('VIDEO', 'https://v.redd.it/x/HLS.m3u8'))
        self.assertEqual((by_id['g']['media_type'], by_id['g']['thumbnail_url']), ('GALLERY', 'https://i.redd.it/g1.jpg'))
        self.assertEqual((by_id['t']['media_type'], by_id['t']['text'], by_id['t']['link_url']), ('TEXT', 'Câu chuyện dài...', ''))

    def test_prefers_640px_image(self):
        """Ảnh gốc có khi 3024px — thẻ bài dùng bản 640px (xlarge), thiếu thì lấy bản gần nhất."""
        still = {
            'source': {'url': 'https://preview.redd.it/full.jpeg'},
            'large': {'url': 'https://preview.redd.it/x.jpeg?width=320'},
            'xlarge': {'url': 'https://preview.redd.it/x.jpeg?width=640'},
        }
        p = parse_reddit_posts([make_post('a', postHint='IMAGE', media={'still': still})])[0]
        self.assertEqual(p['thumbnail_url'], 'https://preview.redd.it/x.jpeg?width=640')
        gallery = make_post('g', gallery={'items': [{'media': {
            'url': 'https://i.redd.it/g.jpg', 'large': {'url': 'https://preview.redd.it/g.jpg?width=320'}}}]})
        self.assertEqual(parse_reddit_posts([gallery])[0]['thumbnail_url'], 'https://preview.redd.it/g.jpg?width=320')

    def test_drops_invalid_and_duplicates(self):
        """Bỏ bài thiếu id / tiêu đề, trùng id, hoặc không phải dict."""
        posts = parse_reddit_posts([make_post('a'), make_post('a'), make_post('b', postTitle=''), {'id': ''}, 'rác'])
        self.assertEqual([p['post_id'] for p in posts], ['a'])


@override_settings(**TIKHUB)
class SearchPostsTests(SimpleTestCase):
    def test_paginates_until_count(self):
        """~7 bài/trang → đi tiếp bằng endCursor tới khi đủ số bài, không gọi thừa trang."""
        pages = [
            http(page([make_post(f'a{i}') for i in range(7)], 'c1', True)),
            http(page([make_post(f'b{i}') for i in range(7)], 'c2', True)),
        ]
        with patch.object(tikhub_reddit.requests, 'get', side_effect=pages) as get:
            posts = search_posts('engagement ring', 'TOP', 'month', 10)

        self.assertEqual(len(posts), 10)
        self.assertEqual(get.call_count, 2)
        first, second = (c.kwargs['params'] for c in get.call_args_list)
        self.assertEqual(first['need_format'], 'false')
        self.assertEqual((first['sort'], first['time_range'], first['search_type']), ('TOP', 'month', 'post'))
        self.assertNotIn('after', first)
        self.assertEqual(second['after'], 'c1')
        self.assertEqual(get.call_args.args[0], 'https://tikhub.test/api/v1/reddit/app/fetch_dynamic_search')

    def test_stops_when_no_next_page(self):
        with patch.object(tikhub_reddit.requests, 'get', return_value=http(page([make_post('a')], '', False))) as get:
            self.assertEqual(len(search_posts('x', 'HOT', 'all', 50)), 1)
        self.assertEqual(get.call_count, 1)

    def test_stops_on_page_without_new_posts(self):
        """Trang trả lại bài cũ (hoặc rỗng) thì dừng — không thành vòng lặp tốn tiền."""
        same = http(page([make_post('a')], 'c1', True))
        with patch.object(tikhub_reddit.requests, 'get', side_effect=[same, same, same]) as get:
            search_posts('x', 'HOT', 'all', 50)
        self.assertEqual(get.call_count, 2)

    def test_auth_error_is_raised(self):
        """Khoá TikHub hỏng (401) phải báo rõ, không giả làm "không có bài"."""
        with patch.object(tikhub_reddit.requests, 'get', return_value=http({}, status=401)):
            with self.assertRaises(TikHubAuthError):
                search_posts('x', 'HOT', 'all', 10)

    @override_settings(TIKHUB_API_KEY='')
    def test_missing_key_names_variable(self):
        with self.assertRaisesMessage(tikhub_reddit.RedditConfigError, 'TIKHUB_API_KEY'):
            search_posts('x', 'HOT', 'all', 10)


class SearchViewTests(SimpleTestCase):
    def _call(self, body):
        request = APIRequestFactory().post('/api/scraper/reddit/fetch/search/', body, format='json')
        force_authenticate(request, user=SimpleNamespace(is_authenticated=True, pk=1, id=1))
        return reddit_fetch_views.fetch_reddit_search(request)

    def test_validates_input(self):
        """Không có giá trị mặc định: thiếu / sai query, sort, time_range, count đều 400."""
        ok = {'query': 'ring', 'sort': 'TOP', 'time_range': 'month', 'count': 20}
        for bad in ({'query': ' '}, {'sort': 'BEST'}, {'time_range': 'decade'}, {'count': 0}, {'count': 'abc'}):
            with self.subTest(bad=bad):
                self.assertEqual(self._call({**ok, **bad}).status_code, 400)

    def test_success(self):
        with patch.object(reddit_fetch_views, 'search_posts', return_value=[make_post('a')]) as search:
            res = self._call({'query': 'engagement ring', 'sort': 'top', 'time_range': 'MONTH', 'count': 20})
        search.assert_called_once_with('engagement ring', 'TOP', 'month', 20)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['posts'][0]['search_keyword'], 'engagement ring')

    @override_settings(TIKHUB_API_KEY='', TIKHUB_API_BASE_URL='https://tikhub.test')
    def test_missing_config_returns_400(self):
        res = self._call({'query': 'ring', 'sort': 'TOP', 'time_range': 'month', 'count': 20})
        self.assertEqual(res.status_code, 400)
        self.assertIn('TIKHUB_API_KEY', res.data['error'])
