import os
import sys
import django
import logging
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.conf import settings

logging.basicConfig(level=logging.DEBUG)

def test_proxy():
    url = "https://scontent-iad3-2.xx.fbcdn.net/v/t39.30808-1/363438572_239967188942673_7930516771941581094_n.jpg?stp=cp0_dst-jpg_s720x720_tt6&_nc_cat=106&ccb=1-7&_nc_sid=2d3e12&_nc_ohc=gVkRl8-C0UcQ7kNvwFLcsYD&_nc_oc=AdmR_tup5Zg4YG2DQv07TyqxHxg-TXCH03SUKS2m-zlC5Ko_WvRIJJUp7il31s8BSoM&_nc_zt=24&_nc_ht=scontent-iad3-2.xx&_nc_gid=2AYthEMJQFyTIA9IG1rCIg&_nc_ss=8&oh=00_AfvbfMb6nHAQJk8vvG1iq2Hghc6o19tuiq6ts6j-5gUKEw&oe=69A84360"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.facebook.com/',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    api_token = getattr(settings, 'APIFY_API_TOKEN', '') or ''
    proxies = None
    if api_token:
        # Note: apify proxy uses proxy_password, which is often a proxy password, not the API token... Oh wait, you can use proxy password.
        # Check if the user used the API token correctly.
        print("Using proxy with token: " + api_token[:5] + "...")
        proxies = {
            'http': f'http://groups-RESIDENTIAL:{api_token}@proxy.apify.com:8000',
            'https': f'http://groups-RESIDENTIAL:{api_token}@proxy.apify.com:8000',
        }
    
    try:
        resp = requests.get(url, timeout=15, headers=headers, proxies=proxies)
        print("Status code (proxy):", resp.status_code)
        print("Headers:", resp.headers)
    except Exception as e:
        print("Proxy test failed:", e)

    print("Trying without proxy:")
    try:
        resp2 = requests.get(url, timeout=15, headers=headers)
        print("Status code (no proxy):", resp2.status_code)
        print("Headers:", resp2.headers)
    except Exception as e:
        print("No proxy test failed:", e)

if __name__ == '__main__':
    test_proxy()
