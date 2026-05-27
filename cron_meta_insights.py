import os, sys, requests, psycopg2, psycopg2.extras, time, re, json, urllib.parse
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv

# Load env
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

# Config
META_TOKEN      = os.getenv('META_ACCESS_TOKEN')
YT_API_KEY      = os.getenv('YOUTUBE_API_KEY')
TT_ADS_TOKEN    = os.getenv('TIKTOK_ACCESS_TOKEN')
TT_ORGANIC_TOKEN= os.getenv('TIKTOK_ORGANIC_TOKEN')
TT_BC_ID        = os.getenv('TIKTOK_BC_ID', '7274810417535270913')
DATABASE_URL    = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

# Lấy tháng hiện tại
CURRENT_MONTH = datetime.now().month
CURRENT_YEAR  = datetime.now().year

# ── Buffer window ──────────────────────────────────────────────────────────────
# Video đăng N ngày cuối tháng cần thêm thời gian tích lũy view → crawl lại
# trong N ngày đầu tháng sau để cập nhật số liệu trước khi chốt báo cáo.
BUFFER_DAYS  = 7
_month_start = datetime(CURRENT_YEAR, CURRENT_MONTH, 1)
BUFFER_START = (_month_start - timedelta(days=BUFFER_DAYS)).date()
# Ví dụ: crawl ngày 5/5 → BUFFER_START = 24/4
# → vẫn cập nhật views cho video đăng 24/4–30/4 trước khi chốt tháng 4

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def extract_and_clean_title(text):
    if not text: return "", []
    hashtags = re.findall(r"#(\w+)", text)
    clean_title = re.sub(r"#\w+", "", text).strip()
    return clean_title, hashtags

def get_channel_metadata():
    meta = {}
    try:
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT channel_id, name, team_traffic, owner FROM huyk_channels")
        for r in cur.fetchall():
            if r['channel_id']: meta[r['channel_id']] = r
            if r['name']: meta[r['name'].lower().strip()] = r
        conn.close()
    except Exception as e: print(f"[!] Lỗi Metadata: {e}")
    return meta

