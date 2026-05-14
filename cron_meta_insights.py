import os, sys, requests, psycopg2, psycopg2.extras, time, re
from datetime import datetime, date
from dotenv import load_dotenv

# Load env
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

# Config
META_TOKEN    = os.getenv('META_ACCESS_TOKEN')
YT_API_KEY    = os.getenv('YOUTUBE_API_KEY')
DATABASE_URL  = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

# Lấy tháng hiện tại
CURRENT_MONTH = datetime.now().month
CURRENT_YEAR = datetime.now().year

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
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT channel_id, name, team_traffic, owner FROM huyk_channels WHERE LOWER(platform) LIKE 'youtube%%'")
        channels = cur.fetchall()
        print(f"    Tìm thấy {len(channels)} kênh YouTube")

        for ch in channels:
            raw_id = (ch['channel_id'] or '').strip()
            if not raw_id:
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
                        # Video cũ hơn tháng cần lấy → dừng phân trang
                        if (pub_dt.year, pub_dt.month) < (CURRENT_YEAR, CURRENT_MONTH):
                            stop_paging = True
                            break
                        if pub_dt.year == CURRENT_YEAR and pub_dt.month == CURRENT_MONTH:
                            videos_this_month.append({
                                "video_id":    item["contentDetails"]["videoId"],
                                "title":       item["snippet"]["title"],
                                "published_at": pub_dt,
                            })

                    if stop_paging or not pl.get("nextPageToken"):
                        break
                    page_token = pl["nextPageToken"]

                if not videos_this_month:
                    print(f"    - {ch['name']}: không có video tháng {CURRENT_MONTH}")
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

                # 4. Upsert vào social_video_report
                saved = 0
                for v in videos_this_month:
                    vid   = v["video_id"]
                    s     = stats_map.get(vid, {"views": 0, "likes": 0, "comments": 0})
                    title, tags = extract_and_clean_title(v["title"])
                    cur.execute("""
                        INSERT INTO social_video_report
                          (id, platform, post_id, channel_name, username, owner, team,
                           title, hashtags, views, likes, comments, shares, followers,
                           video_url, year, month, published_at, source, synced_at)
                        VALUES
                          (gen_random_uuid(), 'youtube', %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, 0, %s,
                           %s, %s, %s, %s, 'api', NOW())
                        ON CONFLICT (platform, post_id) DO UPDATE SET
                          views=EXCLUDED.views, likes=EXCLUDED.likes,
                          comments=EXCLUDED.comments, followers=EXCLUDED.followers,
                          title=EXCLUDED.title, synced_at=NOW()
                    """, (
                        vid, ch['name'], channel_id,
                        ch['owner'], ch['team_traffic'],
                        title, tags,
                        s['views'], s['likes'], s['comments'],
                        subscribers,
                        f"https://youtube.com/watch?v={vid}",
                        CURRENT_YEAR, CURRENT_MONTH, v['published_at'],
                    ))
                    saved += 1

                conn.commit()
                print(f"    + {ch['name']}: {saved} video | {subscribers:,} subscribers")
                time.sleep(0.3)  # tránh rate limit

            except Exception as e:
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
            f_res = requests.get(f"https://graph.facebook.com/v19.0/{pg['id']}/posts", params={"fields": "id,message,created_time,permalink_url,attachments{media},reactions.summary(true),comments.summary(true),shares,insights.metric(post_impressions_unique)", "access_token": pg['access_token'], "limit": 15}).json()
            for p in f_res.get('data', []):
                pub_at = datetime.strptime(p['created_time'], "%Y-%m-%dT%H:%M:%S%z")
                if pub_at.month != CURRENT_MONTH or pub_at.year != CURRENT_YEAR: continue
                title, tags = extract_and_clean_title(p.get('message', ''))
                v_url = p.get('permalink_url')
                atts = p.get('attachments', {}).get('data', [])
                if atts: v_url = atts[0].get('media', {}).get('source') or v_url
                views = 0
                for ins in p.get('insights', {}).get('data', []):
                    if ins['name'] == 'post_impressions_unique': views = ins['values'][0]['value']
                share_count = p.get('shares', {}).get('count', 0)
                cur.execute("""
                    INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, shares, followers, video_url, year, month, published_at, synced_at)
                    VALUES (gen_random_uuid(), 'facebook', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, shares=EXCLUDED.shares, followers=EXCLUDED.followers, video_url=EXCLUDED.video_url, owner=EXCLUDED.owner, team=EXCLUDED.team, synced_at=NOW()
                """, (p['id'], pg['name'], pg['id'], m['owner'] if m else None, m['team_traffic'] if m else None, title, tags, views, p.get('reactions', {}).get('summary', {}).get('total_count', 0), p.get('comments', {}).get('summary', {}).get('total_count', 0), share_count, pg.get('fan_count', 0), v_url, pub_at.year, pub_at.month, pub_at))
            if 'instagram_business_account' in pg:
                ig = pg['instagram_business_account']
                m_ig = ch_meta.get(ig['id']) or ch_meta.get(ig['username'].lower().strip())
                ig_res = requests.get(f"https://graph.facebook.com/v19.0/{ig['id']}/media", params={"fields": "id,caption,timestamp,permalink,media_url,like_count,comments_count", "access_token": pg['access_token'], "limit": 15}).json()
                for mi in ig_res.get('data', []):
                    pub_at = datetime.strptime(mi['timestamp'], "%Y-%m-%dT%H:%M:%S%z")
                    if pub_at.month != CURRENT_MONTH or pub_at.year != CURRENT_YEAR: continue
                    title, tags = extract_and_clean_title(mi.get('caption', ''))
                    cur.execute("""
                        INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, followers, video_url, year, month, published_at, synced_at)
                        VALUES (gen_random_uuid(), 'instagram', %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, followers=EXCLUDED.followers, video_url=EXCLUDED.video_url, owner=EXCLUDED.owner, team=EXCLUDED.team, synced_at=NOW()
                    """, (mi['id'], ig.get('username'), ig['id'], m_ig['owner'] if m_ig else None, m_ig['team_traffic'] if m_ig else None, title, tags, mi.get('like_count', 0), mi.get('comments_count', 0), ig.get('followers_count', 0), mi.get('media_url') or mi.get('permalink'), pub_at.year, pub_at.month, pub_at))
            print(f"    + Đã xong: {pg['name']}")
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi Meta: {e}")

def main():
    ch_meta = get_channel_metadata()
    sync_youtube(ch_meta)
    sync_meta(ch_meta)

if __name__ == "__main__":
    main()
