"""
Fetch real-time data from social platforms + internal DB for VCB Assistant.
"""
import os
import re
import logging
import requests
import psycopg2
import psycopg2.extras
from django.conf import settings

logger = logging.getLogger(__name__)

# ─── Helpers ────────────────────────────────────────────────────────────────

def _env(key: str) -> str:
    val = str(getattr(settings, key, '')).strip()
    return val or os.getenv(key, '').strip()


def _db_conn():
    url = _env('DATABASE_URL').replace('?pgbouncer=true', '')
    direct = _env('DIRECT_URL')
    conn_str = direct or url
    return psycopg2.connect(conn_str, cursor_factory=psycopg2.extras.RealDictCursor)


def _db_query(sql: str, params=None) -> list:
    try:
        conn = _db_conn()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"DB query error: {e}")
        return []


# ─── DB Tools ───────────────────────────────────────────────────────────────

def get_channels_with_owners(platform: str = None, limit: int = 50) -> list:
    """Danh sách kênh + thông tin chủ sở hữu/team từ bảng huyk_channels."""
    where = "WHERE status = 'Đang hoạt động'"
    params = []
    if platform:
        where += " AND LOWER(platform) LIKE LOWER(%s)"
        params.append(f"%{platform}%")

    sql = f"""
        SELECT
            id, name AS display_name, platform, channel_id AS username,
            link_channel, team_traffic AS team, owner AS owner_name,
            email AS owner_email, status
        FROM huyk_channels
        {where}
        ORDER BY name
        LIMIT %s
    """

    params.append(limit)
    return _db_query(sql, params)


def get_team_channel_summary() -> list:
    """Tổng hợp số kênh theo team từ huyk_channels."""
    sql = """
        SELECT
            COALESCE(NULLIF(team_traffic, ''), 'Chưa phân team') AS team,
            COUNT(*) AS total_channels,
            COUNT(DISTINCT platform) AS platforms,
            STRING_AGG(DISTINCT platform, ', ' ORDER BY platform) AS platform_list
        FROM huyk_channels
        WHERE status = 'Đang hoạt động'
        GROUP BY team_traffic
        ORDER BY total_channels DESC
    """
    return _db_query(sql)


def get_top_channels(limit: int = 10, metric: str = 'total_followers') -> list:
    """Top kênh từ huyk_channels (sorted by name vì chưa có metrics)."""
    sql = """
        SELECT name AS display_name, platform, channel_id AS username,
               team_traffic AS team, owner AS owner_name, link_channel
        FROM huyk_channels
        WHERE status = 'Đang hoạt động'
        ORDER BY name
        LIMIT %s
    """
    return _db_query(sql, [limit])


def get_user_channels(user_name: str) -> list:
    """Kênh của một nhân viên cụ thể từ huyk_channels."""
    sql = """
        SELECT name AS display_name, platform, channel_id AS username,
               team_traffic AS team, link_channel
        FROM huyk_channels
        WHERE status = 'Đang hoạt động'
          AND (LOWER(owner) LIKE LOWER(%s) OR LOWER(email) LIKE LOWER(%s))
        ORDER BY platform, name
    """
    pattern = f"%{user_name}%"
    return _db_query(sql, [pattern, pattern])


# ─── Facebook Graph API ──────────────────────────────────────────────────────

FB_BASE = "https://graph.facebook.com/v19.0"

def _fb_token() -> str:
    return _env('FACEBOOK_ACCESS_TOKEN')


# ─── Facebook Ads Manager ────────────────────────────────────────────────────

def get_fb_ad_accounts() -> list:
    """Lấy danh sách ad account của user."""
    try:
        r = requests.get(f"{FB_BASE}/me/adaccounts", params={
            "access_token": _fb_token(),
            "fields": "id,name,account_status,currency,spend_cap,amount_spent",
        }, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        logger.error(f"FB ad accounts error: {e}")
        return []


def get_fb_ads_report(date_preset: str = "this_month", account_id: str = None) -> list:
    """
    Fetch campaign insights từ Facebook Ads Manager.
    date_preset: today, yesterday, this_week_mon_today, last_7d, last_30d, this_month, last_month
    """
    try:
        # Dùng account Ads.Vienchibao Official nếu không chỉ định
        if not account_id:
            account_id = "act_721012660672250"

        # Query trực tiếp insights ở account level — nhanh và đầy đủ hơn
        insights_r = requests.get(f"{FB_BASE}/{account_id}/insights", params={
            "access_token": _fb_token(),
            "fields": "campaign_name,campaign_id,spend,impressions,reach,clicks,actions,cost_per_action_type,date_start,date_stop",
            "date_preset": date_preset,
            "level": "campaign",
            "limit": 100,
        }, timeout=30)
        insights_r.raise_for_status()
        rows = insights_r.json().get("data", [])

        results = [_parse_camp_insight(r) for r in rows]
        results.sort(key=lambda x: float(x.get("spend", 0)), reverse=True)
        return results
    except Exception as e:
        logger.error(f"FB ads report error: {e}")
        return []


def _parse_camp_insight(row: dict) -> dict:
    """Parse 1 dòng insights thành format chuẩn. Spend đã là VND (tài khoản VN)."""
    actions = {a["action_type"]: int(float(a["value"])) for a in row.get("actions", [])}
    cost_per = {a["action_type"]: float(a["value"]) for a in row.get("cost_per_action_type", [])}

    # Mess = cuộc hội thoại bắt đầu qua Messenger
    mess = actions.get("onsite_conversion.messaging_conversation_started_7d", 0)
    cost_mess = cost_per.get("onsite_conversion.messaging_conversation_started_7d", 0)

    # Like page
    like_page = actions.get("like", 0)
    cost_like = cost_per.get("like", 0)

    # Tương tác
    engagements = actions.get("post_engagement", 0)
    cost_engage = cost_per.get("post_engagement", 0)

    spend_vnd = float(row.get("spend", 0))  # Tài khoản VN → đã là VND

    # Tính cost/mess từ spend nếu API không trả về
    if mess > 0 and cost_mess == 0:
        cost_mess = spend_vnd / mess

    name = row.get("campaign_name", "N/A")
    # Parse camp_type và content_type từ tên campaign (format: ... - Mess/Like page/Tương tác - A1/A2/A3/A4/A5)
    name_lower = name.lower()
    if "mess" in name_lower:
        camp_type = "mess"
    elif "like page" in name_lower or "like_page" in name_lower:
        camp_type = "like_page"
    elif "tương tác" in name_lower or "tuong tac" in name_lower:
        camp_type = "tuong_tac"
    else:
        camp_type = "other"

    content_type = "unknown"
    for ct in ["A5", "A4", "A3", "A2", "A1"]:
        if f"- {ct}" in name or f"- {ct} -" in name or name.endswith(ct):
            content_type = ct
            break

    return {
        "campaign_name": name,
        "camp_type": camp_type,
        "content_type": content_type,
        "spend": spend_vnd,
        "impressions": int(row.get("impressions", 0)),
        "reach": int(row.get("reach", 0)),
        "clicks": int(row.get("clicks", 0)),
        "mess": mess,
        "cost_per_mess": round(cost_mess),
        "like_page": like_page,
        "cost_per_like": round(cost_like),
        "engagements": engagements,
        "cost_per_engagement": round(cost_engage),
        "period": f"{row.get('date_start','')} → {row.get('date_stop','')}",
    }


def get_fb_ads_summary(date_preset: str = "this_month") -> dict:
    """Tổng hợp toàn bộ ads account: tổng chi, camp tốt/xấu theo rule VCB."""
    camps = get_fb_ads_report(date_preset)
    if not camps:
        return {"error": "Không lấy được dữ liệu ads. Kiểm tra lại access token."}

    total_spend = sum(c["spend"] for c in camps)
    total_mess = sum(c["mess"] for c in camps)
    total_likes = sum(c["like_page"] for c in camps)

    good, warning, bad = [], [], []
    for c in camps:
        if c["camp_type"] == "mess" and c["mess"] > 0:
            cpm = c["cost_per_mess"]
            if cpm <= 10000:
                good.append(c["campaign_name"])
            elif cpm <= 15000:
                warning.append(c["campaign_name"])
            else:
                bad.append(c["campaign_name"])
        elif c["camp_type"] == "like_page" and c["like_page"] > 0:
            cpl = c["cost_per_like"]
            if cpl <= 1000:
                good.append(c["campaign_name"])
            elif cpl <= 1500:
                warning.append(c["campaign_name"])
            else:
                bad.append(c["campaign_name"])

    return {
        "period": date_preset,
        "total_campaigns": len(camps),
        "total_spend_vnd": total_spend,
        "total_mess": total_mess,
        "total_likes": total_likes,
        "good_camps": good,
        "warning_camps": warning,
        "bad_camps": bad,
        "campaigns": camps,
    }


# ─── Monthly Views by Platform ───────────────────────────────────────────────

import calendar
from datetime import date, datetime as dt


def _month_range(year: int, month: int):
    """Trả về (date_from, date_to) dạng string YYYY-MM-DD."""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day}"