# ── YouTube Sync (YouTube Data API v3) ───────────────────────────────────────
def sync_youtube(ch_meta):
    if not YT_API_KEY:
        print("[!] YOUTUBE_API_KEY chưa cấu hình — bỏ qua YouTube sync")
        return

    print(f"[*] Đang quét YouTube tháng {CURRENT_MONTH}/{CURRENT_YEAR} (Data API v3)...")
    YT_BASE = "https://www.googleapis.com/youtube/v3"

    def yt_get(endpoint, params):
        params['key'] = YT_API_KEY
        r = requests.get(f"{YT_BASE}/{endpoint}", params=params, timeout=15)
        if r.status_code == 403:
            raise Exception(f"YouTube API 403 — hết quota hoặc key không hợp lệ")
        return r.json()

    def resolve_channel(raw_id):
        """Trả về (channel_id, subscribers, uploads_playlist_id) từ UC... hoặc @handle."""
        raw_id = raw_id.strip().replace(" ", "")
        if raw_id.startswith("UC"):
            params = {"part": "statistics,contentDetails", "id": raw_id}
        else:
            handle = raw_id if raw_id.startswith("@") else f"@{raw_id}"
            params = {"part": "statistics,contentDetails", "forHandle": handle}
        data = yt_get("channels", params)
        items = data.get("items", [])
        if not items:
            return None, 0, None
        item = items[0]
        ch_id      = item["id"]
        subs       = int(item.get("statistics", {}).get("subscriberCount", 0))
        uploads_pl = item["contentDetails"]["relatedPlaylists"]["uploads"]
        return ch_id, subs, uploads_pl

    try:
        conn = db_conn()
        with conn.cursor() as sel:
            sel.execute("SELECT channel_id, link_channel, name, team_traffic, owner FROM huyk_channels WHERE LOWER(platform) LIKE 'youtube%%'")
            channels = sel.fetchall()
        print(f"    Tìm thấy {len(channels)} kênh YouTube")

        for ch in channels:
            # Trích raw_id từ link_channel trước (ưu tiên URL), fallback sang channel_id
            raw_id = ''
            link = (ch['link_channel'] or '').strip()
            if link:
                # Dạng 1: youtube.com/@handle
                m = re.search(r'youtube\.com/(@[^/?&\s]+)', link)
                if m:
                    raw_id = urllib.parse.unquote(m.group(1))
                else:
                    # Dạng 2: youtube.com/channel/UCxxx
                    m2 = re.search(r'youtube\.com/channel/(UC[^/?&\s]+)', link)
                    if m2:
                        raw_id = m2.group(1)

            # Fallback sang cột channel_id nếu link không trích được
            if not raw_id:
                raw_id = (ch['channel_id'] or '').strip()

            if not raw_id:
                print(f"    [!] Bỏ qua {ch['name']}: không có link_channel lẫn channel_id")
                continue
            try:
                # 1. Lấy channel ID thực + subscriber count + uploads playlist
                channel_id, subscribers, uploads_pl = resolve_channel(raw_id)
                if not channel_id:
                    print(f"    [!] Không tìm thấy kênh: {ch['name']} ({raw_id})")
                    continue

                # 2. Duyệt uploads playlist, lấy video của tháng hiện tại
                videos_this_month = []
                page_token = None
                while True:
                    params = {"part": "snippet,contentDetails", "playlistId": uploads_pl, "maxResults": 50}
                    if page_token:
                        params["pageToken"] = page_token
                    pl = yt_get("playlistItems", params)

                    stop_paging = False
                    for item in pl.get("items", []):
                        pub_str = item["snippet"].get("publishedAt", "")
                        if not pub_str:
                            continue
                        pub_dt = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                        # Dừng khi video cũ hơn cửa sổ buffer (không cần đọc thêm)
                        if pub_dt.date() < BUFFER_START:
                            stop_paging = True
                            break
                        # Thu thập tất cả video trong cửa sổ:
                        # tháng hiện tại + N ngày cuối tháng trước (buffer)
                        videos_this_month.append({
                            "video_id":    item["contentDetails"]["videoId"],
                            "title":       item["snippet"]["title"],
                            "published_at": pub_dt,
                        })

                    if stop_paging or not pl.get("nextPageToken"):
                        break
                    page_token = pl["nextPageToken"]

                if not videos_this_month:
                    print(f"    - {ch['name']}: không có video tháng {CURRENT_MONTH} (kể cả buffer)")
                    continue

                # 3. Lấy statistics theo batch 50
                video_ids  = [v["video_id"] for v in videos_this_month]
                stats_map  = {}
                for i in range(0, len(video_ids), 50):
                    batch = video_ids[i:i + 50]
                    vdata = yt_get("videos", {"part": "statistics", "id": ",".join(batch)})
                    for vi in vdata.get("items", []):
                        s = vi.get("statistics", {})
                        stats_map[vi["id"]] = {
                            "views":    int(s.get("viewCount",    0)),
                            "likes":    int(s.get("likeCount",    0)),
                            "comments": int(s.get("commentCount", 0)),
                        }

                # 4. Upsert vào social_video_report — dùng cursor riêng
                saved = 0
                active_post_ids = []
                with conn.cursor() as cur:
                    for v in videos_this_month:
                        vid   = v["video_id"]
                        s     = stats_map.get(vid, {"views": 0, "likes": 0, "comments": 0})
                        title, tags = extract_and_clean_title(v["title"])
                        cur.execute("""
                            INSERT INTO social_video_report
                              (id, platform, post_id, channel_name, username, owner, team,
                               title, hashtags, views, likes, comments, shares, followers,
                               video_url, year, month, published_at, source, synced_at,
                               is_active, deleted_at)
                            VALUES
                              (gen_random_uuid(), 'youtube', %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, 0, %s,
                               %s, %s, %s, %s, 'api', NOW(), TRUE, NULL)
                            ON CONFLICT (platform, post_id) DO UPDATE SET
                              views=EXCLUDED.views, likes=EXCLUDED.likes,
                              comments=EXCLUDED.comments, followers=EXCLUDED.followers,
                              title=EXCLUDED.title, synced_at=NOW(),
                              is_active=TRUE, deleted_at=NULL
                        """, (
                            vid, ch['name'], channel_id,
                            ch['owner'], ch['team_traffic'],
                            title, tags,
                            s['views'], s['likes'], s['comments'],
                            subscribers,
                            f"https://youtube.com/watch?v={vid}",
                            v['published_at'].year, v['published_at'].month, v['published_at'],
                        ))
                        active_post_ids.append(vid)
                        saved += 1

                    # Đánh dấu video bị xóa — kiểm tra từng tháng riêng trong cửa sổ
                    # (tháng hiện tại + tháng buffer) để không nhầm tháng
                    months_in_window = set(
                        (v["published_at"].year, v["published_at"].month)
                        for v in videos_this_month
                    )
                    for (y, m_w) in months_in_window:
                        ids_in_month = [
                            v["video_id"] for v in videos_this_month
                            if v["published_at"].year == y and v["published_at"].month == m_w
                        ]
                        if ids_in_month:
                            cur.execute("""
                                UPDATE social_video_report
                                SET is_active=FALSE, deleted_at=NOW()
                                WHERE platform='youtube'
                                  AND username=%s
                                  AND year=%s AND month=%s
                                  AND is_active=TRUE
                                  AND post_id NOT IN %s
                            """, (channel_id, y, m_w, tuple(ids_in_month)))

                conn.commit()
                print(f"    + {ch['name']}: {saved} video | {subscribers:,} subscribers")
                time.sleep(0.3)  # tránh rate limit

            except Exception as e:
                conn.rollback()  # reset transaction để kênh tiếp theo không bị ảnh hưởng
                print(f"    [!] Lỗi kênh {ch['name']}: {e}")
                continue

        conn.close()
        print(f"[*] YouTube sync hoàn tất.")

    except Exception as e:
        print(f"[!] Lỗi hệ thống YouTube: {e}")

