"""Bóc handle (tên rút gọn) của fanpage từ URL Facebook — dùng chung cho RapidAPI, Apify
và profile tạm, để ba nơi không lệch nhau.

Handle chỉ có khi URL là dạng facebook.com/<tên-page>. Page/profile không đặt tên rút gọn
thì Facebook dùng facebook.com/p/<Tên>-<id>/, facebook.com/people/<Tên>/<id>/ hoặc
profile.php?id=<id> — đoạn path đầu ("p", "people", "profile.php") KHÔNG phải handle. Trả
nó ra làm handle thì mọi page kiểu này cùng mang handle "p" và cùng id tạm "tmp_p".
"""

import re

# Đoạn path đầu tiên của URL Facebook mà không phải tên page.
_NON_PAGE_SEGMENTS = {
    'profile.php',
    'p',
    'people',
    'pages',
    'pg',
    'share',
    'watch',
    'reel',
    'reels',
    'groups',
    'events',
    'hashtag',
    'stories',
    'story.php',
    'permalink.php',
    'photo',
    'photo.php',
}


def extract_page_handle(url: str) -> str:
    """Trả handle của page, hoặc '' nếu URL không chứa tên rút gọn của page."""
    m = re.search(r'facebook\.com/([^/?&#]+)', url or '', re.IGNORECASE)
    handle = m.group(1) if m else ''
    return '' if handle.lower() in _NON_PAGE_SEGMENTS else handle
