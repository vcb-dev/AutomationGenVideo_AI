"""Thống kê chi phí TikHub + Gemini của Khám phá Video: AI đo chi phí từng lượt gọi, gửi về BE lưu.

Khoá: MỌI lượt gọi TikHub đều được đo tại một chỗ (tikhub_meter móc vào tầng gửi của requests —
không phụ thuộc tệp nào gọi), chỉ lượt gọi THẬT mới tính tiền (dùng lại bộ đệm = 0đ, lỗi HTTP = 0đ),
giá TikHub theo bảng giá của chính TikHub, giá Gemini theo số token Gemini trả về (gồm token suy
nghĩ) và đổi theo mốc thời gian; mọi request gửi chi phí về BE qua header X-Api-Usage.

Chạy: python manage.py test tests.test_api_usage_cost
"""
import json
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from video_management.api_usage_middleware import ApiUsageMiddleware
from video_management.services import gemini_prices, gemini_video_script, tikhub_cache, tikhub_meter, tikhub_prices, usage_meter
from video_management.views import tikhub_account_views

PRICED_PATH = '/api/v1/xiaohongshu/app_v2/get_video_note_detail'


class UsageMeterTests(SimpleTestCase):
    def test_ngoai_khoi_collect_thi_khong_ghi_gi(self):
        usage_meter.record('tikhub', '/x', cost_usd=0.001)  # không lỗi, không rò sang request sau
        with usage_meter.collect() as events:
            pass
        self.assertEqual(events, [])

    def test_gom_dung_su_kien_trong_mot_request(self):
        with usage_meter.collect() as events:
            usage_meter.record('tikhub', '/a', cost_usd=0.0015, status=200)
            usage_meter.record('gemini', 'gemini-3.8-flash', cost_usd=0.012, input_tokens=7000, output_tokens=1500)
        self.assertEqual([e['provider'] for e in events], ['tikhub', 'gemini'])
        self.assertEqual(events[1]['input_tokens'], 7000)

    def test_collect_long_nhau_deu_nhan_du_su_kien(self):
        # middleware (ngoài) gửi header, view (trong) trả `usage` trong thân — không bên nào thiếu
        with usage_meter.collect() as outer:
            usage_meter.record('tikhub', '/a', cost_usd=0.001, status=200)
            with usage_meter.collect() as inner:
                usage_meter.record('gemini', 'm', cost_usd=0.002)
            usage_meter.record('tikhub', '/b', cost_usd=0.003, status=200)
        self.assertEqual([e['endpoint'] for e in outer], ['/a', 'm', '/b'])
        self.assertEqual([e['endpoint'] for e in inner], ['m'])

    def test_gop_luot_giong_nhau_thanh_mot_dong_co_so_luot(self):
        with usage_meter.collect() as events:
            for _ in range(3):
                usage_meter.record('tikhub', '/posts', cost_usd=0.001, status=200)
            usage_meter.record('tikhub', '/posts', cost_usd=0, status=404)
        rows = usage_meter.summarize(events)
        self.assertEqual([(r['endpoint'], r['status'], r['calls'], r['cost_usd']) for r in rows],
                         [('/posts', 200, 3, 0.003), ('/posts', 404, 1, 0.0)])


