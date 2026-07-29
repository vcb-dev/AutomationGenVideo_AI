"""
Sinh cookie chống bot cho Douyin bằng cách mở thật 1 trang Douyin trong Chromium
headless (Playwright) — Douyin bắt buộc cookie được sinh bởi JavaScript chạy trong
trình duyệt thật, không công cụ dòng lệnh nào (yt-dlp, curl...) tự tạo được.

Chạy như 1 script độc lập (không import trực tiếp vào Django) vì Playwright's sync
API không an toàn khi chạy trong thread phụ — process riêng có main thread riêng,
né hẳn vấn đề đó.

Usage: python douyin_cookie_gen.py <url> <output_cookie_file.txt>
Exit 0 + ghi file Netscape cookie jar nếu thành công. Exit khác + in lỗi ra stderr
nếu thất bại (timeout, trang chặn, Chromium lỗi...).
"""
import sys
import time

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)
NAV_TIMEOUT_MS = 30_000
EXTRA_WAIT_MS = 5_000  # cho JS challenge sinh cookie kịp chạy sau khi trang load xong


def _write_netscape_cookies(cookies, out_path):
    lines = ['# Netscape HTTP Cookie File']
    for c in cookies:
        domain = c.get('domain', '')
        flag = 'TRUE' if domain.startswith('.') else 'FALSE'
        path = c.get('path', '/')
        secure = 'TRUE' if c.get('secure') else 'FALSE'
        expires = int(c['expires']) if c.get('expires') and c['expires'] > 0 else 0
        name = c.get('name', '')
        value = c.get('value', '')
        lines.append('\t'.join([domain, flag, path, secure, str(expires), name, value]))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def main():
    if len(sys.argv) != 3:
        print('Usage: python douyin_cookie_gen.py <url> <output_cookie_file>', file=sys.stderr)
        sys.exit(2)
    url, out_path = sys.argv[1], sys.argv[2]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale='vi-VN')
            page = context.new_page()
            page.goto(url, wait_until='load', timeout=NAV_TIMEOUT_MS)
            time.sleep(EXTRA_WAIT_MS / 1000)
            cookies = context.cookies()
            if not cookies:
                print('No cookies obtained from page.', file=sys.stderr)
                sys.exit(1)
            _write_netscape_cookies(cookies, out_path)
            print(f'Wrote {len(cookies)} cookies to {out_path}')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