# ── Meta Sync (Giữ nguyên) ──────────────────────────────────────────────────
def sync_meta(ch_meta):
    print(f"[*] Đang quét Meta tháng {CURRENT_MONTH}...")
    try:
        r_pgs = requests.get("https://graph.facebook.com/v19.0/me/accounts", params={"fields": "name,access_token,id,fan_count,instagram_business_account{id,username,followers_count}", "access_token": META_TOKEN, "limit": 200}).json()
        pages = r_pgs.get('data', [])
        conn = db_conn(); cur = conn.cursor()
        for pg in pages:
            m = ch_meta.get(pg['id']) or ch_meta.get(pg['name'].lower().strip())
            _buf_since = int(datetime(BUFFER_START.year, BUFFER_START.month, BUFFER_START.day).timestamp())
            f_res = requests.get(f"https://graph.facebook.com/v19.0/{pg['id']}/posts", params={"fields": "id,message,created_time,permalink_url,attachments{media},reactions.summary(true),comments.summary(true),shares,insights.metric(post_impressions_unique)", "access_token": pg['access_token'], "limit": 50, "since": _buf_since}).json()
            fb_active_ids = []        # Tất cả ID trong cửa sổ (upsert)
            fb_current_ids = []       # Chỉ tháng hiện tại (dùng cho deletion check)
            for p in f_res.get('data', []):
                pub_at = datetime.strptime(p['created_time'], "%Y-%m-%dT%H:%M:%S%z")
                # Bỏ qua post cũ hơn cửa sổ buffer
                if pub_at.date() < BUFFER_START: continue
                title, tags = extract_and_clean_title(p.get('message', ''))
                v_url = p.get('permalink_url')
                atts = p.get('attachments', {}).get('data', [])
                if atts: v_url = atts[0].get('media', {}).get('source') or v_url
                views = 0
                for ins in p.get('insights', {}).get('data', []):
                    if ins['name'] == 'post_impressions_unique': views = ins['values'][0]['value']
                share_count = p.get('shares', {}).get('count', 0)
                cur.execute("""
                    INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, shares, followers, video_url, year, month, published_at, synced_at, is_active, deleted_at)
                    VALUES (gen_random_uuid(), 'facebook', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), TRUE, NULL)
                    ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, shares=EXCLUDED.shares, followers=EXCLUDED.followers, video_url=EXCLUDED.video_url, owner=EXCLUDED.owner, team=EXCLUDED.team, synced_at=NOW(), is_active=TRUE, deleted_at=NULL
                """, (p['id'], pg['name'], pg['id'], m['owner'] if m else None, m['team_traffic'] if m else None, title, tags, views, p.get('reactions', {}).get('summary', {}).get('total_count', 0), p.get('comments', {}).get('summary', {}).get('total_count', 0), share_count, pg.get('fan_count', 0), v_url, pub_at.year, pub_at.month, pub_at))
                fb_active_ids.append(p['id'])
                if pub_at.year == CURRENT_YEAR and pub_at.month == CURRENT_MONTH:
                    fb_current_ids.append(p['id'])
            # Đánh dấu post FB bị xóa — chỉ check tháng hiện tại
            # (buffer tháng trước không cần check vì đã xử lý trong tháng đó)
            if fb_current_ids:
                cur.execute("""
                    UPDATE social_video_report SET is_active=FALSE, deleted_at=NOW()
                    WHERE platform='facebook' AND username=%s AND year=%s AND month=%s
                      AND is_active=TRUE AND post_id NOT IN %s
                """, (pg['id'], CURRENT_YEAR, CURRENT_MONTH, tuple(fb_current_ids)))
            if 'instagram_business_account' in pg:
                ig = pg['instagram_business_account']
                m_ig = ch_meta.get(ig['id']) or ch_meta.get(ig['username'].lower().strip())
                # Lấy insights (impressions + video_views) để có view count
                ig_res = requests.get(
                    f"https://graph.facebook.com/v19.0/{ig['id']}/media",
                    params={
                        "fields": "id,caption,timestamp,permalink,media_url,media_type,like_count,comments_count,insights.metric(reach,impressions,video_views,plays)",
                        "access_token": pg['access_token'],
                        "limit": 50,
                        "since": _buf_since,
                    }
                ).json()
                ig_current_ids = []
                for mi in ig_res.get('data', []):
                    pub_at = datetime.strptime(mi['timestamp'], "%Y-%m-%dT%H:%M:%S%z")
                    # Bỏ qua media cũ hơn cửa sổ buffer
                    if pub_at.date() < BUFFER_START: continue
                    title, tags = extract_and_clean_title(mi.get('caption', ''))

                    # Instagram API trả về: reach > plays/video_views > impressions
                    # reach = số tài khoản unique đã xem (tương đương views)
                    views = 0
                    for ins in mi.get('insights', {}).get('data', []):
                        val = ins.get('values', [{}])[0].get('value', 0) if ins.get('values') else ins.get('value', 0)
                        val = int(val or 0)
                        if ins['name'] in ('plays', 'video_views') and val > 0:
                            views = max(views, val)
                        elif ins['name'] == 'reach' and views == 0:
                            views = val
                        elif ins['name'] == 'impressions' and views == 0:
                            views = val

                    cur.execute("""
                        INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, followers, video_url, year, month, published_at, synced_at, is_active, deleted_at)
                        VALUES (gen_random_uuid(), 'instagram', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), TRUE, NULL)
                        ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, followers=EXCLUDED.followers, video_url=EXCLUDED.video_url, owner=EXCLUDED.owner, team=EXCLUDED.team, synced_at=NOW(), is_active=TRUE, deleted_at=NULL
                    """, (mi['id'], ig.get('username'), ig['id'], m_ig['owner'] if m_ig else None, m_ig['team_traffic'] if m_ig else None, title, tags, views, mi.get('like_count', 0), mi.get('comments_count', 0), ig.get('followers_count', 0), mi.get('media_url') or mi.get('permalink'), pub_at.year, pub_at.month, pub_at))
                    # Track riêng ID tháng hiện tại để deletion check
                    if pub_at.year == CURRENT_YEAR and pub_at.month == CURRENT_MONTH:
                        ig_current_ids.append(mi['id'])
                # Đánh dấu post IG bị xóa — chỉ check tháng hiện tại
                if ig_current_ids:
                    cur.execute("""
                        UPDATE social_video_report SET is_active=FALSE, deleted_at=NOW()
                        WHERE platform='instagram' AND username=%s AND year=%s AND month=%s
                          AND is_active=TRUE AND post_id NOT IN %s
                    """, (ig.get('username'), CURRENT_YEAR, CURRENT_MONTH, tuple(ig_current_ids)))
            print(f"    + Đã xong: {pg['name']}")
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi Meta: {e}")

