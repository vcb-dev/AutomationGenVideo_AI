"""Handle fanpage bóc từ URL không được là đoạn path cố định của Facebook.

Page không đặt tên rút gọn có URL dạng facebook.com/p/<Tên>-<id>/ (hoặc /people/<Tên>/<id>/,
profile.php?id=<id>). Bản cũ lấy đoạn path đầu làm handle nên 6/14 fanpage ở mục Khám phá kênh
hiện "@p", và khi RapidAPI lỗi thì mọi page kiểu này cùng nhận id tạm "tmp_p" + tra cache theo
username "p" — page này có thể mượn tên/avatar của page khác. Bản RapidAPI còn không loại cả
"profile.php".

Chạy: python manage.py test tests.test_facebook_page_handle
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from video_management.services import apify_facebook, rapidapi_facebook
from video_management.services.facebook_page_url import extract_page_handle
from video_management.views.facebook_external_fetch_views import _build_fallback_profile

_VIEW = 'video_management.views.facebook_external_fetch_views'

URLS_WITHOUT_HANDLE = [
    'https://www.facebook.com/p/iFacet-61579965066322/',
    'https://www.facebook.com/p/House-of-Midas-Luxe-61565578125138',
    'https://www.facebook.com/people/Wen-Huang/61590472511842/',
    'https://www.facebook.com/profile.php?id=100063576820959',
    'https://www.facebook.com/share/1AbCdEfGh/',
    'https://www.facebook.com/watch/?v=123',
    'https://www.facebook.com/reel/456',
    'https://www.facebook.com/groups/12345',
    '',
    None,
]


class ExtractPageHandleTests(SimpleTestCase):
    def test_vanity_url_returns_handle(self):
        """URL tên rút gọn trả đúng handle, kể cả link tab Reels / m.facebook.com."""
        cases = {
            'https://www.facebook.com/hapas.official': 'hapas.official',
            'https://www.facebook.com/kazan.jewelry/': 'kazan.jewelry',
            'https://m.facebook.com/daychetacvangbac.vn?mibextid=ZbWKwL': 'daychetacvangbac.vn',
            'https://www.facebook.com/SenninEskoJewelry/reels/': 'SenninEskoJewelry',
            'https://www.facebook.com/100063576820959': '100063576820959',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_page_handle(url), expected)

    def test_url_without_vanity_name_returns_empty(self):
        """/p/, /people/, profile.php, share, watch, reel, groups → không có handle."""
        for url in URLS_WITHOUT_HANDLE:
            with self.subTest(url=url):
                self.assertEqual(extract_page_handle(url), '')

    def test_fixed_segments_are_case_insensitive(self):
        """Đoạn cố định viết hoa (/P/, /Profile.php) vẫn không phải handle."""
        self.assertEqual(extract_page_handle('https://www.facebook.com/P/Name-123/'), '')
        self.assertEqual(extract_page_handle('https://www.facebook.com/Profile.php?id=1'), '')


class SourcesShareHandleRuleTests(SimpleTestCase):
    """RapidAPI, Apify và profile tạm phải cho cùng một kết quả với cùng URL."""

    def test_rapidapi_profile_has_no_p_handle(self):
        """RapidAPI trả page /p/... → handle rỗng, profile_id giữ nguyên."""
        parsed = rapidapi_facebook.parse_fanpage_profile(
            {'ad_page_id': '405898282610977', 'title': 'House of Midas Luxe',
             'url': 'https://www.facebook.com/p/House-of-Midas-Luxe-61565578125138/'},
            {},
        )
        self.assertEqual(parsed['handle'], '')
        self.assertEqual(parsed['profile_id'], '405898282610977')

    def test_rapidapi_profile_has_no_profile_php_handle(self):
        """Bản RapidAPI cũ không loại 'profile.php'."""
        parsed = rapidapi_facebook.parse_fanpage_profile(
            {'ad_page_id': '1', 'title': 'X', 'url': 'https://www.facebook.com/profile.php?id=1'}, {},
        )
        self.assertEqual(parsed['handle'], '')

    def test_rapidapi_profile_keeps_real_handle(self):
        """Page có tên rút gọn vẫn giữ handle."""
        parsed = rapidapi_facebook.parse_fanpage_profile(
            {'ad_page_id': '1', 'title': 'Kazan', 'url': 'https://www.facebook.com/kazan.jewelry'}, {},
        )
        self.assertEqual(parsed['handle'], 'kazan.jewelry')

    def test_apify_uses_same_rule(self):
        """Apify cho cùng kết quả với RapidAPI trên cùng URL."""
        for url in URLS_WITHOUT_HANDLE:
            with self.subTest(url=url):
                self.assertEqual(apify_facebook._extract_handle(url), '')
        self.assertEqual(apify_facebook._extract_handle('https://www.facebook.com/kazan.jewelry'), 'kazan.jewelry')

    def test_no_fallback_profile_for_p_pages(self):
        """Không còn id tạm chung 'tmp_p' cho mọi page /p/..."""
        with patch(f'{_VIEW}._fallback_from_cache', return_value={}) as cache:
            self.assertEqual(_build_fallback_profile('https://www.facebook.com/p/iFacet-61579965066322/'), {})
            self.assertEqual(_build_fallback_profile('https://www.facebook.com/people/Wen-Huang/61590472511842/'), {})
            cache.assert_not_called()
