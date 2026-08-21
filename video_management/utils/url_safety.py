"""Shared URL safety helpers — SSRF guard for user/BE-controlled fetch URLs."""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_public_http_url(url: str) -> bool:
    """
    Chặn SSRF: chỉ cho phép http(s) trỏ tới host resolve ra IP công khai.
    ponytail: không pin IP qua redirect — cùng giới hạn như video_downloader_views._is_public_url.
    """
    try:
        parsed = urlparse((url or '').strip())
        if parsed.scheme not in ('http', 'https'):
            return False
        host = parsed.hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return False
        return True
    except Exception:
        logger.warning('[URL-SAFETY] Could not validate URL host: %s', (url or '')[:100])
        return False
