"""Transcribe thuộc module CHUYỂN VIDEO THÀNH TEXT: khoá riêng VIDEO_TO_TEXT_GEMINI_API_KEY và model
riêng TRANSCRIBE_GEMINI_MODEL, tách hẳn khỏi module TẠO ẢNH (ảnh thẻ: GEMINI_API_KEY + GEMINI_MODEL).

Lỗi gốc: transcribe đọc chung GEMINI_MODEL với ảnh thẻ, mà trên server biến đó là
gemini-3.1-flash-image — model TẠO ẢNH, đầu vào "Text, Image, Video, and PDF", không có âm thanh
(ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image, đọc 2026-09-28). Transcribe khi đó
"nghe" bằng một model không nghe được tiếng. Cũng không thể đổi GEMINI_MODEL sang model chữ: đo
thật, gemini-3.1-flash-lite trả NO_IMAGE nên ảnh thẻ sẽ hỏng. Vì vậy tách biến.

SDK được thay bằng bản giả chỉ để khoá luồng điều khiển của phía mình (chọn model, dự phòng khi
404, khoá API riêng). Hành vi thật của SDK (404 → NotFound, model nghe được tiếng) đã kiểm
bằng Gemini thật trên image dựng từ Dockerfile.railway.

Chạy: python manage.py test tests.test_transcribe_gemini_model
"""
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from google.api_core import exceptions as google_api_exceptions

from video_management.views import transcribe_views

LOGGER = 'video_management.views.transcribe_views'


class FakeGenai:
    """Thay google.generativeai: ghi lại khoá API, các model đã gọi và file đã xoá trên Gemini."""

    def __init__(self, failures=None):
        self.failures = failures or {}  # tên model → exception khi generate_content
        self.api_key = None
        self.models = []
        self.deleted = []

    def patch(self):
        uploaded = SimpleNamespace(name='files/abc', state=SimpleNamespace(name='ACTIVE'))
        fake = self

        class Model:
            def __init__(self, name, **kwargs):
                self.name = name

            def generate_content(self, contents, request_options=None):
                fake.models.append(self.name)
                if self.name in fake.failures:
                    raise fake.failures[self.name]
                return SimpleNamespace(text=f'  lời thoại từ {self.name}  ')

        return mock.patch.multiple(
            'google.generativeai',
            configure=lambda api_key=None, **kwargs: setattr(self, 'api_key', api_key),
            upload_file=lambda path, **kwargs: uploaded,
            get_file=lambda name: uploaded,
            GenerativeModel=Model,
            delete_file=lambda name: self.deleted.append(name),
        )


class TranscribeModelNameTests(SimpleTestCase):
    def test_defaults_to_flash_lite_even_when_gemini_model_is_image_model(self):
        """Mặc định gemini-3.1-flash-lite, bỏ qua GEMINI_MODEL = model tạo ảnh như trên server."""
        with mock.patch.dict(os.environ, {'GEMINI_MODEL': 'gemini-3.1-flash-image', 'TRANSCRIBE_GEMINI_MODEL': ''}), \
             override_settings(GEMINI_MODEL='gemini-3.1-flash-image', TRANSCRIBE_GEMINI_MODEL=''):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-3.1-flash-lite')

    def test_transcribe_gemini_model_overrides_default(self):
        """Đặt TRANSCRIBE_GEMINI_MODEL (biến môi trường hoặc settings) thì dùng đúng giá trị đó."""
        with mock.patch.dict(os.environ, {'TRANSCRIBE_GEMINI_MODEL': ' gemini-3.5-flash '}):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-3.5-flash')
        with override_settings(TRANSCRIBE_GEMINI_MODEL='gemini-2.5-flash'):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-2.5-flash')

    def test_blank_value_falls_back_to_default(self):
        """Biến chỉ có khoảng trắng coi như chưa đặt."""
        with mock.patch.dict(os.environ, {'TRANSCRIBE_GEMINI_MODEL': '   '}), \
             override_settings(TRANSCRIBE_GEMINI_MODEL=''):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-3.1-flash-lite')


@override_settings(VIDEO_TO_TEXT_GEMINI_API_KEY='video-to-text-key', GEMINI_API_KEY='image-key',
                   GEMINI_MODEL='gemini-3.1-flash-image', TRANSCRIBE_GEMINI_MODEL='')
