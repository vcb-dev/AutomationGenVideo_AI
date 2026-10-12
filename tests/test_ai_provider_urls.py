"""URL của các nhà cung cấp AI (DeepSeek, MiniMax, HeyGen) phải đọc từ settings/.env.

Không phải vì URL là bí mật, mà vì URL gõ cứng trong service là loại cấu hình không
đổi được khi cần gấp: nhà cung cấp đổi domain (MiniMax từng đổi minimaxi.com →
minimax.io), cần trỏ qua proxy nội bộ, hoặc cần mock server khi test tải — tất cả
đều phải sửa code + deploy lại thay vì đổi một biến môi trường. Khuôn theo đúng
mẫu sẵn có của repo: TIKHUB_API_BASE_URL / DOUYIN_API_BASE_URL đọc qua settings.

Chạy: python manage.py test tests.test_ai_provider_urls
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings


def _deepseek_ok(content='{"title": "x"}'):
    r = MagicMock()
    r.ok = True
    r.status_code = 200
    r.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return r


class DeepSeekUrl(SimpleTestCase):
    """Cả 3 nơi gọi DeepSeek phải theo cùng một biến — sót nơi nào là nơi đó không đổi được."""

    # Phải cấp cả KEY, không chỉ URL: _call_deepseek chặn ngay từ đầu khi thiếu key, mà thân
    # test lại nuốt exception nên post không được gọi và call_args là None — lỗi hiện ra dưới
    # dạng TypeError khó hiểu thay vì "thiếu key". Máy dev có .env nên xanh, CI thì đỏ.
    @override_settings(DEEPSEEK_API_BASE_URL='https://ds.test', DEEPSEEK_API_KEY='sk-test')
    def test_task_script_service(self):
        from video_management.services import task_script_service as m
        with patch.object(m.requests, 'post', return_value=_deepseek_ok()) as post:
            try:
                m._call_deepseek('prompt')
            except Exception:
                pass  # chỉ cần biết đã gọi URL nào; parse kết quả không thuộc test này
        self.assertTrue(
            post.call_args[0][0].startswith('https://ds.test/'),
            f"Gọi nhầm: {post.call_args[0][0]}",
        )

    @override_settings(DEEPSEEK_API_BASE_URL='https://ds.test', DEEPSEEK_API_KEY='sk-test')
    def test_scraped_video_script_service(self):
        from video_management.services import scraped_video_script_service as m
        with patch.object(m.requests, 'post', return_value=_deepseek_ok()) as post:
            try:
                m._call_deepseek('prompt')
            except Exception:
                pass
        self.assertTrue(
            post.call_args[0][0].startswith('https://ds.test/'),
            f"Gọi nhầm: {post.call_args[0][0]}",
        )

    @override_settings(DEEPSEEK_API_BASE_URL='https://ds.test')
    def test_content_generation_service(self):
        from video_management.services import content_generation_service as m
        # __new__ để né __init__ (khởi tạo cả Anthropic client) — hàm chỉ cần 2 thuộc tính
        svc = m.ContentGenerationService.__new__(m.ContentGenerationService)
        svc.deepseek_key = 'k'
        svc.logger = MagicMock()
        with patch.object(m.requests, 'post', return_value=_deepseek_ok()) as post:
            svc._call_deepseek_raw('p', 'sys')
        self.assertTrue(
            post.call_args[0][0].startswith('https://ds.test/'),
            f"Gọi nhầm: {post.call_args[0][0]}",
        )


class DeepSeekUrlTrongViews(SimpleTestCase):
    """Hai view chat cũng gọi DeepSeek — lọt lưới lượt dọn đầu vì nằm ngoài services/."""

    @override_settings(DEEPSEEK_API_BASE_URL='https://ds.test')
    def test_chat_views(self):
        from video_management.views import chat_views as m
        self.assertEqual(m._deepseek_chat_url(), 'https://ds.test/chat/completions')

    @override_settings(DEEPSEEK_API_BASE_URL='https://ds.test')
    def test_analytics_chat_views(self):
        from video_management.views import analytics_chat_views as m
        self.assertEqual(m._deepseek_chat_url(), 'https://ds.test/chat/completions')


class MinimaxUrl(SimpleTestCase):
    @override_settings(MINIMAX_API_BASE_URL='https://mm.test')
    def test_tts_service(self):
        from video_management.services.minimax_tts_service import MinimaxTTSService
        self.assertEqual(MinimaxTTSService(api_key='sk-t').base_url, 'https://mm.test/t2a_v2')

    @override_settings(MINIMAX_API_BASE_URL='https://mm.test')
    def test_voice_clone_service(self):
        from video_management.services.minimax_voice_clone_service import MinimaxVoiceCloneService
        self.assertEqual(MinimaxVoiceCloneService(api_key='sk-t').base_url, 'https://mm.test')


class HeygenUrl(SimpleTestCase):
    """Hai endpoint từng gõ cứng api.heygen.com, bỏ qua luôn api_url đã cấu hình —
    đổi HEYGEN_API_URL chỉ đổi được một nửa client."""

    def test_endpoints_derive_from_api_url(self):
        import asyncio

        from heygen_service.heygen_client import HeyGenClient

        # __init__ mở sẵn aiohttp.ClientSession nên phải dựng trong event loop
        # (và đóng lại, không thì aiohttp kêu unclosed session).
        async def _run():
            c = HeyGenClient(api_key='k', api_url='https://hg.test/v2')
            try:
                return c._video_status_endpoint(), c._voice_clone_endpoint()
            finally:
                await c.close()

        status_ep, clone_ep = asyncio.run(_run())
        self.assertEqual(status_ep, 'https://hg.test/v1/video_status.get')
        self.assertEqual(clone_ep, 'https://hg.test/v2/voice/clone')


class FacebookGraphUrl(SimpleTestCase):
    @override_settings(
        FACEBOOK_GRAPH_BASE_URL='https://fb-mock.test/v25.0',
        FACEBOOK_APP_ID='app_id',
        FACEBOOK_APP_SECRET='app_secret',
    )
    def test_facebook_graph_service_base_url(self):
        from video_management.services.facebook_graph_service import FacebookGraphService
        svc = FacebookGraphService()
        self.assertEqual(svc.BASE_URL, 'https://fb-mock.test/v25.0')

    @override_settings(FACEBOOK_GRAPH_OAUTH_URL='https://fb-oauth.test/oauth/access_token')
    def test_facebook_token_store_oauth_url(self):
        from video_management.services.facebook_token_store import _get_graph_oauth_url
        self.assertEqual(_get_graph_oauth_url(), 'https://fb-oauth.test/oauth/access_token')
