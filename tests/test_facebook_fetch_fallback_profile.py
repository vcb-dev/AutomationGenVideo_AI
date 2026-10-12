"""Profile tạm phải tự khai báo là tạm, không được giả làm dữ liệu thật.

Endpoint fetch reels có 2 cấp fallback (cache 24h, rồi trích handle từ URL) để user thêm
kênh không bị crash khi RapidAPI hết quota. Vấn đề là cách nó báo cáo: `profile_api_ok`
tính bằng `profile is not None or parsed_profile is not None`, mà parsed_profile gần như
luôn có giá trị sau khi thêm fallback — nên cờ này luôn True. BE
(facebook-external-scraper.service.ts) dùng đúng cờ đó để quyết định có hard-fail hay
không, nên toàn bộ nhánh báo lỗi của BE thành code chết: RapidAPI sập mà user vẫn thấy
"cào xong", kênh 0 reels, không cảnh báo gì.

Thêm nữa profile tạm trả `profile_id` = handle trần và `is_verified` = False. BE ghi đè
mọi field truthy, và chỉ coi 'tmp_*' là bản ghi tạm — nên fanpage thật bị đổi tên thành
handle, mất tick xanh, và profile_id sai vĩnh viễn vì không bao giờ được "graduate".

Chạy: python manage.py test tests.test_facebook_fetch_fallback_profile
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from video_management.views.facebook_external_fetch_views import _build_fallback_profile


PAGE_URL = 'https://www.facebook.com/hapas.official'

_VIEW = 'video_management.views.facebook_external_fetch_views'


class BuildFallbackProfileTests(SimpleTestCase):
    def _fallback(self, cached=None):
        with patch(f'{_VIEW}._fallback_from_cache', return_value=cached or {}):
            return _build_fallback_profile(PAGE_URL)

    def test_profile_id_mang_tien_to_tmp(self):
        """BE nhận diện bản ghi tạm bằng tiền tố 'tmp_'. Handle trần sẽ bị coi là id thật."""
        self.assertEqual(self._fallback()['profile_id'], 'tmp_hapas.official')

    def test_is_verified_la_none_chu_khong_phai_false(self):
        """'Chưa biết' khác 'không có tick'. False sẽ xoá tick xanh thật bên BE."""
        self.assertIsNone(self._fallback()['is_verified'])

    def test_khong_dung_url_fanpage_thi_khong_dung_duoc_profile_tam(self):
        for url in [
            'https://www.facebook.com/watch/?v=123',
            'https://www.facebook.com/reel/456',
            'https://example.com/abc',
        ]:
            with self.subTest(url=url):
                with patch(f'{_VIEW}._fallback_from_cache', return_value={}):
                    self.assertEqual(_build_fallback_profile(url), {})

    def test_uu_tien_du_lieu_cache_hon_handle_tran(self):
        result = self._fallback({'name': 'HAPAS Official', 'avatar_url': 'https://cdn.fb/a.jpg', 'followers_count': 120000})
        self.assertEqual(result['name'], 'HAPAS Official')
        self.assertEqual(result['avatar_url'], 'https://cdn.fb/a.jpg')
        self.assertEqual(result['followers_count'], 120000)

    def test_khong_co_cache_thi_lay_handle_lam_ten_tam(self):
        result = self._fallback()
        self.assertEqual(result['name'], 'hapas.official')
        self.assertEqual(result['followers_count'], 0)
