"""
Lấy thông tin + link video thật từ Douyin bằng Chromium headless (Playwright).

Douyin ký (a_bogus/x-secsdk-web-signature) MỌI request gọi API chi tiết video bằng
JavaScript chạy trong trình duyệt — yt-dlp tự gọi API đó mà không tính được chữ ký
nên luôn bị từ chối ("Fresh cookies... needed", xem chính TODO trong code yt-dlp).
Cách né: để CHÍNH trang Douyin tự gọi API đó (tự tính chữ ký đúng), ta chỉ "nghe trộm"
response mạng thành công (status 200) rồi tự đọc JSON lấy link video thật
(video.play_addr.url_list) — không cần yt-dlp cho bước này nữa.

Chạy như 1 script độc lập (không import trực tiếp vào Django) vì Playwright's sync
API không an toàn khi chạy trong thread phụ — process riêng có main thread riêng,
né hẳn vấn đề đó.

Usage: python douyin_extract.py <url> <output_json_file>
Exit 0 + ghi JSON {title, thumbnail, duration_ms, uploader, video_url} nếu thành công.
Exit khác + in lỗi ra stderr nếu thất bại.
"""
import json
import sys
import time

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)
NAV_TIMEOUT_MS = 30_000
EXTRA_WAIT_MS = 20_000  # trang có thể thử lại vài lần (403 rồi mới 200) trước khi ký đúng


def main():
    if len(sys.argv) != 3:
        print('Usage: python douyin_extract.py <url> <output_json_file>', file=sys.stderr)
        sys.exit(2)
    url, out_path = sys.argv[1], sys.argv[2]

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    captured = {}

    def on_response(response):
        if captured.get('done'):
            return
        if 'aweme/v1/web/aweme/detail' not in response.url:
            return
        if response.status != 200:
            return
        try:
            body = json.loads(response.text())
        except Exception:
            return
        detail = (body or {}).get('aweme_detail')
        if not detail:
            return
        video = detail.get('video') or {}
        play_addr = video.get('play_addr') or {}
        url_list = play_addr.get('url_list') or []
        if not url_list:
            return
        cover = (video.get('cover') or {}).get('url_list') or []
        captured.update({
            'done': True,
            'title': detail.get('desc') or f"douyin_{detail.get('aweme_id', '')}",
            'thumbnail': cover[0] if cover else None,
            'duration_ms': video.get('duration'),
            'uploader': ((detail.get('author') or {}).get('nickname')),
            'video_url': url_list[0],
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale='vi-VN')
            Stealth().apply_stealth_sync(context)
            page = context.new_page()
            page.on('response', on_response)
            # Chờ 'domcontentloaded' chứ KHÔNG chờ 'load': trang Douyin tải thêm tài nguyên liên
            # tục nên sự kiện 'load' không tới trong 30s (đo trên image Railway), goto ném lỗi và
            # script thoát dù API chi tiết video đã về. Hết giờ điều hướng cũng không bỏ cuộc —
            # thứ cần là response của API, không phải trang tải xong.
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=NAV_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001
                print(f'goto: {e.__class__.__name__}', file=sys.stderr)
            # Phải chờ bằng page.wait_for_timeout: API đồng bộ của Playwright chỉ xử lý sự kiện
            # (on_response) khi được trao quyền điều khiển. time.sleep chặn luôn việc đó nên
            # bản cũ chờ đủ 20s mà không bao giờ nhận được response.
            deadline = time.time() + EXTRA_WAIT_MS / 1000
            while not captured.get('done') and time.time() < deadline:
                page.wait_for_timeout(300)
        finally:
            browser.close()

    if not captured.get('done'):
        print('Could not capture a successful video detail response.', file=sys.stderr)
        sys.exit(1)

    captured.pop('done', None)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(captured, f, ensure_ascii=False)
    print(f'Extracted info written to {out_path}')


if __name__ == '__main__':
    main()
