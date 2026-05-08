"""
Fetch real-time data from social platforms + internal DB for VCB Assistant.
"""
import os
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
    """Danh sách kênh + thông tin chủ sở hữu/team từ DB."""
    where = "WHERE tc.is_active = true"
    params = []
    if platform:
        where += " AND LOWER(tc.platform::text) = LOWER(%s)"
        params.append(platform)

    sql = f"""
        SELECT
            tc.id, tc.platform::text AS platform, tc.username, tc.display_name,
            tc.total_followers, tc.total_views, tc.total_likes, tc.total_videos,
            tc.engagement_rate, tc.last_synced_at,
            u.full_name AS owner_name, u.email AS owner_email,
            u.team, u.employee_position, u.roles
        FROM tracked_channels tc
        LEFT JOIN users u ON u.id = tc.user_id
        {where}
        ORDER BY tc.total_followers DESC NULLS LAST
        LIMIT %s
    """
    params.append(limit)
    return _db_query(sql, params)


def get_team_channel_summary() -> list:
    """Tổng hợp số kênh + follower theo team."""
    sql = """
        SELECT
            COALESCE(u.team, 'Chưa phân team') AS team,
            COUNT(tc.id) AS total_channels,
            COUNT(DISTINCT tc.platform) AS platforms,
            SUM(tc.total_followers) AS total_followers,
            SUM(tc.total_views) AS total_views,
            AVG(tc.engagement_rate) AS avg_engagement
        FROM tracked_channels tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE tc.is_active = true
        GROUP BY u.team
        ORDER BY total_followers DESC NULLS LAST
    """
    return _db_query(sql)


def get_top_channels(limit: int = 10, metric: str = 'total_followers') -> list:
    allowed = {'total_followers', 'total_views', 'total_likes', 'engagement_rate'}
    col = metric if metric in allowed else 'total_followers'
    sql = f"""
        SELECT
            tc.platform::text, tc.username, tc.display_name,
            tc.total_followers, tc.total_views, tc.engagement_rate,
            u.full_name AS owner, u.team
        FROM tracked_channels tc
        LEFT JOIN users u ON u.id = tc.user_id
        WHERE tc.is_active = true AND tc.{col} IS NOT NULL
        ORDER BY tc.{col} DESC NULLS LAST
        LIMIT %s
    """
    return _db_query(sql, [limit])


def get_user_channels(user_name: str) -> list:
    """Kênh của một nhân viên cụ thể."""
    sql = """
        SELECT tc.platform::text, tc.username, tc.display_name,
               tc.total_followers, tc.total_views, tc.engagement_rate
        FROM tracked_channels tc
        JOIN users u ON u.id = tc.user_id
        WHERE tc.is_active = true
          AND (LOWER(u.full_name) LIKE LOWER(%s) OR LOWER(u.email) LIKE LOWER(%s))
        ORDER BY tc.total_followers DESC NULLS LAST
    """
    pattern = f"%{user_name}%"
    return _db_query(sql, [pattern, pattern])


# ─── Facebook Graph API ──────────────────────────────────────────────────────

FB_BASE = "https://graph.facebook.com/v19.0"

def _fb_token() -> str:
    return _env('FACEBOOK_ACCESS_TOKEN')


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


# ─── Instagram Graph API ─────────────────────────────────────────────────────

def _ig_token() -> str:
    return _env('INSTAGRAM_ACCESS_TOKEN')


def get_instagram_accounts() -> list:
    """Lấy IG business accounts."""
    try:
        r = requests.get(f"{FB_BASE}/me/accounts", params={
            "access_token": _ig_token(),
            "fields": "id,name,instagram_business_account{id,username,followers_count,media_count,profile_picture_url}",
        }, timeout=15)
        r.raise_for_status()
        pages = r.json().get("data", [])
        accounts = []
        for p in pages:
            ig = p.get("instagram_business_account")
            if ig:
                accounts.append({**ig, "page_name": p.get("name")})
        return accounts
    except Exception as e:
        logger.error(f"IG accounts error: {e}")
        return []


def get_instagram_insights(ig_user_id: str, days: int = 30) -> dict:
    """Insights của 1 IG business account."""
    try:
        r = requests.get(f"{FB_BASE}/{ig_user_id}/insights", params={
            "access_token": _ig_token(),
            "metric": "impressions,reach,profile_views,follower_count",
            "period": "day",
            "since": __import__('datetime').date.today() - __import__('datetime').timedelta(days=days),
            "until": __import__('datetime').date.today(),
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
        "description": "Lấy danh sách Instagram Business Account (followers, media count).",
        "parameters": {"type": "object", "properties": {}},
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
        "get_youtube_channel_stats": lambda a: get_youtube_channel_stats(a["channel_id"]),
        "get_user_channels": lambda a: get_user_channels(a["user_name"]),
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