@override_settings(TIKHUB_API_KEY='k', CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class TikhubCostTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        cache.set(tikhub_prices.CACHE_KEY, {'/api/v1/xiaohongshu/app_v2/get_video_note_detail': 0.01}, 60)

    def _resp(self, status=200, body=None):
        return SimpleNamespace(status_code=status, ok=status == 200, json=lambda: body or {'code': 200, 'data': {}})

    def test_bo_dem_chi_ghi_luot_dung_lai_0d_luot_that_de_moc_ghi(self):
        # lượt gọi thật do tikhub_meter ghi ở tầng requests; goi_co_dem ghi thêm là tính trùng
        with usage_meter.collect() as events, mock.patch.object(tikhub_cache.requests, 'get', return_value=self._resp()) as get:
            tikhub_cache.goi_co_dem('https://api', PRICED_PATH, {'note_id': '1'}, 'k')
            tikhub_cache.goi_co_dem('https://api', PRICED_PATH, {'note_id': '1'}, 'k')
        self.assertEqual(get.call_count, 1)
        self.assertEqual([(e['cost_usd'], e['cached']) for e in events], [(0.0, True)])

    def test_endpoint_ngoai_bang_gia_dung_du_phong(self):
        self.assertEqual(tikhub_prices.price_of('/api/v1/kuaishou/web/fetch_one_video'), 0.002)
        self.assertEqual(tikhub_prices.price_of('/api/v1/tiktok/app/v3/fetch_one_video'), tikhub_prices.DEFAULT_PRICE)


@override_settings(TIKHUB_API_BASE_URL='https://api.tikhub.io',
                   CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class TikhubMeterTests(SimpleTestCase):
    """Móc ở tầng gửi của requests: lượt gọi nào tới TikHub cũng được đo, dù tệp nào gọi."""

    def setUp(self):
        cache.clear()
        cache.set(tikhub_prices.CACHE_KEY, {PRICED_PATH: 0.01}, 60)

    def _observe(self, url, status=200):
        tikhub_meter.observe(SimpleNamespace(url=url), SimpleNamespace(status_code=status))

    def test_goi_that_tinh_theo_bang_gia_loi_http_0d(self):
        with usage_meter.collect() as events:
            self._observe(f'https://api.tikhub.io{PRICED_PATH}?note_id=1')
            self._observe('https://api.tikhub.io/api/v1/youtube/web/get_video_info_v2?video_id=x', status=404)
        self.assertEqual([(e['endpoint'], e['status'], e['cost_usd']) for e in events],
                         [(PRICED_PATH, 200, 0.01), ('/api/v1/youtube/web/get_video_info_v2', 404, 0.0)])

    def test_bo_qua_host_khac_endpoint_tra_tai_khoan_va_ngoai_khoi_do(self):
        with usage_meter.collect() as events:
            self._observe('https://generativelanguage.googleapis.com/v1beta/models/x:generateContent')
            self._observe('https://api.tikhub.io/api/v1/tikhub/user/get_user_daily_usage')
        self.assertEqual(events, [])
        self._observe(f'https://api.tikhub.io{PRICED_PATH}')  # ngoài collect: không lỗi, không ghi

    @override_settings(TIKHUB_API_BASE_URL='https://tikhub.proxy.local')
    def test_host_cau_hinh_rieng_van_duoc_do(self):
        with usage_meter.collect() as events:
            self._observe(f'https://tikhub.proxy.local{PRICED_PATH}')
        self.assertEqual(len(events), 1)

    def test_moc_gui_nhu_cu_va_loi_khi_do_khong_lam_hong_luot_goi(self):
        response = SimpleNamespace(status_code=200)
        send = tikhub_meter.wrap(lambda adapter, request, **kw: response)
        request = SimpleNamespace(url=f'https://api.tikhub.io{PRICED_PATH}')
        with usage_meter.collect() as events:
            self.assertIs(send(None, request, timeout=5), response)
        self.assertEqual(events[0]['cost_usd'], 0.01)
        with usage_meter.collect(), mock.patch.object(tikhub_meter, 'price_of', side_effect=RuntimeError('mất mạng')):
            self.assertIs(send(None, request), response)

    def test_da_gan_moc_cho_ca_tien_trinh(self):
        from requests.adapters import HTTPAdapter
        self.assertTrue(getattr(HTTPAdapter.send, tikhub_meter._INSTALLED_MARK, False))
        before = HTTPAdapter.send
        tikhub_meter.install()  # gọi lại không bọc thêm lớp nữa
        self.assertIs(HTTPAdapter.send, before)


class ApiUsageMiddlewareTests(SimpleTestCase):
    def test_gui_chi_phi_ve_BE_qua_header_da_gop(self):
        def view(request):
            usage_meter.record('tikhub', '/posts', cost_usd=0.001, status=200)
            usage_meter.record('tikhub', '/posts', cost_usd=0.001, status=200)
            return HttpResponse('ok')
        response = ApiUsageMiddleware(view)(RequestFactory().get('/x'))
        rows = json.loads(response['X-Api-Usage'])
        self.assertEqual([(r['endpoint'], r['calls'], r['cost_usd']) for r in rows], [('/posts', 2, 0.002)])

    def test_khong_ton_gi_thi_khong_gan_header(self):
        response = ApiUsageMiddleware(lambda request: HttpResponse('ok'))(RequestFactory().get('/x'))
        self.assertFalse(response.has_header('X-Api-Usage'))

    def test_da_dang_ky_trong_settings(self):
        from django.conf import settings
        self.assertIn('video_management.api_usage_middleware.ApiUsageMiddleware', settings.MIDDLEWARE)


class GeminiCostTests(SimpleTestCase):
    def test_gia_38_flash_tang_gap_doi_tu_2027(self):
        self.assertEqual(gemini_prices.prices_for('gemini-3.8-flash', date(2026, 12, 31)), (0.75, 3.75, 0.75))
        self.assertEqual(gemini_prices.prices_for('gemini-3.8-flash', date(2027, 1, 1)), (1.50, 7.50, 1.50))

    def test_ten_flash_latest_tinh_nhu_ban_flash_moi_nhat(self):
        self.assertEqual(gemini_prices.prices_for('gemini-flash-latest', date(2026, 9, 28)), (0.75, 3.75, 0.75))
        self.assertEqual(gemini_prices.prices_for('gemini-flash-lite-latest'), (0.25, 1.50, 0.50))

    def test_flash_lite_tach_gia_am_thanh(self):
        # Đo thật (video 75,9s, gemini-3.1-flash-lite): 7.264 token vào, 305 ra; tiếng ≈ 25 token/giây
        audio = round(gemini_prices.AUDIO_TOKENS_PER_SECOND * 75.9)
        cost = gemini_prices.cost_usd('gemini-3.1-flash-lite', 7264, 305, audio_tokens=audio)
        self.assertAlmostEqual(cost, ((7264 - audio) * 0.25 + audio * 0.50 + 305 * 1.50) / 1e6)
        self.assertEqual(round(cost * 26000), 71)
        # phần âm thanh không bao giờ vượt tổng input
        self.assertAlmostEqual(gemini_prices.cost_usd('gemini-3.1-flash-lite', 100, 0, audio_tokens=999), 100 * 0.50 / 1e6)

    def test_ghi_de_gia_qua_bien_moi_truong(self):
        with mock.patch.dict('os.environ', {'GEMINI_PRICES_JSON': '{"gemini-3.8-flash": [[null, 1.0, 2.0]]}'}):
            self.assertEqual(gemini_prices.prices_for('gemini-3.8-flash'), (1.0, 2.0, 1.0))
        with mock.patch.dict('os.environ', {'GEMINI_PRICES_JSON': '{"gemini-3.1-flash-lite": [[null, 0.3, 2.0, 0.6]]}'}):
            self.assertEqual(gemini_prices.prices_for('gemini-3.1-flash-lite'), (0.3, 2.0, 0.6))

    def test_model_ngoai_bang_gia_van_tinh_duoc_ca_phan_am_thanh(self):
        # model có thật nhưng chưa có giá (vd bản pro) + video có tiếng → không được lỗi TypeError
        self.assertEqual(gemini_prices.prices_for('gemini-3.1-pro-preview'), gemini_prices.UNKNOWN_MODEL_PRICE)
        self.assertGreater(gemini_prices.cost_usd('gemini-3.1-pro-preview', 7264, 305, audio_tokens=1898), 0)

    def test_gia_ghi_de_sai_dinh_dang_thi_dung_bang_mac_dinh(self):
        with mock.patch.dict('os.environ', {'GEMINI_PRICES_JSON': '{"gemini-3.1-flash-lite": [[null, "abc", 1.5]]}'}):
            self.assertEqual(gemini_prices.prices_for('gemini-3.1-flash-lite'), (0.25, 1.50, 0.50))
        with mock.patch.dict('os.environ', {'GEMINI_PRICES_JSON': '{"gemini-x": [[]]}'}):
            self.assertEqual(gemini_prices.prices_for('gemini-x'), gemini_prices.UNKNOWN_MODEL_PRICE)

    def test_tinh_tien_theo_token(self):
        # clip 66s đo thật ≈ 7.147 token vào; giả sử 2.000 token ra
        self.assertAlmostEqual(gemini_prices.cost_usd('gemini-3.8-flash', 7147, 2000, date(2026, 9, 28)),
                               (7147 * 0.75 + 2000 * 3.75) / 1e6)

    def test_ghi_so_token_that_gom_ca_token_suy_nghi(self):
        response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=7147, candidates_token_count=900, total_token_count=9147))
        with usage_meter.collect() as events:
            gemini_video_script._record_usage('gemini-3.8-flash', response)
        e = events[0]
        self.assertEqual((e['provider'], e['input_tokens'], e['output_tokens']), ('gemini', 7147, 2000))
        self.assertGreater(e['cost_usd'], 0)

    def test_ghi_chi_phi_tach_phan_am_thanh(self):
        response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=7264, total_token_count=7569))
        with usage_meter.collect() as events:
            gemini_video_script._record_usage('gemini-3.1-flash-lite', response, audio_seconds=75.9)
        self.assertEqual(round(events[0]['cost_usd'] * 26000), 71)

    def test_ghi_chi_phi_theo_ten_model_cau_hinh(self):
        class FakeModel:
            def __init__(self, name, generation_config=None):
                self.name = name
            def generate_content(self, contents, request_options=None):
                return SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10, total_token_count=30))
        with usage_meter.collect() as events, mock.patch.object(gemini_video_script, '_model_name', return_value='gemini-3.1-flash-lite'):
            gemini_video_script._generate_once(SimpleNamespace(GenerativeModel=FakeModel), 'x', 10)
        self.assertEqual([e['endpoint'] for e in events], ['gemini-3.1-flash-lite'])


class EndpointsReturnUsageTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(pk='u1', is_authenticated=True)

    @mock.patch('video_management.views.scraped_video_script_views.VideoScriptThrottle.allow_request', return_value=True)
    def test_script_from_video_tra_usage_ke_ca_khi_tai_hong(self, _t):
        from video_management.services import video_script_pipeline as pipeline
        from video_management.views import scraped_video_script_views as views

        def fake_download(*a, **k):
            usage_meter.record('tikhub', '/api/v1/reddit/app/fetch_post_details', cost_usd=0.001, status=200)
            raise pipeline.VideoDownloadError(['hỏng'])
        request = self.factory.post('/api/scraped-video/script-from-video/', {'video_url': 'https://www.reddit.com/r/a/comments/1abcdef/x/'}, format='json')
        force_authenticate(request, user=self.user)
        text_only = {'has_voice': False, 'language': '', 'transcript': '', 'script_text': 'x', 'source': 'gemini_text'}
        with mock.patch.object(pipeline, 'download_for_analysis', side_effect=fake_download), \
             mock.patch.object(pipeline, 'script_engine', return_value='gemini'), \
             mock.patch.object(gemini_video_script, 'generate_script', return_value=text_only):
            res = views.generate_script_from_video(request)
        self.assertEqual(res.data['status'], 'DONE')
        self.assertEqual(res.data['usage'][0]['endpoint'], '/api/v1/reddit/app/fetch_post_details')

    def test_video_detail_tra_usage(self):
        from video_management.views import video_detail_views as views

        def fake_detail(*a):
            usage_meter.record('tikhub', '/api/v1/douyin/web/fetch_one_video', cost_usd=0.001, status=200)
            return {'title': 'x'}
        request = self.factory.post('/api/scraper/video-detail/', {'platform': 'douyin', 'video_id': '1'}, format='json')
        with mock.patch.object(views, 'fetch_video_detail', side_effect=fake_detail):
            res = views.get_video_detail(request)
        self.assertEqual(res.data['usage'][0]['cost_usd'], 0.001)


