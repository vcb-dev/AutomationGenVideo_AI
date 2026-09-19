import os
import tempfile
from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, RequestFactory
from video_management.services.minimax_tts_service import (
    format_srt_timestamp,
    normalize_subtitle_entry,
    subtitles_to_srt,
    split_sentences_for_tts,
)
from video_management.views.voice_views import serve_minimax_tts_file, _TTS_FILENAME_RE


class MinimaxTtsSubtitlesTest(SimpleTestCase):
    """Kiểm tra logic tạo phụ đề SRT và serve file SRT của Minimax TTS."""

    def test_format_srt_timestamp(self):
        self.assertEqual(format_srt_timestamp(0), "00:00:00,000")
        self.assertEqual(format_srt_timestamp(654.6), "00:00:00,655")
        self.assertEqual(format_srt_timestamp(125430), "00:02:05,430")
        self.assertEqual(format_srt_timestamp(3661005), "01:01:01,005")

    def test_normalize_subtitle_entry(self):
        # Format kiểu WebSocket / Extension cũ
        e1 = {"text": "Xin chào các bạn", "time_begin": 0, "time_end": 1500}
        norm1 = normalize_subtitle_entry(e1)
        self.assertIsNotNone(norm1)
        self.assertEqual(norm1["text"], "Xin chào các bạn")
        self.assertEqual(norm1["start_ms"], 0.0)
        self.assertEqual(norm1["end_ms"], 1500.0)

        # Format kiểu start_time / end_time
        e2 = {"content": "Chào mừng đến với Viễn Chí Bảo", "start_time": 1550.2, "end_time": 3200.8}
        norm2 = normalize_subtitle_entry(e2)
        self.assertIsNotNone(norm2)
        self.assertEqual(norm2["text"], "Chào mừng đến với Viễn Chí Bảo")
        self.assertEqual(norm2["start_ms"], 1550.2)
        self.assertEqual(norm2["end_ms"], 3200.8)

        # Entry không hợp lệ (thiếu text hoặc timing)
        self.assertIsNone(normalize_subtitle_entry({}))
        self.assertIsNone(normalize_subtitle_entry({"text": "Hello"}))
        self.assertIsNone(normalize_subtitle_entry({"time_begin": 0, "time_end": 100}))

    def test_subtitles_to_srt_formatting(self):
        subs = [
            {"text": "Câu thứ hai", "time_begin": 2000, "time_end": 3500},
            {"text": "Câu thứ nhất", "time_begin": 0, "time_end": 1800},
        ]
        srt = subtitles_to_srt(subs)
        # Kiểm tra tự động sắp xếp theo start_time
        expected_lines = [
            "1",
            "00:00:00,000 --> 00:00:01,800",
            "Câu thứ nhất",
            "",
            "2",
            "00:00:02,000 --> 00:00:03,500",
            "Câu thứ hai",
        ]
        for line in expected_lines:
            self.assertIn(line, srt)

    def test_subtitles_to_srt_empty(self):
        self.assertEqual(subtitles_to_srt([]), "")
        self.assertEqual(subtitles_to_srt(None), "")
        self.assertEqual(subtitles_to_srt([{"invalid": 123}]), "")

    def test_tts_filename_regex_allows_mp3_and_srt(self):
        valid_mp3 = "tts_1234567890abcdef1234567890abcdef.mp3"
        valid_srt = "tts_1234567890abcdef1234567890abcdef.srt"
        invalid_ext = "tts_1234567890abcdef1234567890abcdef.wav"
        invalid_name = "sample.srt"

        self.assertTrue(bool(_TTS_FILENAME_RE.match(valid_mp3)))
        self.assertTrue(bool(_TTS_FILENAME_RE.match(valid_srt)))
        self.assertFalse(bool(_TTS_FILENAME_RE.match(invalid_ext)))
        self.assertFalse(bool(_TTS_FILENAME_RE.match(invalid_name)))

    def test_serve_minimax_tts_file_srt(self):
        factory = RequestFactory()
        req = factory.get("/api/voice/tts/file/tts_0123456789abcdef0123456789abcdef.srt")
        srt_filename = "tts_0123456789abcdef0123456789abcdef.srt"

        with tempfile.TemporaryDirectory() as tmpdir:
            minimax_dir = os.path.join(tmpdir, "minimax_tts")
            os.makedirs(minimax_dir, exist_ok=True)
            srt_path = os.path.join(minimax_dir, srt_filename)
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n")

            with patch("video_management.views.voice_views.default_storage") as mock_storage:
                mock_storage.location = tmpdir
                resp = serve_minimax_tts_file(req, srt_filename)
                self.assertEqual(resp.status_code, 200)
                self.assertIn("application/x-subrip", resp["Content-Type"])
                self.assertIn("utf-8", resp["Content-Type"])

    def test_generate_audio_saves_srt_file(self):
        from video_management.services.minimax_tts_service import MinimaxTTSService

        service = MinimaxTTSService(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            output_mp3 = os.path.join(tmpdir, "tts_test.mp3")
            mock_response_data = {
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {
                    "audio": "494433",  # hex for "ID3"
                    "subtitles": [
                        {"text": "Test phrase", "time_begin": 0, "time_end": 1000}
                    ]
                },
                "extra_info": {
                    "audio_length": 1.0,
                }
            }

            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = mock_response_data
                mock_post.return_value = mock_resp

                res = service.generate_audio(
                    text="Test phrase",
                    voice_id="voice_123",
                    output_path=output_mp3
                )

                self.assertTrue(res["success"])
                self.assertIsNotNone(res["srt_content"])
                expected_srt_path = os.path.join(tmpdir, "tts_test.srt")
                self.assertEqual(res["srt_file_path"], expected_srt_path)
                self.assertTrue(os.path.exists(expected_srt_path))
                with open(expected_srt_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("Test phrase", content)

    def test_split_sentences_for_tts(self):
        # 1. Đoạn văn nhiều câu trên 1 dòng -> tự động tách thành nhiều dòng \n
        raw = "xin chào mọi người. mình là Huy ca đến từ viễn chí bào. rất vui được làm quen mọi người . chào mọi người"
        res = split_sentences_for_tts(raw)
        lines = res.split("\n")
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "xin chào mọi người.")
        self.assertEqual(lines[1], "mình là Huy ca đến từ viễn chí bào.")
        self.assertEqual(lines[2], "rất vui được làm quen mọi người.")
        self.assertEqual(lines[3], "chào mọi người")

        # 2. Không làm hỏng số tiền/thập phân và viết tắt
        text_with_number = "Sản phẩm 1.500.000đ phiên bản 2.0. Rất tốt!"
        res2 = split_sentences_for_tts(text_with_number)
        lines2 = res2.split("\n")
        self.assertEqual(len(lines2), 2)
        self.assertIn("1.500.000đ phiên bản 2.0.", lines2[0])
        self.assertEqual(lines2[1], "Rất tốt!")

        # 3. Tách theo dấu phẩy (,), hai chấm (:), chấm phẩy (;)
        comma_text = "Hơn mười năm làm nghề kim hoàn, điều HuyK sợ nhất không phải làm hỏng một món trang sức, mà là làm mất lòng tin của một người khách."
        res3 = split_sentences_for_tts(comma_text)
        lines3 = res3.split("\n")
        self.assertEqual(len(lines3), 3)
        self.assertEqual(lines3[0], "Hơn mười năm làm nghề kim hoàn,")
        self.assertEqual(lines3[1], "điều HuyK sợ nhất không phải làm hỏng một món trang sức,")
        self.assertEqual(lines3[2], "mà là làm mất lòng tin của một người khách.")

        # 4. Không tách dấu phẩy trong số (1,500,000)
        number_comma = "Giá 1,500,000đ phiên bản 2.0, rất đáng tiền."
        res4 = split_sentences_for_tts(number_comma)
        lines4 = res4.split("\n")
        self.assertEqual(len(lines4), 2)
        self.assertIn("1,500,000đ", lines4[0])
        self.assertEqual(lines4[1], "rất đáng tiền.")

        # 5. Văn bản đã có sẵn dấu xuống dòng được giữ nguyên cấu trúc
        multiline = "Dòng một.\nDòng hai.\nDòng ba."
        self.assertEqual(split_sentences_for_tts(multiline), multiline)



