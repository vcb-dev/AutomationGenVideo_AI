import os, requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('TIKTOK_ADS_ACCESS_TOKEN') or os.getenv('TIKTOK_ACCESS_TOKEN')

print(f"[*] Kiểm tra Token: {token[:10]}...")

# Thử nghiệm các endpoint khác nhau của TikTok
endpoints = [
    "https://business-api.tiktok.com/open_api/v1.3/advertiser/info/",
    "https://business-api.tiktok.com/open_api/v1.3/user/info/"
]

for url in endpoints:
    print(f"\n--- Thử với: {url} ---")
    try:
        # Thử cả 2 cách gửi token (Header và Query)
        res = requests.get(url, params={"access_token": token}, headers={"Access-Token": token}).json()
        print(f"Mã lỗi (Code): {res.get('code')}")
        print(f"Thông báo: {res.get('message')}")
        if res.get('data'):
            print(f"Dữ liệu nhận được: {res.get('data')}")
    except Exception as e:
        print(f"Lỗi kết nối: {e}")