@override_settings(TIKHUB_API_KEY='k')
class TikhubAccountTests(SimpleTestCase):
    def test_tong_hop_so_du_va_top_endpoint_ton_nhat(self):
        # Rút gọn từ hồi đáp thật get_user_info / get_user_daily_usage (2026-09-28)
        replies = {
            '/api/v1/tikhub/user/get_user_info': {'api_key_data': {'api_key_name': 'VCB-DEV'}, 'user_data': {'balance': 10.3608, 'free_credit': 0.0}},
            '/api/v1/tikhub/user/get_user_daily_usage': {'time_zone': 'America/Los_Angeles', 'data': {
                'date': '2026-09-27', 'usage': 0.404, 'total_request_per_day': 400, 'paid_request_per_day': 391,
                'uri_counts': {'/api/v1/tiktok/app/v3/fetch_user_post_videos': 193, '/api/v1/xiaohongshu/app_v2/get_video_note_detail': 1}}},
        }
        with mock.patch.object(tikhub_account_views, '_get', side_effect=lambda p: replies[p]), \
             mock.patch.object(tikhub_account_views, 'price_of', side_effect=lambda p: 0.01 if 'xiaohongshu' in p else 0.001):
            s = tikhub_account_views.build_summary()
        self.assertEqual((s['key_name'], s['balance_usd'], s['today']['usage_usd'], s['today']['paid_requests']), ('VCB-DEV', 10.3608, 0.404, 391))
        self.assertEqual(s['today']['top_endpoints'][0], {'endpoint': '/api/v1/tiktok/app/v3/fetch_user_post_videos', 'count': 193, 'cost_usd': 0.193})
