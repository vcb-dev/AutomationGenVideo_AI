"""transcribe gửi AUDIO-ONLY lên Gemini thay vì cả video.

Vì sao: video HEVC 1080x1920 (dọc) khiến Gemini phải giải mã + lập chỉ mục khung hình —
đo thật: cùng 1 file 30s, gửi cả video stall >889s qua NHIỀU lần thử; lượt gửi lại khác
xong trong 61s. Transcribe chỉ cần tiếng nói → tách track audio ra mp3 mono 16k trước khi
upload cắt bỏ hẳn khâu xử lý video + giảm upload ~20-50 lần.

Khoá:
  1. Trích audio thành công → transcribe_with_gemini nhận PATH AUDIO, không phải file gốc;
     file audio tạm được dọn sau đó.
  2. Trích audio thất bại (file không có audio / ffmpeg lỗi) → FALLBACK gửi file gốc,
     không làm hỏng luồng.
  3. extract_audio_for_gemini: rc!=0 / file rỗng / timeout → trả False.

Chạy: python manage.py test tests.test_transcribe_audio_extract
"""
import os
import subprocess
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from video_management.views import transcribe_views as tv


class ExtractAudioTests(SimpleTestCase):
    def test_rc_khac_0_tra_false(self):
        with mock.patch.object(tv.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="no audio")):
            self.assertFalse(tv.extract_audio_for_gemini("/in.mp4", "ffmpeg", "/out.mp3"))

    def test_file_rong_tra_false(self):
        with mock.patch.object(tv.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")), \
             mock.patch.object(tv.os.path, "exists", return_value=True), \
             mock.patch.object(tv.os.path, "getsize", return_value=100):  # < 500 byte
            self.assertFalse(tv.extract_audio_for_gemini("/in.mp4", "ffmpeg", "/out.mp3"))

    def test_timeout_tra_false_khong_nem(self):
        with mock.patch.object(tv.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 180)):
            self.assertFalse(tv.extract_audio_for_gemini("/in.mp4", "ffmpeg", "/out.mp3"))

    def test_thanh_cong_tra_true(self):
        with mock.patch.object(tv.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")), \
             mock.patch.object(tv.os.path, "exists", return_value=True), \
             mock.patch.object(tv.os.path, "getsize", return_value=250_000):
            self.assertTrue(tv.extract_audio_for_gemini("/in.mp4", "ffmpeg", "/out.mp3"))


class CoreUsesAudioTests(SimpleTestCase):
    def _run_core(self, extract_ok: bool):
        sent = {}

        def fake_gemini(path, deadline=None, heartbeat=None):
            sent["path"] = path
            return "  transcript nè  "

        real_input = os.path.join(tempfile.gettempdir(), "vcb_test_core.mp4")
        with open(real_input, "wb") as f:
            f.write(b"x" * 4096)

        try:
            with mock.patch.object(tv, "_get_media_duration", return_value=30.0), \
                 mock.patch.object(tv, "extract_audio_for_gemini", return_value=extract_ok) as ext, \
                 mock.patch.object(tv, "transcribe_with_gemini", side_effect=fake_gemini), \
                 mock.patch.object(tv, "_normalize_transcript_vi", side_effect=lambda s: s.strip()):
                # nếu extract_ok, giả lập file audio tồn tại để nhánh dọn chạy
                if extract_ok:
                    audio_p = os.path.splitext(real_input)[0] + ".gemini16k.mp3"
                    open(audio_p, "wb").write(b"a" * 1000)
                res = tv.run_transcribe_upload_core(
                    real_input, "ffmpeg",
                    deadline=None, request_started_at=0.0, total_budget=480,
                )
                return res, sent, ext
        finally:
            for p in (real_input, os.path.splitext(real_input)[0] + ".gemini16k.mp3"):
                if os.path.exists(p):
                    os.remove(p)

    def test_trich_audio_ok_thi_gui_path_audio_len_gemini_va_don_file(self):
        res, sent, ext = self._run_core(extract_ok=True)
        self.assertTrue(res["success"])
        self.assertTrue(sent["path"].endswith(".gemini16k.mp3"))
        ext.assert_called_once()
        # file audio tạm đã bị dọn
        self.assertFalse(os.path.exists(sent["path"]))

    def test_trich_audio_that_bai_thi_fallback_gui_file_goc(self):
        res, sent, _ = self._run_core(extract_ok=False)
        self.assertTrue(res["success"])
        self.assertTrue(sent["path"].endswith(".mp4"))
