"""transcribe_with_gemini phải CẮT mỗi lần gọi generate_content ở GEMINI_GENERATE_MAX_PER_CALL
rồi GỌI LẠI, thay vì dồn cả ngân sách vào 1 lệnh gọi.

Vì sao: đo thật — cùng 1 file 30s, lần đầu Gemini "câm" >889s rồi mới lỗi, lần thử lại xong
trong 61s. Bản cũ cho generate_content timeout = TOÀN BỘ ngân sách còn lại (tới ~889s) và chỉ
gọi 1 lần → 1 lượt stall của Gemini kéo job chạy 15 phút.

Khoá:
  1. per_call timeout luôn <= GEMINI_GENERATE_MAX_PER_CALL (không bao giờ = cả ngân sách).
  2. DeadlineExceeded ở lần gọi này → GỌI LẠI (không raise ngay).
  3. Hết ngân sách sau vài lần thử → raise TimeoutError kèm số lần đã thử.
  4. heartbeat được gọi mỗi lần thử.

Chạy: python manage.py test tests.test_transcribe_gemini_retry
"""
import sys
import time
import types
from unittest import mock

from django.test import SimpleTestCase, override_settings


class _FakeState:
    name = "ACTIVE"


class _FakeFile:
    name = "files/fake123"
    state = _FakeState()


class _DeadlineExceeded(Exception):
    pass


class _RetryError(Exception):
    pass


def _install_fake_genai(generate_side_effects):
    """Cài google.generativeai + google.api_core.exceptions giả. Trả về list ghi lại
    request_options['timeout'] của từng lần gọi generate_content."""
    timeouts = []

    class _FakeModel:
        def __init__(self, *a, **k):
            pass

        def generate_content(self, _parts, request_options=None):
            timeouts.append((request_options or {}).get("timeout"))
            eff = generate_side_effects.pop(0)
            if isinstance(eff, Exception):
                raise eff
            resp = mock.Mock()
            resp.text = eff
            return resp

    genai = types.ModuleType("google.generativeai")
    genai.configure = lambda **k: None
    genai.upload_file = lambda *a, **k: _FakeFile()
    genai.get_file = lambda name: _FakeFile()
    genai.delete_file = lambda name: None
    genai.GenerativeModel = _FakeModel

    api_core = types.ModuleType("google.api_core")
    exc_mod = types.ModuleType("google.api_core.exceptions")
    exc_mod.DeadlineExceeded = _DeadlineExceeded
    exc_mod.RetryError = _RetryError
    api_core.exceptions = exc_mod

    return genai, api_core, exc_mod, timeouts


@override_settings(GEMINI_API_KEY="fake-key", GEMINI_MODEL="gemini-2.5-flash")
class GeminiRetryTests(SimpleTestCase):
    def _run(self, side_effects, deadline_offset=600):
        genai, api_core, exc_mod, timeouts = _install_fake_genai(list(side_effects))
        beats = []
        mods = {
            "google.generativeai": genai,
            "google.api_core": api_core,
            "google.api_core.exceptions": exc_mod,
        }
        with mock.patch.dict(sys.modules, mods), \
             mock.patch("video_management.views.transcribe_views.os.path.getsize", return_value=2 * 1024 * 1024):
            from video_management.views import transcribe_views as tv
            result = tv.transcribe_with_gemini(
                "/tmp/fake.mp4",
                deadline=time.time() + deadline_offset,
                heartbeat=lambda msg=None: beats.append(msg),
            )
        return result, timeouts, beats

    def test_per_call_timeout_khong_bao_gio_vuot_max_per_call(self):
        from video_management.views import transcribe_views as tv
        result, timeouts, _ = self._run(["transcript ok"], deadline_offset=5000)
        self.assertEqual(result, "transcript ok")
        self.assertEqual(len(timeouts), 1)
        self.assertLessEqual(timeouts[0], tv.GEMINI_GENERATE_MAX_PER_CALL)

    def test_deadline_exceeded_thi_goi_lai_roi_thanh_cong(self):
        result, timeouts, beats = self._run(
            [_DeadlineExceeded("stall"), _DeadlineExceeded("stall"), "xong ở lần 3"],
            deadline_offset=5000,
        )
        self.assertEqual(result, "xong ở lần 3")
        self.assertEqual(len(timeouts), 3)  # đã gọi lại 2 lần
        self.assertEqual(len([b for b in beats if b and "lần thử" in b]), 3)

    def test_khong_du_ngan_sach_cho_generate_thi_raise_timeout(self):
        # Ngân sách quá ít để sinh transcript → raise TimeoutError (không lao vào gọi generate
        # với timeout bé tới mức chắc chắn fail).
        with self.assertRaises(TimeoutError):
            self._run([_DeadlineExceeded("x")] * 5, deadline_offset=20)

    def test_deadline_exceeded_roi_het_ngan_sach_giua_chung(self):
        # generate_content "tiêu" thời gian thật (fake sleep) → sau 1 lần thử, _remaining tụt
        # dưới sàn → raise thay vì gọi lại vô hạn.
        def _slow_deadline(*_a, **_k):
            time.sleep(2.0)
            raise _DeadlineExceeded("stall")

        genai, api_core, exc_mod, timeouts = _install_fake_genai([])
        genai.GenerativeModel = lambda *a, **k: types.SimpleNamespace(generate_content=_slow_deadline)
        mods = {
            "google.generativeai": genai,
            "google.api_core": api_core,
            "google.api_core.exceptions": exc_mod,
        }
        with mock.patch.dict(sys.modules, mods), \
             mock.patch("video_management.views.transcribe_views.os.path.getsize", return_value=2 * 1024 * 1024):
            from video_management.views import transcribe_views as tv
            with self.assertRaises(TimeoutError):
                tv.transcribe_with_gemini("/tmp/fake.mp4", deadline=time.time() + 36, heartbeat=None)
