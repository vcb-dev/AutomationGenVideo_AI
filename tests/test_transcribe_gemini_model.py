"""Transcribe nghe giọng nói bằng VIDEO_TO_TEXT_GEMINI_MODEL (chung với video → kịch bản) — biến BẮT BUỘC,
code không có model mặc định; khoá dùng chung GEMINI_API_KEY (ảnh thẻ giữ GEMINI_MODEL = model tạo ảnh).

Lỗi gốc: transcribe đọc chung GEMINI_MODEL với ảnh thẻ, mà trên server biến đó là
gemini-3.1-flash-image — model TẠO ẢNH, đầu vào "Text, Image, Video, and PDF", không có âm thanh
(ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image, đọc 2026-09-28). Transcribe khi đó
"nghe" bằng một model không nghe được tiếng. Cũng không thể đổi GEMINI_MODEL sang model chữ: đo
thật, gemini-3.1-flash-lite trả NO_IMAGE nên ảnh thẻ sẽ hỏng. Vì vậy tách biến.

SDK được thay bằng bản giả chỉ để khoá luồng điều khiển của phía mình (đọc biến model, báo lỗi khi
404, khoá API dùng chung). Hành vi thật của SDK (404 → NotFound, model nghe được tiếng) đã kiểm
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
    def test_reads_video_to_text_model_not_gemini_model(self):
        """Dùng đúng VIDEO_TO_TEXT_GEMINI_MODEL (biến môi trường hoặc settings), không theo GEMINI_MODEL = model tạo ảnh."""
        with mock.patch.dict(os.environ, {'VIDEO_TO_TEXT_GEMINI_MODEL': ' gemini-3.5-flash ', 'GEMINI_MODEL': 'gemini-3.1-flash-image'}), \
             override_settings(VIDEO_TO_TEXT_GEMINI_MODEL='', GEMINI_MODEL='gemini-3.1-flash-image'):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-3.5-flash')
        with override_settings(VIDEO_TO_TEXT_GEMINI_MODEL='gemini-3.1-flash-lite'):
            self.assertEqual(transcribe_views._transcribe_model_name(), 'gemini-3.1-flash-lite')

    def test_missing_or_blank_raises_with_variable_name(self):
        """Không có giá trị mặc định trong code: thiếu / chỉ khoảng trắng → lỗi nêu tên biến (transcribe trả 400)."""
        for blank in ('', '   '):
            with self.subTest(value=repr(blank)), mock.patch.dict(os.environ, {'VIDEO_TO_TEXT_GEMINI_MODEL': blank}), \
                 override_settings(VIDEO_TO_TEXT_GEMINI_MODEL='', GEMINI_MODEL='gemini-3.1-flash-image'):
                with self.assertRaisesMessage(ValueError, 'VIDEO_TO_TEXT_GEMINI_MODEL'):
                    transcribe_views._transcribe_model_name()


@override_settings(GEMINI_API_KEY='shared-key', GEMINI_MODEL='gemini-3.1-flash-image', VIDEO_TO_TEXT_GEMINI_MODEL='gemini-3.1-flash-lite')
class TranscribeWithGeminiTests(SimpleTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.mp4')
        os.write(fd, b'\x00' * 1024)
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        env = mock.patch.dict(os.environ, {'VIDEO_TO_TEXT_GEMINI_MODEL': ''})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, fake):
        with fake.patch():
            return transcribe_views.transcribe_with_gemini(self.path)

    def test_listens_with_configured_model_and_shares_gemini_api_key(self):
        """Nghe bằng model cấu hình, không đụng model tạo ảnh của GEMINI_MODEL, dùng chung GEMINI_API_KEY."""
        fake = FakeGenai()
        self.assertEqual(self._run(fake), 'lời thoại từ gemini-3.1-flash-lite')
        self.assertEqual(fake.api_key, 'shared-key')
        self.assertEqual(fake.models, ['gemini-3.1-flash-lite'])
        self.assertEqual(fake.deleted, ['files/abc'])

    def test_missing_gemini_api_key_fails_before_calling_gemini(self):
        """Thiếu GEMINI_API_KEY → báo rõ tên biến, không gọi Gemini."""
        fake = FakeGenai()
        with override_settings(GEMINI_API_KEY=''), mock.patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            with self.assertRaisesMessage(ValueError, 'GEMINI_API_KEY'):
                self._run(fake)
        self.assertEqual(fake.models, [])

    def test_missing_model_variable_fails_before_calling_gemini(self):
        """Thiếu VIDEO_TO_TEXT_GEMINI_MODEL → báo rõ tên biến, không tải file lên / gọi Gemini."""
        fake = FakeGenai()
        with override_settings(VIDEO_TO_TEXT_GEMINI_MODEL=''):
            with self.assertRaisesMessage(ValueError, 'VIDEO_TO_TEXT_GEMINI_MODEL'):
                self._run(fake)
        self.assertEqual(fake.models, [])

    def test_unknown_model_reports_variable_to_fix_without_switching_model(self):
        """Tên model không tồn tại (404) → lỗi nêu tên biến cần sửa, không tự đổi model, vẫn xoá file trên Gemini."""
        # Nguyên văn thông báo Gemini trả cho tên "gemini-3.1-flash" (đo thật 2026-09-28)
        fake = FakeGenai({'gemini-3.1-flash': google_api_exceptions.NotFound(
            'models/gemini-3.1-flash is not found for API version v1beta, or is not supported for generateContent.')})
        with override_settings(VIDEO_TO_TEXT_GEMINI_MODEL='gemini-3.1-flash'):
            with self.assertRaisesMessage(ValueError, "Model Gemini 'gemini-3.1-flash' không dùng được"):
                self._run(fake)
        self.assertEqual(fake.models, ['gemini-3.1-flash'])
        self.assertEqual(fake.deleted, ['files/abc'])

    def test_non_404_errors_are_not_reported_as_config_errors(self):
        """Hết hạn mức (429) không phải lỗi cấu hình — giữ nguyên lỗi gốc, chỉ một lượt gọi."""
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
