"""
Lấy TikTok Organic Access Token qua OAuth 2.0 (Business API)
Chạy: python get_organic_token.py

TRƯỚC KHI CHẠY — đăng ký redirect URI trong TikTok Developer App:
  1. Vào https://developers.tiktok.com/apps/
  2. Chọn app ID 7629607469592870929
  3. Mục "Redirect URI" → thêm: http://localhost:8080/callback
  4. Lưu lại rồi mới chạy script này
"""
import os, sys, requests, urllib.parse, threading, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv, set_key

ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)

APP_ID       = os.getenv('TIKTOK_APP_ID',     '7629607469592870929')
APP_SECRET   = os.getenv('TIKTOK_APP_SECRET', '158587a761bfefcd2799a585c98c2134aa38b95a')
REDIRECT_URI = 'http://localhost:8080/callback'
PORT         = 8080

# Biến toàn cục nhận auth_code từ callback
_auth_code = None


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code   = params.get('code', [None])[0]
        error  = params.get('error', [None])[0]

        if code:
            _auth_code = code
            body = b'<h2 style="font-family:sans-serif;color:green">OK! Token dang duoc xu ly...<br>Quay lai terminal.</h2>'
        else:
            body = f'<h2 style="font-family:sans-serif;color:red">Loi: {error}</h2>'.encode()

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # tắt log HTTP


def exchange_token(auth_code: str) -> dict:
    # Thử lần lượt các endpoint TikTok Business API
    endpoints = [
        'https://business-api.tiktok.com/open_api/v1.3/oauth2/creator_token/',
        'https://business-api.tiktok.com/open_api/v1.3/tt_user/oauth2/token/',
        'https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/',
    ]
    bc_id = os.getenv('TIKTOK_BC_ID', '7274810417535270913')
    print(f"    BC_ID       : '{bc_id}'")
    print(f"    auth_code   : '{auth_code[:20]}...'")

    # Thử cả 2 tên field TikTok có thể dùng
    payload = {
        'app_id':      APP_ID,
        'secret':      APP_SECRET,
        'auth_code':   auth_code,
        'business':    bc_id,
        'bc_id':       bc_id,
        'business_id': bc_id,
    }
    print(f"    Payload keys: {list(payload.keys())}")
    for url in endpoints:
        print(f"    Thử endpoint: {url}")
        r = requests.post(url, json=payload,
                          headers={'Content-Type': 'application/json'}, timeout=15)
        print(f"    HTTP status : {r.status_code}")
        print(f"    Raw response: {r.text[:300]}")
        if r.status_code == 404:
            continue
        try:
            return r.json()
        except Exception:
            return {'code': -1, 'message': f'Response không phải JSON: {r.text[:200]}'}
    return {'code': -1, 'message': 'Tất cả endpoints đều trả về 404'}


def main():
    global _auth_code

    print("=" * 60)
    print("  TikTok Business API — Lấy Organic Access Token")
    print("=" * 60)
    print()
    print("⚠  QUAN TRỌNG: Trước khi chạy, đảm bảo đã đăng ký URI này")
    print(f"   trong TikTok Developer App settings:")
    print(f"   http://localhost:8080/callback")
    print()
    print("   https://developers.tiktok.com/apps/ → chọn app → Redirect URI")
    print()
    input("Đã đăng ký redirect URI rồi? Nhấn Enter để tiếp tục...")

    # Build auth URL
    params = {
        'app_id':       APP_ID,
        'redirect_uri': REDIRECT_URI,
        'state':        'vcb_organic',
    }
    auth_url = 'https://business-api.tiktok.com/portal/auth?' + urllib.parse.urlencode(params)

    # Khởi động local HTTP server để bắt callback
    server = HTTPServer(('localhost', PORT), CallbackHandler)
    t = threading.Thread(target=server.handle_request)
    t.daemon = True
    t.start()

    print(f"\nĐang mở trình duyệt để đăng nhập TikTok...")
    print(f"Nếu không tự mở, copy URL này vào trình duyệt:")
    print(f"{auth_url}\n")
    webbrowser.open(auth_url)

    print("Chờ bạn đăng nhập và đồng ý quyền...")
    t.join(timeout=120)  # chờ tối đa 2 phút

    if not _auth_code:
        print("[!] Hết thời gian chờ hoặc không nhận được auth_code.")
        print("    Kiểm tra lại redirect URI đã được đăng ký chưa.")
        sys.exit(1)

    print(f"\nĐang đổi auth_code lấy token...")
    result = exchange_token(_auth_code)

    if result.get('code') != 0:
        print(f"[!] Lỗi đổi token: {result.get('message')} (code: {result.get('code')})")
        print(f"    Chi tiết: {result}")
        sys.exit(1)

    data    = result.get('data', {})
    token   = data.get('access_token', '')
    open_id = data.get('open_id', '')
    expires = data.get('expires_in', 0)

    print(f"\n✓ Lấy token thành công!")
    print(f"  open_id    : {open_id}")
    print(f"  expires_in : {expires}s (~{expires//3600}h)")
    print(f"  token      : {token[:20]}...")

    set_key(ENV_PATH, 'TIKTOK_ORGANIC_TOKEN',   token)
    set_key(ENV_PATH, 'TIKTOK_ORGANIC_OPEN_ID', open_id)
    print(f"\n✓ Đã lưu TIKTOK_ORGANIC_TOKEN và TIKTOK_ORGANIC_OPEN_ID vào .env")
    print("\n[Lưu ý] Token organic gắn với 1 TikTok account.")
    print("        Nếu nhiều kênh thuộc nhiều account khác nhau,")
    print("        mỗi account cần chạy lại script và đăng nhập riêng.")


if __name__ == '__main__':
    main()
