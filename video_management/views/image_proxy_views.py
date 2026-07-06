"""
Image proxy to bypass CORS/referrer when loading external thumbnails (e.g. Instagram CDN).
"""

import logging
from urllib.parse import urlparse

from django.http import HttpResponse, HttpResponseBadRequest
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_GET

import requests

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = (
    'cdninstagram.com',
    'instagram.com',
    'fbcdn.net',
    'fbcdn.com',
    'cdn.fbsbx.com',
    'fbsbx.com',
    'xx.fbcdn.net',
    'graph.facebook.com',
)


def is_allowed_url(url: str) -> bool:
    """Chỉ cho phép proxy ảnh từ CDN Instagram/Facebook."""
    if not url or not url.startswith(('http://', 'https://')):
        return False
    try:
        host = urlparse(url).netloc.lower()
        return any(host == h or host.endswith('.' + h) or h in host for h in ALLOWED_HOSTS)
    except Exception:
        return False


@method_decorator(require_GET, name='dispatch')
class ImageProxyView(View):
    """Proxy ảnh từ CDN để tránh CORS/referrer block."""

    def get(self, request):
        url = request.GET.get('url', '').strip()
        if not url:
            return HttpResponseBadRequest('Missing url parameter')
        if not is_allowed_url(url):
            logger.warning(f"Image proxy rejected non-whitelisted URL: {url[:80]}...")
            return HttpResponseBadRequest('URL not allowed')

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Referer': 'https://www.facebook.com/',
                'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Sec-Fetch-Dest': 'image',
                'Sec-Fetch-Mode': 'no-cors',
            }
            resp = requests.get(url, timeout=15, headers=headers, stream=True, allow_redirects=True)
            resp.raise_for_status()

            content_type = resp.headers.get('Content-Type', 'image/jpeg')
            # Chỉ cho phép image/*
            if not content_type.startswith('image/'):
                content_type = 'image/jpeg'
            response = HttpResponse(
                resp.content,
                content_type=content_type,
                headers={
                    'Cache-Control': 'public, max-age=3600',
                    'Access-Control-Allow-Origin': '*',
                    'Cross-Origin-Resource-Policy': 'cross-origin',
                    'Cross-Origin-Embedder-Policy': 'unsafe-none',
                },
            )
            return response
        except requests.RequestException as e:
            logger.warning(f"Image proxy failed for {url[:80]}: {e}")
            return HttpResponseBadRequest('Failed to fetch image')