def get_youtube_monthly_views(channel_identifier: str, year: int, month: int) -> dict:
    """
    Lấy tổng views của các video đăng trong tháng từ YouTube channel.
    channel_identifier: channel ID (UCxxx) hoặc handle (@handle) hoặc username.
    """
    key = _yt_key()
    # Làm sạch identifier — trim newline, space
    channel_identifier = channel_identifier.strip().strip("\n").strip()
    if not channel_identifier:
        return {"error": "channel_identifier trống"}

    date_from, date_to = _month_range(year, month)

    try:
        channel_id = channel_identifier
        channel_stats = {}

        if channel_identifier.startswith("UC"):
            # Đã là channel ID
            r0 = requests.get(f"{YT_BASE}/channels", params={
                "key": key, "id": channel_identifier, "part": "id,snippet,statistics",
            }, timeout=10)
            if r0.ok:
                items0 = r0.json().get("items", [])
                if items0:
                    channel_stats = items0[0].get("statistics", {})
        else:
            handle = channel_identifier.lstrip("@").strip()
            if not handle:
                return {"error": "handle trống"}
            # Thử forHandle trước
            r = requests.get(f"{YT_BASE}/channels", params={
                "key": key, "forHandle": f"@{handle}", "part": "id,snippet,statistics",
            }, timeout=10)
            items = r.json().get("items", []) if r.ok else []
            if not items:
                # Fallback forUsername
                r2 = requests.get(f"{YT_BASE}/channels", params={
                    "key": key, "forUsername": handle, "part": "id,snippet,statistics",
                }, timeout=10)
                items = r2.json().get("items", []) if r2.ok else []
            if items:
                channel_id = items[0]["id"]
                channel_stats = items[0].get("statistics", {})
            else:
                return {"error": f"Không tìm thấy channel: {channel_identifier}"}

        # Search videos trong tháng
        search_r = requests.get(f"{YT_BASE}/search", params={
            "key": key, "channelId": channel_id, "part": "id",
            "type": "video", "order": "date",
            "publishedAfter": f"{date_from}T00:00:00Z",
            "publishedBefore": f"{date_to}T23:59:59Z",
            "maxResults": 50,
        }, timeout=15)
        search_r.raise_for_status()
        video_ids = [i["id"]["videoId"] for i in search_r.json().get("items", [])]

        if not video_ids:
            return {"channel": channel_identifier, "year": year, "month": month,
                    "video_count": 0, "total_views": 0, "total_likes": 0, "videos": []}

        # Lấy stats từng video
        stats_r = requests.get(f"{YT_BASE}/videos", params={
            "key": key, "id": ",".join(video_ids), "part": "snippet,statistics",
        }, timeout=15)
        stats_r.raise_for_status()

        videos = []
        for item in stats_r.json().get("items", []):
            s = item.get("statistics", {})
            videos.append({
                "title": item["snippet"]["title"][:60],
                "published_at": item["snippet"]["publishedAt"][:10],
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
            })

        videos.sort(key=lambda x: x["views"], reverse=True)
        return {
            "channel": channel_identifier,
            "channel_id": channel_id,
            "year": year, "month": month,
            "video_count": len(videos),
            "total_views": sum(v["views"] for v in videos),
            "total_likes": sum(v["likes"] for v in videos),
            "videos": videos[:10],
        }
    except Exception as e:
        logger.error(f"YT monthly views error: {e}")
        return {"error": str(e), "channel": channel_identifier}