# ── TikTok Organic Sync ──────────────────────────────────────────────────────
def sync_tiktok():
    if not TT_ORGANIC_TOKEN:
        print("[!] TIKTOK_ORGANIC_TOKEN chưa có — bỏ qua TikTok sync")
        print("    Chạy: python get_organic_token.py để lấy token trước.")
        return
    if not TT_ADS_TOKEN:
        print("[!] TIKTOK_ACCESS_TOKEN chưa có — bỏ qua TikTok sync")
        return

    TT_BASE = 'https://business-api.tiktok.com/open_api/v1.3'

    def tt_get(path, params):
        r = requests.get(f"{TT_BASE}{path}", params=params,
                         headers={'Access-Token': TT_ADS_TOKEN}, timeout=20)
        return r.json()

    def tt_post_organic(path, payload):
        r = requests.post(f"{TT_BASE}{path}", json=payload,
                          headers={'Access-Token': TT_ORGANIC_TOKEN,
                                   'Content-Type': 'application/json'}, timeout=20)
        return r.json()

    print(f"[*] Đang quét TikTok tháng {CURRENT_MONTH}/{CURRENT_YEAR}...")

    # 1. Lấy danh sách kênh từ Business Center
    accounts = []
    cursor   = None
    while True:
        params = {'bc_id': TT_BC_ID, 'asset_type': 'TT_ACCOUNT', 'page_size': 50}
        if cursor:
            params['cursor'] = cursor
        res = tt_get('/bc/asset/get/', params)
        if res.get('code') != 0:
            print(f"    [!] Lỗi lấy kênh BC: {res.get('message')}")
            break
        data = res.get('data', {})
        accounts.extend(data.get('list', []))
        if not data.get('page_info', {}).get('has_more'):
            break
        cursor = data['page_info'].get('cursor')
        time.sleep(0.3)
    print(f"    Tìm thấy {len(accounts)} kênh TikTok")

    # Metadata owner/team từ huyk_channels
    try:
        conn = db_conn(); cur = conn.cursor()
        cur.execute("""
            SELECT name, owner, team_traffic FROM huyk_channels
            WHERE LOWER(platform) LIKE '%tiktok%'
              AND status IN ('Đang hoạt động', 'ON')
        """)
        tt_meta = {r['name'].strip().lower(): r for r in cur.fetchall()}
    except Exception as e:
        print(f"    [!] Lỗi load metadata TikTok: {e}")
        return

    # Cửa sổ crawl mở rộng: từ BUFFER_START đến cuối tháng hiện tại
    # Buffer giúp cập nhật view cho video đăng cuối tháng trước
    TZ_VN    = timezone(timedelta(hours=7))
    start_dt = datetime(BUFFER_START.year, BUFFER_START.month, BUFFER_START.day, tzinfo=TZ_VN)
    end_dt   = (datetime(CURRENT_YEAR + 1, 1, 1, tzinfo=TZ_VN)
                if CURRENT_MONTH == 12
                else datetime(CURRENT_YEAR, CURRENT_MONTH + 1, 1, tzinfo=TZ_VN))
    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    total_saved = 0
    for acc in accounts:
        business_id  = str(acc.get('asset_id', ''))
        display_name = acc.get('asset_name', business_id)
        name_key     = display_name.strip().lower()
        m            = tt_meta.get(name_key, {})

        # Lấy followers qua organic token
        followers = 0
        try:
            info_res = requests.get(
                f"{TT_BASE}/business/get/",
                params={'business_id': business_id},
                headers={'Access-Token': TT_ORGANIC_TOKEN}, timeout=15
            ).json()
            if info_res.get('code') == 0:
                d = info_res.get('data', {})
                followers    = d.get('follower_count', 0)
                display_name = d.get('display_name', display_name)
        except Exception as e:
            print(f"    [!] Lỗi /business/get/ {display_name}: {e}")

        # Lấy danh sách video trong tháng
        videos = []
        vid_cursor = None
        while True:
            payload = {
                'business_id': business_id,
                'fields': json.dumps([
                    'item_id', 'caption', 'video_views', 'likes',
                    'comment_count', 'share_count', 'create_time',
                    'share_url', 'reach',
                ]),
                'filters': json.dumps({
                    'create_time': {'min': start_ts, 'max': end_ts}
                }),
                'page_size': 50,
            }
            if vid_cursor:
                payload['cursor'] = vid_cursor
            vres = tt_post_organic('/business/video/list/', payload)
            if vres.get('code') != 0:
                print(f"    [!] Lỗi video list {display_name}: {vres.get('message')} (code {vres.get('code')})")
                break
            vdata = vres.get('data', {})
            batch = vdata.get('videos', []) or vdata.get('list', [])
            videos.extend(batch)
            if not vdata.get('has_more'):
                break
            vid_cursor = vdata.get('cursor')
            time.sleep(0.2)

        if not videos:
            print(f"    - {display_name}: không có video tháng {CURRENT_MONTH}")
            continue

        # Upsert từng video
        saved = 0
        for v in videos:
            vid_id    = str(v.get('item_id', ''))
            caption   = v.get('caption', '')
            title, tags = extract_and_clean_title(caption)
            views     = int(v.get('video_views', 0) or v.get('reach', 0) or 0)
            likes     = int(v.get('likes', 0) or 0)
            comments  = int(v.get('comment_count', 0) or 0)
            shares    = int(v.get('share_count', 0) or 0)
            create_ts = v.get('create_time', 0)
            published = (datetime.fromtimestamp(create_ts, tz=TZ_VN)
                         if create_ts else datetime.now(TZ_VN))
            video_url = v.get('share_url', f'https://www.tiktok.com/@{business_id}/video/{vid_id}')

            try:
                cur.execute("""
                    INSERT INTO social_video_report
                      (id, platform, post_id, channel_name, username, owner, team,
                       title, hashtags, views, likes, comments, shares, followers,
                       video_url, year, month, published_at, source, synced_at)
                    VALUES
                      (gen_random_uuid(), 'tiktok', %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, 'api', NOW())
                    ON CONFLICT (platform, post_id) DO UPDATE SET
                      views=EXCLUDED.views, likes=EXCLUDED.likes,
                      comments=EXCLUDED.comments, shares=EXCLUDED.shares,
                      followers=EXCLUDED.followers, title=EXCLUDED.title,
                      synced_at=NOW()
                """, (
                    vid_id, display_name, business_id,
                    m.get('owner', ''), m.get('team_traffic', ''),
                    title or caption[:500], tags,
                    views, likes, comments, shares, int(followers or 0),
                    video_url, published.year, published.month, published,
                ))
                saved += 1
            except Exception as e:
                print(f"    [!] Lỗi upsert video {vid_id}: {e}")
                conn.rollback()

        conn.commit()
        total_saved += saved
        print(f"    + {display_name}: {saved} video | {followers:,} followers")
        time.sleep(0.3)

    cur.close(); conn.close()
    print(f"[*] TikTok sync hoàn tất: {total_saved} video.")


def main():
    ch_meta = get_channel_metadata()
    sync_youtube(ch_meta)
    sync_meta(ch_meta)
    sync_tiktok()

if __name__ == "__main__":
    main()
