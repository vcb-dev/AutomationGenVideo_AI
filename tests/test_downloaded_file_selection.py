"""Chức năng: chỉ nhận file đã ghép hoàn chỉnh, không nhận mảnh tạm của yt-dlp.

Vì sao đáng một file test riêng: khi ghép hỏng, yt-dlp để lại các MẢNH mang tên
`<out_base>.f<id>.<ext>` — ví dụ `vcb_dl_<job>.f395.mp4` (chỉ hình) và `vcb_dl_<job>.f251.webm`
(chỉ tiếng) — rồi vẫn thoát mã 0.

Bản cũ chọn file bằng `endswith('.mp4')`, mà `vcb_dl_<job>.f395.mp4` cũng kết thúc bằng `.mp4`,
nên nó nhận nguyên cái mảnh câm đó làm "video hoàn chỉnh", xoá mảnh tiếng đi rồi báo job `done`.
Sau đó `download_file` lại tự dựng đường dẫn theo luật KHÁC (`<out_base>.mp4` đúng nghĩa đen),
không thấy file, trả 410.

Người dùng thấy: chạy tới 100% "Hoàn tất" rồi bấm tải về thì báo file không tồn tại. Tái hiện
được ngày 13/08/2026 với YouTube 720p.

Hai chỗ tự suy ra đường dẫn theo hai luật khác nhau là gốc của cả lớp lỗi này — nên cả hai giờ
dùng CHUNG một hàm. Test khoá vào chính hàm chung đó.
"""

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

import tempfile  # noqa: E402
from django.test import SimpleTestCase  # noqa: E402

from video_management.views.video_downloader_views import finished_media_path  # noqa: E402


class ChiNhanFileDaGhepXong(SimpleTestCase):
    def setUp(self):
        self.thu_muc = tempfile.TemporaryDirectory()
        self.addCleanup(self.thu_muc.cleanup)
        self.out_base = os.path.join(self.thu_muc.name, 'vcb_dl_abc123')

    def tao(self, duoi: str) -> str:
        path = self.out_base + duoi
        with open(path, 'wb') as f:
            f.write(b'0' * 2048)
        return path

    def test_manh_hinh_cua_ytdlp_khong_duoc_tinh_la_video_hoan_chinh(self):
        # Đúng ca đã làm hỏng: mảnh này cũng kết thúc bằng ".mp4".
        self.tao('.f395.mp4')
        self.tao('.f251.webm')

        self.assertIsNone(finished_media_path(self.out_base, 'mp4'))

    def test_tra_ve_file_da_ghep_khi_co(self):
        hoan_chinh = self.tao('.mp4')

        self.assertEqual(finished_media_path(self.out_base, 'mp4'), hoan_chinh)

    def test_co_ca_manh_lan_file_ghep_thi_lay_file_ghep(self):
        self.tao('.f395.mp4')
        hoan_chinh = self.tao('.mp4')

        self.assertEqual(finished_media_path(self.out_base, 'mp4'), hoan_chinh)

    def test_khong_co_gi_thi_tra_none(self):
        self.assertIsNone(finished_media_path(self.out_base, 'mp4'))

    def test_mp3_cung_mot_luat(self):
        self.tao('.f251.mp3')

        self.assertIsNone(finished_media_path(self.out_base, 'mp3'))

        hoan_chinh = self.tao('.mp3')
        self.assertEqual(finished_media_path(self.out_base, 'mp3'), hoan_chinh)