def get_facebook_page_monthly_views(page_identifier: str, year: int, month: int) -> dict:
    """
    Lấy tổng views video của Facebook page trong tháng.
    page_identifier: page username hoặc page ID.
    """
    token = _fb_token()
    date_from, date_to = _month_range(year, month)
    since = int(dt.strptime(date_from, "%Y-%m-%d").timestamp())
    until = int(dt.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399

    try:
        # Lấy thông tin page + page token (nếu là admin)
        page_r = requests.get(f"{FB_BASE}/{page_identifier}", params={
            "access_token": token,
            "fields": "id,name,access_token,fan_count,followers_count,talking_about_count",
        }, timeout=10)
        if not page_r.ok:
            return {"error": f"Không tìm thấy page: {page_identifier}", "total_views": 0}
        page_data = page_r.json()
        if "error" in page_data:
            return {"error": page_data["error"].get("message", "FB API error"), "total_views": 0}

        page_id = page_data.get("id", page_identifier)
        page_token = page_data.get("access_token")  # None nếu không phải admin
        is_admin = bool(page_token)
        use_token = page_token or token  # dùng user token nếu không có page token

        # Lấy posts trong tháng
        posts_r = requests.get(f"{FB_BASE}/{page_id}/posts", params={
            "access_token": use_token,
            "fields": "id,message,created_time,likes.summary(true),comments.summary(true),shares",
            "since": since, "until": until,
            "limit": 50,
        }, timeout=15)
        posts_r.raise_for_status()
        posts = posts_r.json().get("data", [])

        total_views = 0
        total_likes = 0
        total_comments = 0
        total_shares = 0
        post_details = []

        for post in posts:
            post_id = post["id"]
            likes = post.get("likes", {}).get("summary", {}).get("total_count", 0)
            comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
            shares = post.get("shares", {}).get("count", 0)
            total_likes += likes
            total_comments += comments
            total_shares += shares

            views = 0
            if is_admin:
                try:
                    ins_r = requests.get(f"{FB_BASE}/{post_id}/insights", params={
                        "access_token": page_token,
                        "metric": "post_impressions,post_video_views",
                    }, timeout=8)
                    if ins_r.ok:
                        ins_data = {d["name"]: d.get("values", [{}])[0].get("value", 0)
                                    for d in ins_r.json().get("data", [])}
                        video_views = ins_data.get("post_video_views", 0)
                        impressions = ins_data.get("post_impressions", 0)
                        if isinstance(video_views, dict): video_views = sum(video_views.values())
                        if isinstance(impressions, dict): impressions = sum(impressions.values())
                        views = video_views if video_views > 0 else impressions
                except Exception:
                    pass

            total_views += views
            post_details.append({
                "created_time": post.get("created_time", "")[:10],
                "message": (post.get("message") or "")[:60],
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
            })

        post_details.sort(key=lambda x: x["likes"] + x["comments"], reverse=True)
        return {
            "page": page_data.get("name", page_identifier),
            "page_id": page_id,
            "followers": page_data.get("followers_count", 0),
            "talking_about": page_data.get("talking_about_count", 0),
            "is_admin": is_admin,
            "year": year, "month": month,
            "post_count": len(posts),
            "total_views": total_views,        # chỉ có khi là admin
            "total_likes": total_likes,         # luôn có
            "total_comments": total_comments,   # luôn có
            "total_shares": total_shares,       # luôn có
            "top_posts": post_details[:10],
            "note": None if is_admin else "Không phải admin page → chỉ có engagement (likes/comments/shares), không có views/impressions",
        }
    except Exception as e:
        logger.error(f"FB monthly views error: {e}")
        return {"error": str(e), "page": page_identifier}


def _extract_fb_identifier(channel_id: str, link_channel: str) -> str:
    """
    Lấy Facebook page identifier từ channel_id hoặc link_channel.
    Ưu tiên channel_id, fallback sang parse link_channel.
    """
    cid = (channel_id or "").strip().strip("\n")
    if cid:
        return cid

    url = (link_channel or "").strip()
    if not url:
        return ""

    # profile.php?id=XXXXXXX
    m = re.search(r'profile\.php\?id=(\d+)', url)
    if m:
        return m.group(1)

    # /people/Name/XXXXXXX/
    m = re.search(r'/people/[^/]+/(\d+)', url)
    if m:
        return m.group(1)

    # facebook.com/username/ hoặc facebook.com/username
    m = re.search(r'facebook\.com/([^/?#\s]+)', url)
    if m:
        slug = m.group(1).rstrip('/')
        # Bỏ các slug không hợp lệ
        if slug not in ('share', 'pages', 'groups', 'watch', 'video'):
            return slug

    return ""


def _extract_yt_identifier(channel_id: str, link_channel: str) -> str:
    """Lấy YouTube channel identifier từ channel_id hoặc link_channel."""
    cid = (channel_id or "").strip().strip("\n")
    if cid:
        return cid

    url = (link_channel or "").strip()
    if not url:
        return ""

    # youtube.com/@handle hoặc youtube.com/channel/UCxxx hoặc youtube.com/c/name
    m = re.search(r'youtube\.com/(@[^/?#\s]+|channel/([^/?#\s]+)|c/([^/?#\s]+))', url)
    if m:
        return m.group(1)  # trả về @handle hoặc channel/UCxxx

    return ""


def _normalize_platform(raw: str) -> str:
    p = (raw or "").lower().strip()
    if "tiktok" in p: return "tiktok"
    if "instagram" in p or p == "ig": return "instagram"
    if "facebook" in p: return "facebook"
    if "youtube" in p: return "youtube"
    return p


def get_channels_monthly_views(year: int, month: int, platform: str = None) -> list:
    """
    Tổng hợp lượt xem tháng cho các kênh — giới hạn 15 kênh/lần để tránh timeout.
    """
    channels = get_channels_with_owners(platform=platform, limit=100)
    if not channels:
        return []

    # Lọc và chuẩn hóa — bỏ kênh không có channel_id
    valid = []
    for ch in channels:
        uid = (ch.get("username") or "").strip().strip("\n").strip()
        if not uid:
            continue
        ch["username"] = uid
        ch["_platform"] = _normalize_platform(ch.get("platform", ""))
        valid.append(ch)

    # Giới hạn 15 kênh để tránh timeout
    valid = valid[:15]

    results = []
    for ch in valid:
        plat = ch["_platform"]
        raw_uid = ch.get("username", "")
        link = ch.get("link_channel", "")
        display = ch.get("display_name") or raw_uid

        # Resolve identifier từ channel_id hoặc link_channel
        if plat == "youtube":
            username = _extract_yt_identifier(raw_uid, link)
        elif plat == "facebook":
            username = _extract_fb_identifier(raw_uid, link)
        else:
            username = raw_uid.strip().strip("\n")

        if not username:
            results.append({
                "channel": display, "platform": plat,
                "owner": ch.get("owner_name", ""), "team": ch.get("team", ""),
                "video_count": 0, "total_views": 0, "total_likes": 0,
                "error": "Thiếu channel_id và link_channel",
            })
            continue

        if plat == "youtube":
            data = get_youtube_monthly_views(username, year, month)
        elif plat == "facebook":
            data = get_facebook_page_monthly_views(username, year, month)
        elif plat == "tiktok":
            data = get_tiktok_monthly_views(username, year, month)
        else:
            # Instagram: query DB scrapedvideo (nếu có) hoặc trả 0
            sql = """
                SELECT COUNT(*) as video_count,
                       COALESCE(SUM(views_count), 0) as total_views,
                       COALESCE(SUM(likes_count), 0) as total_likes
                FROM video_management_scrapedvideo
                WHERE LOWER(author_username) = LOWER(%s)
                  AND LOWER(platform) = LOWER(%s)
                  AND published_at >= %s AND published_at <= %s
            """
            rows = _db_query(sql, [username, plat, f"{year}-{month:02d}-01",
                                   f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"])
            row = rows[0] if rows else {}
            data = {
                "channel": display, "year": year, "month": month,
                "video_count": int(row.get("video_count", 0)),
                "total_views": int(row.get("total_views", 0)),
                "total_likes": int(row.get("total_likes", 0)),
            }

        results.append({
            "channel": display,
            "platform": plat,
            "owner": ch.get("owner_name", ""),
            "team": ch.get("team", ""),
            "video_count": data.get("video_count", 0),
            "total_views": data.get("total_views", 0),
            "total_likes": data.get("total_likes", 0),
            "error": data.get("error"),
        })

    results.sort(key=lambda x: x["total_views"], reverse=True)
    return results


def get_facebook_pages() -> list:
    """Lấy danh sách page FB đang quản lý."""
    try:
        r = requests.get(f"{FB_BASE}/me/accounts", params={
            "access_token": _fb_token(),
            "fields": "id,name,fan_count,followers_count,category",
        }, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        logger.error(f"FB pages error: {e}")
        return []


def get_facebook_page_insights(page_id: str, days: int = 30) -> dict:
    """Insights của 1 page FB."""
    try:
        page_token_r = requests.get(f"{FB_BASE}/{page_id}", params={
            "access_token": _fb_token(),
            "fields": "access_token,name,fan_count,followers_count",
        }, timeout=15)
        page_token_r.raise_for_status()
        page_data = page_token_r.json()
        page_token = page_data.get("access_token", _fb_token())

        metrics = "page_impressions,page_reach,page_engaged_users,page_post_engagements,page_fan_adds_unique"
        insights_r = requests.get(f"{FB_BASE}/{page_id}/insights", params={
            "access_token": page_token,
            "metric": metrics,
            "period": "day",
            "date_preset": f"last_{days}d",
        }, timeout=15)
        insights_r.raise_for_status()

        raw = insights_r.json().get("data", [])
        result = {"page_id": page_id, "name": page_data.get("name"), "fans": page_data.get("fan_count")}
        for item in raw:
            vals = item.get("values", [])
            total = sum(v.get("value", 0) if isinstance(v.get("value"), (int, float)) else 0 for v in vals)
            result[item["name"]] = total
        return result
    except Exception as e:
        logger.error(f"FB insights error: {e}")
        return {}


# ─── Instagram Graph API (dùng Facebook token) ───────────────────────────────

def get_instagram_accounts() -> list:
    """Lấy tất cả IG Business accounts liên kết với FB pages đang admin."""
    token = _fb_token()
    try:
        accounts = []
        url = f"{FB_BASE}/me/accounts"
        while url:
            r = requests.get(url, params={
                "access_token": token,
                "fields": "id,name,instagram_business_account{id,username,followers_count,media_count}",
                "limit": 50,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()
            for p in data.get("data", []):
                ig = p.get("instagram_business_account")
                if ig:
                    accounts.append({
                        "ig_id": ig["id"],
                        "username": ig.get("username", ""),
                        "followers_count": ig.get("followers_count", 0),
                        "media_count": ig.get("media_count", 0),
                        "page_name": p.get("name", ""),
                        "page_id": p.get("id", ""),
                    })
            url = data.get("paging", {}).get("next")
        return sorted(accounts, key=lambda x: x["followers_count"], reverse=True)
    except Exception as e:
        logger.error(f"IG accounts error: {e}")
        return []


def get_instagram_monthly_views(ig_username: str, year: int, month: int) -> dict:
    """
    Lấy views/impressions của IG Business account trong tháng.
    Dùng Facebook token — chỉ hoạt động với IG Business liên kết FB page.
    """
    token = _fb_token()
    date_from, date_to = _month_range(year, month)

    try:
        # Tìm ig_id từ username
        accounts = get_instagram_accounts()
        account = next((a for a in accounts if a["username"].lower() == ig_username.lower()), None)
        if not account:
            return {"error": f"Không tìm thấy IG account: {ig_username}", "total_views": 0}

        ig_id = account["ig_id"]

        # Lấy media trong tháng
        since_ts = int(dt.strptime(date_from, "%Y-%m-%d").timestamp())
        until_ts = int(dt.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399

        media_r = requests.get(f"{FB_BASE}/{ig_id}/media", params={
            "access_token": token,
            "fields": "id,caption,media_type,timestamp,like_count,comments_count",
            "since": since_ts, "until": until_ts,
            "limit": 50,
        }, timeout=15)
        media_r.raise_for_status()
        media_items = media_r.json().get("data", [])

        total_impressions = 0
        total_reach = 0
        total_likes = 0
        total_comments = 0
        posts = []

        for item in media_items:
            mid = item["id"]
            likes = item.get("like_count", 0)
            comments = item.get("comments_count", 0)
            total_likes += likes
            total_comments += comments

            # Lấy insights từng post
            try:
                ins_r = requests.get(f"{FB_BASE}/{mid}/insights", params={
                    "access_token": token,
                    "metric": "impressions,reach",
                }, timeout=8)
                if ins_r.ok:
                    for d_item in ins_r.json().get("data", []):
                        val = d_item.get("values", [{}])[0].get("value", 0)
                        if d_item["name"] == "impressions":
                            total_impressions += int(val)
                        elif d_item["name"] == "reach":
                            total_reach += int(val)
            except Exception:
                pass

            posts.append({
                "timestamp": item.get("timestamp", "")[:10],
                "caption": (item.get("caption") or "")[:60],
                "media_type": item.get("media_type", ""),
                "likes": likes,
                "comments": comments,
            })

        posts.sort(key=lambda x: x["likes"], reverse=True)

        # Account-level insights (follower growth)
        try:
            acc_r = requests.get(f"{FB_BASE}/{ig_id}/insights", params={
                "access_token": token,
                "metric": "follower_count,impressions,reach,profile_views",
                "period": "day",
                "since": since_ts, "until": until_ts,
            }, timeout=10)
            if acc_r.ok:
                for d_item in acc_r.json().get("data", []):
                    if d_item["name"] == "impressions" and total_impressions == 0:
                        total_impressions = sum(
                            v.get("value", 0) for v in d_item.get("values", [])
                            if isinstance(v.get("value"), (int, float))
                        )
        except Exception:
            pass

        return {
            "username": ig_username,
            "ig_id": ig_id,
            "page_name": account["page_name"],
            "followers": account["followers_count"],
            "year": year, "month": month,
            "post_count": len(posts),
            "total_impressions": total_impressions,
            "total_reach": total_reach,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "top_posts": posts[:10],
        }
    except Exception as e:
        logger.error(f"IG monthly views error for {ig_username}: {e}")
        return {"error": str(e), "username": ig_username, "total_views": 0}


def get_all_instagram_monthly_summary(year: int, month: int) -> list:
    """Tổng hợp tất cả IG accounts trong tháng — impressions, likes, posts."""
    token = _fb_token()
    date_from, date_to = _month_range(year, month)
    since_ts = int(dt.strptime(date_from, "%Y-%m-%d").timestamp())
    until_ts = int(dt.strptime(date_to, "%Y-%m-%d").timestamp()) + 86399

    accounts = get_instagram_accounts()
    results = []

    for acc in accounts[:20]:  # Giới hạn 20 để tránh timeout
        ig_id = acc["ig_id"]
        try:
            # Đếm posts trong tháng
            media_r = requests.get(f"{FB_BASE}/{ig_id}/media", params={
                "access_token": token,
                "fields": "id,like_count,comments_count,timestamp",
                "since": since_ts, "until": until_ts,
                "limit": 50,
            }, timeout=10)
            media_r.raise_for_status()
            items = media_r.json().get("data", [])

            total_likes = sum(i.get("like_count", 0) for i in items)
            total_comments = sum(i.get("comments_count", 0) for i in items)

            results.append({
                "username": acc["username"],
                "page_name": acc["page_name"],
                "followers": acc["followers_count"],
                "post_count": len(items),
                "total_likes": total_likes,
                "total_comments": total_comments,
                "engagement": total_likes + total_comments,
            })
        except Exception as e:
            logger.error(f"IG summary error for {acc['username']}: {e}")

    results.sort(key=lambda x: x["engagement"], reverse=True)
    return results


def get_instagram_insights(ig_user_id: str, days: int = 30) -> dict:
    """Legacy — giữ lại để tương thích."""
    token = _fb_token()
    try:
        r = requests.get(f"{FB_BASE}/{ig_user_id}/insights", params={
            "access_token": token,
            "metric": "impressions,reach,profile_views",
            "period": "day",
        }, timeout=15)
        r.raise_for_status()
        raw = r.json().get("data", [])
        result = {"ig_user_id": ig_user_id}
        for item in raw:
            vals = item.get("values", [])
            total = sum(v.get("value", 0) for v in vals if isinstance(v.get("value"), (int, float)))
            result[item["name"]] = total
        return result
    except Exception as e:
        logger.error(f"IG insights error: {e}")
        return {}


# ─── YouTube Data API v3 ─────────────────────────────────────────────────────

YT_BASE = "https://www.googleapis.com/youtube/v3"

def _yt_key() -> str:
    return _env('YOUTUBE_API_KEY')


def get_youtube_channel_stats(channel_id: str) -> dict:
    """Stats của 1 YouTube channel."""
    try:
        r = requests.get(f"{YT_BASE}/channels", params={
            "key": _yt_key(),
            "id": channel_id,
            "part": "snippet,statistics",
        }, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return {}
        item = items[0]
        stats = item.get("statistics", {})
        return {
            "channel_id": channel_id,
            "title": item["snippet"]["title"],
            "subscribers": int(stats.get("subscriberCount", 0)),
            "views": int(stats.get("viewCount", 0)),
            "videos": int(stats.get("videoCount", 0)),
        }
    except Exception as e:
        logger.error(f"YT channel error: {e}")
        return {}


def get_youtube_top_videos(channel_id: str, limit: int = 10) -> list:
    """Top videos của 1 channel YT."""
    try:
        search_r = requests.get(f"{YT_BASE}/search", params={
            "key": _yt_key(),
            "channelId": channel_id,
            "part": "id",
            "order": "viewCount",
            "maxResults": limit,
            "type": "video",
        }, timeout=15)
        search_r.raise_for_status()
        ids = [i["id"]["videoId"] for i in search_r.json().get("items", [])]
        if not ids:
            return []

        stats_r = requests.get(f"{YT_BASE}/videos", params={
            "key": _yt_key(),
            "id": ",".join(ids),
            "part": "snippet,statistics",
        }, timeout=15)
        stats_r.raise_for_status()
        results = []
        for item in stats_r.json().get("items", []):
            s = item.get("statistics", {})
            results.append({
                "title": item["snippet"]["title"][:60],
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            })
        return results
    except Exception as e:
        logger.error(f"YT videos error: {e}")
        return []


# ─── TikTok (via existing Apify/scraper) ────────────────────────────────────

def _apify_fetch_tiktok(username: str, max_videos: int = 30) -> list:
    """
    Fetch TikTok videos qua Apify. Trả về list video raw.
    Cache vào DB để tránh fetch lại.
    """
    apify_token = _env('APIFY_API_TOKEN')
    if not apify_token or apify_token == 'your_apify_api_token_here':
        return []

    username = username.lstrip('@').strip()

    try:
        r = requests.post(
            "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items",
            params={"token": apify_token, "timeout": 120},
            json={"profiles": [username], "resultsPerPage": max_videos},
            timeout=150,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        logger.error(f"Apify TikTok fetch error for {username}: {e}")
        return []


def _parse_tiktok_video(item: dict, channel_name: str = "", owner: str = "", team: str = "") -> dict:
    """Parse 1 video TikTok từ Apify response."""
    # Lấy timestamp
    create_iso = item.get("createTimeISO") or ""
    create_ts = item.get("createTime", 0)
    if create_iso:
        pub_date = create_iso[:10]
    elif create_ts:
        from datetime import datetime as _dt
        pub_date = _dt.utcfromtimestamp(int(create_ts)).strftime("%Y-%m-%d")
    else:
        pub_date = ""

    return {
        "video_id": item.get("id", ""),
        "title": (item.get("text") or "")[:80],
        "url": item.get("webVideoUrl") or item.get("videoUrl", ""),
        "published_at": pub_date,
        "views": int(item.get("playCount", 0)),
        "likes": int(item.get("diggCount", 0)),
        "comments": int(item.get("commentCount", 0)),
        "shares": int(item.get("shareCount", 0)),
        "username": item.get("authorMeta", {}).get("name") or item.get("author", {}).get("uniqueId", ""),
        "channel_name": channel_name,
        "owner": owner,
        "team": team,
    }


def get_tiktok_monthly_views(username: str, year: int, month: int) -> dict:
    """Lấy views TikTok 1 kênh trong tháng (dùng cho get_channels_monthly_views)."""
    date_from, date_to = _month_range(year, month)
    items = _apify_fetch_tiktok(username, 30)

    if not items:
        return {"channel": username, "year": year, "month": month,
                "video_count": 0, "total_views": 0, "total_likes": 0,
                "note": "Cần APIFY_API_TOKEN hợp lệ"}

    videos = [_parse_tiktok_video(i) for i in items
              if date_from <= _parse_tiktok_video(i)["published_at"] <= date_to]
    return {
        "channel": username, "year": year, "month": month,
        "video_count": len(videos),
        "total_views": sum(v["views"] for v in videos),
        "total_likes": sum(v["likes"] for v in videos),
        "videos": sorted(videos, key=lambda x: x["views"], reverse=True)[:10],
    }


def get_tiktok_monthly_report(year: int, month: int, team: str = None, owner: str = None) -> dict:
    """
    Báo cáo traffic TikTok toàn bộ kênh đang hoạt động trong tháng.
    Trả về: tổng views/likes/comments, top 10 theo từng chỉ số, breakdown theo team/người.
    """
    apify_token = _env('APIFY_API_TOKEN')
    if not apify_token or apify_token == 'your_apify_api_token_here':
        return {"error": "Chưa cấu hình APIFY_API_TOKEN. Vui lòng điền token vào file .env"}

    date_from, date_to = _month_range(year, month)

    # Lấy danh sách kênh TikTok từ huyk_channels
    params = ["%%tiktok%%"]
    extra_where = ""
    if team:
        extra_where += " AND LOWER(team_traffic) LIKE LOWER(%s)"
        params.append(f"%{team}%")
    if owner:
        extra_where += " AND LOWER(owner) LIKE LOWER(%s)"
        params.append(f"%{owner}%")

    channels = _db_query(f"""
        SELECT name, channel_id, link_channel, team_traffic AS team, owner
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s AND status = 'Đang hoạt động'
        {extra_where}
        ORDER BY name
    """, params)

    if not channels:
        return {"error": "Không tìm thấy kênh TikTok nào"}

    logger.info(f"TikTok report: fetching {len(channels)} channels for {year}-{month:02d}")

    all_videos = []
    channel_summaries = []

    for ch in channels:
        raw_uid = (ch.get("channel_id") or "").strip().strip("\n")
        link = ch.get("link_channel") or ""
        # Extract username từ link nếu channel_id trống
        if not raw_uid and "tiktok.com/@" in link:
            raw_uid = link.split("tiktok.com/@")[-1].split("?")[0].strip()
        if not raw_uid:
            continue

        channel_name = ch.get("name", raw_uid)
        owner_name = ch.get("owner", "")
        team_name = ch.get("team", "")

        items = _apify_fetch_tiktok(raw_uid, 30)
        month_videos = []
        for item in items:
            v = _parse_tiktok_video(item, channel_name, owner_name, team_name)
            if date_from <= v["published_at"] <= date_to:
                month_videos.append(v)

        total_views = sum(v["views"] for v in month_videos)
        total_likes = sum(v["likes"] for v in month_videos)
        total_comments = sum(v["comments"] for v in month_videos)
        total_shares = sum(v["shares"] for v in month_videos)

        channel_summaries.append({
            "channel": channel_name,
            "username": raw_uid,
            "owner": owner_name,
            "team": team_name,
            "video_count": len(month_videos),
            "total_views": total_views,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "total_shares": total_shares,
        })
        all_videos.extend(month_videos)

    # Tổng hợp
    grand_views    = sum(c["total_views"] for c in channel_summaries)
    grand_likes    = sum(c["total_likes"] for c in channel_summaries)
    grand_comments = sum(c["total_comments"] for c in channel_summaries)
    grand_shares   = sum(c["total_shares"] for c in channel_summaries)
    grand_videos   = sum(c["video_count"] for c in channel_summaries)

    # Top 10 theo từng chỉ số
    top_views    = sorted(all_videos, key=lambda x: x["views"],    reverse=True)[:10]
    top_likes    = sorted(all_videos, key=lambda x: x["likes"],    reverse=True)[:10]
    top_comments = sorted(all_videos, key=lambda x: x["comments"], reverse=True)[:10]

    # Breakdown theo team
    team_stats: dict = {}
    for c in channel_summaries:
        t = c["team"] or "Chưa phân team"
        if t not in team_stats:
            team_stats[t] = {"team": t, "channels": 0, "total_views": 0, "total_likes": 0, "total_comments": 0}
        team_stats[t]["channels"] += 1
        team_stats[t]["total_views"] += c["total_views"]
        team_stats[t]["total_likes"] += c["total_likes"]
        team_stats[t]["total_comments"] += c["total_comments"]

    return {
        "period": f"Tháng {month}/{year}",
        "platform": "TikTok",
        "channels_fetched": len(channel_summaries),
        "summary": {
            "total_videos": grand_videos,
            "total_views": grand_views,
            "total_likes": grand_likes,
            "total_comments": grand_comments,
            "total_shares": grand_shares,
        },
        "top_10_views":    [{"title": v["title"][:50], "channel": v["channel_name"], "owner": v["owner"], "team": v["team"], "views": v["views"], "url": v["url"]} for v in top_views],
        "top_10_likes":    [{"title": v["title"][:50], "channel": v["channel_name"], "owner": v["owner"], "team": v["team"], "likes": v["likes"], "url": v["url"]} for v in top_likes],
        "top_10_comments": [{"title": v["title"][:50], "channel": v["channel_name"], "owner": v["owner"], "team": v["team"], "comments": v["comments"], "url": v["url"]} for v in top_comments],
        "by_team": sorted(team_stats.values(), key=lambda x: x["total_views"], reverse=True),
        "by_channel": sorted(channel_summaries, key=lambda x: x["total_views"], reverse=True),
    }


def get_tiktok_profile_from_db(username: str) -> dict:
    """Lấy TikTok profile từ DB (đã được sync bởi Apify)."""
    sql = """
        SELECT username, display_name, total_followers, total_views,
               total_likes, total_videos, engagement_rate, last_synced_at,
               u.full_name AS owner, u.team
        FROM tracked_channels tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE LOWER(tc.username) = LOWER(%s) AND tc.platform = 'tiktok'
        LIMIT 1
    """
    rows = _db_query(sql, [username])
    return rows[0] if rows else {}


# ─── Ads Analysis Tool (rule-based, no API needed) ──────────────────────────

def analyze_ads_camp(
    content_type: str,      # A1/A2/A3/A4/A5
    camp_type: str,         # like_page / tuong_tac / mess
    cost_per_result: float,
    results: int,
    spend: float,
    days_running: int,
    views: int = 0,
    page_size: int = 0,
) -> dict:
    """Phân tích camp ads theo rule nội bộ VCB."""
    ct = content_type.upper().strip()
    camp = camp_type.lower().strip()

    verdict = "GOOD"
    action = "KEEP"
    reasons = []
    insights = []
    warnings = []

    # ── Rule: A4 mới được chạy mess ─────────────────────────────────────────
    if camp == "mess" and ct not in ("A4", "A5"):
        warnings.append(f"⚠️ SAI CHIẾN LƯỢC: {ct} không được chạy mess — chỉ A4/A5 mới chạy mess")
        verdict = "BAD"
        action = "STOP"

    # ── Rule đánh giá theo loại camp ────────────────────────────────────────
    if camp == "mess":
        if cost_per_result <= 10000:
            reasons.append(f"✅ Cost/mess {cost_per_result:,.0f}đ ≤ 10.000đ → TỐT")
            verdict = max(verdict, "GOOD") if verdict != "BAD" else verdict
            if results > 30 and cost_per_result < 8000:
                action = "SCALE"
                insights.append("🚀 SCALE MẠNH: mess > 30 và cost < 8.000đ")
        else:
            reasons.append(f"❌ Cost/mess {cost_per_result:,.0f}đ > 10.000đ → KÉM")
            verdict = "BAD"
            action = "STOP"

        if days_running > 3:
            if results < 20:
                warnings.append(f"⛔ Chạy {days_running} ngày nhưng chỉ {results} mess < 20 → STOP")
                verdict = "BAD"
                action = "STOP"
            if cost_per_result > 15000:
                warnings.append(f"⛔ Chạy {days_running} ngày, cost {cost_per_result:,.0f}đ > 15.000đ → STOP")
                verdict = "BAD"
                action = "STOP"

    elif camp == "like_page":
        if cost_per_result <= 1000:
            reasons.append(f"✅ Cost/follow {cost_per_result:,.0f}đ ≤ 1.000đ → TỐT → SCALE")
            action = "SCALE"
        elif cost_per_result <= 1500:
            reasons.append(f"⚡ Cost/follow {cost_per_result:,.0f}đ (1.000–1.500đ) → TRUNG BÌNH → TEST")
            verdict = "WARNING"
            action = "TEST"
        else:
            reasons.append(f"❌ Cost/follow {cost_per_result:,.0f}đ > 1.500đ → KÉM → STOP")
            verdict = "BAD"
            action = "STOP"

    elif camp == "tuong_tac":
        if cost_per_result <= 12:
            reasons.append(f"✅ Cost/tương tác {cost_per_result:,.0f}đ ≈ 12đ → TỐT")
        elif cost_per_result <= 20:
            reasons.append(f"⚡ Cost/tương tác {cost_per_result:,.0f}đ (12–20đ) → TRUNG BÌNH")
            verdict = "WARNING"
        else:
            reasons.append(f"❌ Cost/tương tác {cost_per_result:,.0f}đ > 20đ → STOP")
            verdict = "BAD"
            action = "STOP"
        insights.append("ℹ️ Camp tương tác: không cần scale mạnh")

    # ── Rule tắt ads theo content type ──────────────────────────────────────
    tuat_threshold = {"A1": 20, "A2": 30, "A3": 30, "A4": 40, "A5": 40}.get(ct)
    if tuat_threshold and cost_per_result > tuat_threshold and days_running > 3:
        warnings.append(f"⛔ {ct} chạy {days_running} ngày, cost {cost_per_result:,.0f}đ > {tuat_threshold}đ → STOP")
        verdict = "BAD"
        action = "STOP"

    # ── Insight content strategy ─────────────────────────────────────────────
    if ct in ("A1", "A2", "A3") and camp == "mess":
        insights.append("💡 Cân nhắc chuyển sang A4 để tối ưu mess campaign")
    if ct == "A4" and camp == "mess" and action == "SCALE":
        insights.append("🎯 Content A4 đang WIN — nhân nhóm ngay!")
    if spend > 500000 and verdict == "BAD":
        insights.append(f"🔥 Đang đốt {spend:,.0f}đ — dừng ngay để tránh lãng phí")

    return {
        "verdict": verdict,
        "action": action,
        "content_type": ct,
        "camp_type": camp,
        "cost_per_result": cost_per_result,
        "results": results,
        "spend": spend,
        "days_running": days_running,
        "reasons": reasons,
        "warnings": warnings,
        "insights": insights,
    }


# ─── Router: chọn đúng tool theo intent ─────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "get_channels_with_owners",
        "description": "Lấy danh sách tất cả kênh MXH kèm thông tin chủ sở hữu (tên, team). Dùng khi hỏi về ai quản lý kênh gì, team nào có kênh gì.",
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "Lọc theo nền tảng: tiktok, instagram, facebook, youtube. Bỏ trống = tất cả."},
                "limit": {"type": "integer", "description": "Số lượng kết quả tối đa", "default": 30},
            },
        },
    },
    {
        "name": "get_team_channel_summary",
        "description": "Tổng hợp số kênh và follower theo từng team. Dùng khi hỏi so sánh hiệu suất team.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_top_channels",
        "description": "Top kênh theo chỉ số: total_followers, total_views, total_likes, engagement_rate.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "metric": {"type": "string", "enum": ["total_followers", "total_views", "total_likes", "engagement_rate"]},
            },
        },
    },
    {
        "name": "get_facebook_pages",
        "description": "Lấy danh sách Facebook Page đang quản lý (fan count, category).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_instagram_accounts",
        "description": "Lấy danh sách tất cả Instagram Business Account liên kết với Facebook pages đang admin (followers, media count, page liên kết).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_all_instagram_monthly_summary",
        "description": "Tổng hợp hiệu suất tất cả Instagram accounts trong tháng: posts, likes, comments, engagement. Dùng khi hỏi báo cáo Instagram tháng X, so sánh kênh Instagram.",
        "parameters": {
            "type": "object",
            "required": ["year", "month"],
            "properties": {
                "year":  {"type": "integer"},
                "month": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_instagram_monthly_views",
        "description": "Lấy chi tiết impressions, reach, likes, comments của 1 kênh Instagram cụ thể trong tháng.",
        "parameters": {
            "type": "object",
            "required": ["ig_username", "year", "month"],
            "properties": {
                "ig_username": {"type": "string", "description": "Username Instagram (không có @)"},
                "year":  {"type": "integer"},
                "month": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_youtube_channel_stats",
        "description": "Lấy thống kê YouTube channel (subscribers, views, videos).",
        "parameters": {
            "type": "object",
            "required": ["channel_id"],
            "properties": {
                "channel_id": {"type": "string", "description": "YouTube Channel ID"},
            },
        },
    },
    {
        "name": "get_user_channels",
        "description": "Lấy kênh của một nhân viên cụ thể theo tên hoặc email.",
        "parameters": {
            "type": "object",
            "required": ["user_name"],
            "properties": {
                "user_name": {"type": "string", "description": "Tên hoặc email nhân viên"},
            },
        },
    },
    {
        "name": "get_tiktok_monthly_report",
        "description": "Báo cáo traffic TikTok toàn bộ kênh đang hoạt động trong tháng. Trả về: tổng views/likes/comments/shares, top 10 video theo views/likes/comments, breakdown theo team và từng người. Dùng khi hỏi báo cáo traffic TikTok, thống kê kênh TikTok tháng X.",
        "parameters": {
            "type": "object",
            "required": ["year", "month"],
            "properties": {
                "year":  {"type": "integer", "description": "Năm, ví dụ: 2026"},
                "month": {"type": "integer", "description": "Tháng 1-12"},
                "team":  {"type": "string",  "description": "Lọc theo team (tuỳ chọn), ví dụ: Team K1"},
                "owner": {"type": "string",  "description": "Lọc theo tên người quản lý (tuỳ chọn)"},
            },
        },
    },
    {
        "name": "get_channels_monthly_views",
        "description": "Lấy lượt xem thực tế của các kênh trong 1 tháng cụ thể bằng cách fetch video đăng trong tháng đó từ YouTube/Facebook API và DB. Dùng khi hỏi top kênh theo views tháng X, hiệu suất kênh tháng X.",
        "parameters": {
            "type": "object",
            "required": ["year", "month"],
            "properties": {
                "year":  {"type": "integer", "description": "Năm, ví dụ: 2026"},
                "month": {"type": "integer", "description": "Tháng (1-12), ví dụ: 5"},
                "platform": {"type": "string", "description": "Lọc theo platform: youtube, facebook, tiktok, instagram. Bỏ trống = tất cả."},
            },
        },
    },
    {
        "name": "get_youtube_monthly_views",
        "description": "Lấy views YouTube của 1 kênh cụ thể trong tháng: tổng views, danh sách video, likes.",
        "parameters": {
            "type": "object",
            "required": ["channel_identifier", "year", "month"],
            "properties": {
                "channel_identifier": {"type": "string", "description": "Channel ID (UCxxx), handle (@handle), hoặc username"},
                "year":  {"type": "integer"},
                "month": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_fb_ads_summary",
        "description": "Lấy báo cáo tổng hợp quảng cáo Facebook Ads Manager: tổng chi tiêu, số mess, like page, danh sách camp GOOD/WARNING/BAD theo rule VCB. Dùng khi hỏi báo cáo ads, chi tiêu quảng cáo, camp nào đang chạy tốt.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_preset": {
                    "type": "string",
                    "description": "Khoảng thời gian: today, yesterday, last_7d, last_30d, this_month, last_month",
                    "default": "this_month",
                },
            },
        },
    },
    {
        "name": "get_fb_ads_report",
        "description": "Lấy danh sách chi tiết từng campaign quảng cáo Facebook: spend, mess, like, engagement và chi phí từng loại.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_preset": {"type": "string", "default": "this_month"},
                "account_id": {"type": "string", "description": "Ad account ID (tuỳ chọn, tự động lấy nếu bỏ trống)"},
            },
        },
    },
    {
        "name": "analyze_ads_camp",
        "description": "Phân tích hiệu quả camp quảng cáo Facebook theo rule nội bộ VCB. Trả về: verdict (GOOD/WARNING/BAD), action (SCALE/KEEP/TEST/STOP), lý do, cảnh báo và insight chiến lược.",
        "parameters": {
            "type": "object",
            "required": ["content_type", "camp_type", "cost_per_result", "results", "spend", "days_running"],
            "properties": {
                "content_type": {"type": "string", "description": "Loại content: A1, A2, A3, A4, A5"},
                "camp_type": {"type": "string", "description": "Loại camp: like_page, tuong_tac, mess"},
                "cost_per_result": {"type": "number", "description": "Chi phí mỗi kết quả (đồng)"},
                "results": {"type": "integer", "description": "Số kết quả đạt được"},
                "spend": {"type": "number", "description": "Tổng chi tiêu (đồng)"},
                "days_running": {"type": "integer", "description": "Số ngày đã chạy"},
                "views": {"type": "integer", "description": "Số lượt xem (tuỳ chọn)", "default": 0},
                "page_size": {"type": "integer", "description": "Kích thước page (tuỳ chọn)", "default": 0},
            },
        },
    },
]


def call_tool(name: str, args: dict):
    """Dispatch tool call."""
    fn_map = {
        "get_channels_with_owners": lambda a: get_channels_with_owners(a.get("platform"), a.get("limit", 30)),
        "get_team_channel_summary": lambda a: get_team_channel_summary(),
        "get_top_channels": lambda a: get_top_channels(a.get("limit", 10), a.get("metric", "total_followers")),
        "get_facebook_pages": lambda a: get_facebook_pages(),
        "get_instagram_accounts": lambda a: get_instagram_accounts(),
        "get_all_instagram_monthly_summary": lambda a: get_all_instagram_monthly_summary(int(a["year"]), int(a["month"])),
        "get_instagram_monthly_views": lambda a: get_instagram_monthly_views(a["ig_username"], int(a["year"]), int(a["month"])),
        "get_youtube_channel_stats": lambda a: get_youtube_channel_stats(a["channel_id"]),
        "get_user_channels": lambda a: get_user_channels(a["user_name"]),
        "get_tiktok_monthly_report": lambda a: get_tiktok_monthly_report(
            int(a["year"]), int(a["month"]), a.get("team"), a.get("owner")),
        "get_channels_monthly_views": lambda a: get_channels_monthly_views(
            int(a["year"]), int(a["month"]), a.get("platform")),
        "get_youtube_monthly_views": lambda a: get_youtube_monthly_views(
            a["channel_identifier"], int(a["year"]), int(a["month"])),
        "get_fb_ads_summary": lambda a: get_fb_ads_summary(a.get("date_preset", "this_month")),
        "get_fb_ads_report": lambda a: get_fb_ads_report(a.get("date_preset", "this_month"), a.get("account_id")),
        "analyze_ads_camp": lambda a: analyze_ads_camp(
            content_type=a["content_type"],
            camp_type=a["camp_type"],
            cost_per_result=float(a["cost_per_result"]),
            results=int(a["results"]),
            spend=float(a["spend"]),
            days_running=int(a["days_running"]),
            views=int(a.get("views", 0)),
            page_size=int(a.get("page_size", 0)),
        ),
    }
    fn = fn_map.get(name)
    if fn:
        try:
            return fn(args)
        except Exception as e:
            logger.error(f"Tool {name} error: {e}")
            return {"error": str(e)}
    return {"error": f"Unknown tool: {name}"}
