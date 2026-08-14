"""Chức năng: chỉ cho yt-dlp đúng chỗ có ffmpeg, kể cả khi binary không tên "ffmpeg".

Vì sao đáng một file test riêng: `--ffmpeg-location` nhận HOẶC đường dẫn binary HOẶC thư mục
chứa nó — nhưng truyền thư mục thì yt-dlp đi tìm một file tên đúng `ffmpeg` trong đó. Bản build
đang dùng là `imageio_ffmpeg`, nó đặt tên binary theo nền tảng:

    venv/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-x86_64-v7.1

Truyền thư mục đó vào là yt-dlp không thấy `ffmpeg` nào cả. Hậu quả đo được ngày 13/08/2026:
video 720p tải về thành HAI mảnh rời (`.f395.mp4` chỉ hình, `.f251.webm` chỉ tiếng), không ghép,
và yt-dlp vẫn **thoát mã 0** nên không chỗ nào biết là đã hỏng. Truyền thẳng đường dẫn binary
thì yt-dlp chạy `[Merger] Merging formats into ...` và ra đúng một file mp4 hoàn chỉnh.

Đây là gốc của chuỗi lỗi "tải xong 100% rồi bấm tải về thì báo file không tồn tại".
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

import tempfile  # noqa: E402
from django.test import SimpleTestCase  # noqa: E402

from video_management.views.video_downloader_views import ytdlp_ffmpeg_args  # noqa: E402


class ChiDungChoFfmpeg(SimpleTestCase):
    def test_tro_thang_vao_binary_chu_khong_phai_thu_muc_cha(self):
        with tempfile.TemporaryDirectory() as thu_muc:
            # Đúng cách imageio_ffmpeg đặt tên — thư mục KHÔNG có file nào tên "ffmpeg".
            binary = os.path.join(thu_muc, 'ffmpeg-macos-x86_64-v7.1')
            open(binary, 'wb').close()

            args = ytdlp_ffmpeg_args(binary)

            self.assertEqual(args[0], '--ffmpeg-location')
            self.assertEqual(args[1], binary)
            # Chốt lại đúng cái đã làm hỏng: truyền thư mục là yt-dlp mù.
            self.assertNotEqual(args[1], thu_muc)
            self.assertTrue(os.path.isfile(args[1]))

    def test_khong_co_ffmpeg_thi_khong_them_co_de_ytdlp_tu_do_PATH(self):
        self.assertEqual(ytdlp_ffmpeg_args(''), [])