class TranscribeWithGeminiTests(SimpleTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.mp4')
        os.write(fd, b'\x00' * 1024)
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        env = mock.patch.dict(os.environ, {'TRANSCRIBE_GEMINI_MODEL': ''})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, fake):
        with fake.patch():
            return transcribe_views.transcribe_with_gemini(self.path)

    def test_listens_with_flash_lite_using_video_to_text_key(self):
        """Nghe bằng flash-lite với khoá VIDEO_TO_TEXT_GEMINI_API_KEY — không đụng khoá/model tạo ảnh."""
        fake = FakeGenai()
        self.assertEqual(self._run(fake), 'lời thoại từ gemini-3.1-flash-lite')
        self.assertEqual(fake.api_key, 'video-to-text-key')
        self.assertEqual(fake.models, ['gemini-3.1-flash-lite'])
        self.assertEqual(fake.deleted, ['files/abc'])

    def test_missing_video_to_text_key_never_borrows_image_key(self):
        """Thiếu khoá video→text → báo lỗi rõ tên biến, KHÔNG lấy tạm GEMINI_API_KEY, không gọi Gemini."""
        fake = FakeGenai()
        with override_settings(VIDEO_TO_TEXT_GEMINI_API_KEY=''), \
             mock.patch.dict(os.environ, {'VIDEO_TO_TEXT_GEMINI_API_KEY': '', 'GEMINI_API_KEY': 'image-key'}):
            with self.assertRaisesMessage(ValueError, 'VIDEO_TO_TEXT_GEMINI_API_KEY'):
                self._run(fake)
        self.assertIsNone(fake.api_key)
        self.assertEqual(fake.models, [])

    def test_video_to_text_key_read_from_environment(self):
        """Không có trong settings thì đọc biến môi trường (Railway), bỏ khoảng trắng thừa."""
        with override_settings(VIDEO_TO_TEXT_GEMINI_API_KEY=''), \
             mock.patch.dict(os.environ, {'VIDEO_TO_TEXT_GEMINI_API_KEY': ' env-key '}):
            self.assertEqual(transcribe_views._video_to_text_api_key(), 'env-key')

    def test_missing_model_falls_back_to_flash_lite_latest(self):
        """Tên model không tồn tại (404) → dùng gemini-flash-lite-latest và cảnh báo sửa cấu hình."""
        # Nguyên văn thông báo Gemini trả cho tên "gemini-3.1-flash" (đo thật 2026-09-28)
        fake = FakeGenai({'gemini-3.1-flash': google_api_exceptions.NotFound(
            'models/gemini-3.1-flash is not found for API version v1beta, or is not supported for generateContent.')})
        with override_settings(TRANSCRIBE_GEMINI_MODEL='gemini-3.1-flash'), \
             self.assertLogs(LOGGER, level='WARNING') as logs:
            text = self._run(fake)
        self.assertEqual(text, 'lời thoại từ gemini-flash-lite-latest')
        self.assertEqual(fake.models, ['gemini-3.1-flash', 'gemini-flash-lite-latest'])
        self.assertIn('Sửa TRANSCRIBE_GEMINI_MODEL', logs.output[0])
        self.assertEqual(fake.deleted, ['files/abc'])

    def test_fallback_also_missing_raises_after_two_attempts(self):
        """Model dự phòng cũng 404 → báo lỗi sau đúng 2 lượt, vẫn xoá file trên Gemini."""
        gone = google_api_exceptions.NotFound('gone')
        fake = FakeGenai({'gemini-3.1-flash': gone, 'gemini-flash-lite-latest': gone})
        with override_settings(TRANSCRIBE_GEMINI_MODEL='gemini-3.1-flash'), self.assertLogs(LOGGER, level='WARNING'):
            with self.assertRaises(google_api_exceptions.NotFound):
                self._run(fake)
        self.assertEqual(fake.models, ['gemini-3.1-flash', 'gemini-flash-lite-latest'])
        self.assertEqual(fake.deleted, ['files/abc'])

    def test_configured_fallback_model_is_tried_once(self):
        """Cấu hình đúng bằng model dự phòng mà 404 → không gọi lại lần hai."""
        fake = FakeGenai({'gemini-flash-lite-latest': google_api_exceptions.NotFound('gone')})
        with override_settings(TRANSCRIBE_GEMINI_MODEL='gemini-flash-lite-latest'):
            with self.assertRaises(google_api_exceptions.NotFound):
                self._run(fake)
        self.assertEqual(fake.models, ['gemini-flash-lite-latest'])

    def test_non_404_errors_do_not_switch_model(self):
        """Hết hạn mức (429) không phải lỗi cấu hình — đổi model chỉ tốn thêm một lượt gọi."""
        fake = FakeGenai({'gemini-3.1-flash-lite': google_api_exceptions.ResourceExhausted('quota')})
        with self.assertRaises(google_api_exceptions.ResourceExhausted):
            self._run(fake)
        self.assertEqual(fake.models, ['gemini-3.1-flash-lite'])

    def test_generate_timeout_still_becomes_timeout_error(self):
        """Giữ hành vi cũ: Gemini quá giờ → TimeoutError để transcribe_upload trả 504 kèm lý do."""
        fake = FakeGenai({'gemini-3.1-flash-lite': google_api_exceptions.DeadlineExceeded('slow')})
        with self.assertRaises(TimeoutError):
            self._run(fake)
        self.assertEqual(fake.deleted, ['files/abc'])
