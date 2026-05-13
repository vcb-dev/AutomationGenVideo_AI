import os, sys, requests, psycopg2, psycopg2.extras, time, re
from datetime import datetime, date
from dotenv import load_dotenv

# Load env
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

# Config
META_TOKEN = os.getenv('META_ACCESS_TOKEN')
YT_KEY = os.getenv('YOUTUBE_API_KEY')
DATABASE_URL = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

# Lấy tháng hiện tại để lọc
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

def sync_youtube(ch_meta):
    print(f"[*] Đang quét YouTube tháng {CURRENT_MONTH}...")
    try:
        conn = db_conn(); cur = conn.cursor()
        cur.execute("SELECT channel_id, name, team_traffic, owner FROM huyk_channels WHERE LOWER(platform) LIKE 'youtube%%'")
        channels = cur.fetchall()
        for ch in channels:
            res = requests.get("https://www.googleapis.com/youtube/v3/search", params={"part": "snippet", "channelId": ch['channel_id'], "maxResults": 10, "order": "date", "type": "video", "key": YT_KEY}).json()
            for item in res.get('items', []):
                pub_at = datetime.strptime(item['snippet']['publishedAt'], "%Y-%m-%dT%H:%M:%SZ")
                # Lọc đúng tháng hiện tại
                if pub_at.month != CURRENT_MONTH or pub_at.year != CURRENT_YEAR: continue
                
                vid_id = item['id']['videoId']
                s_res = requests.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "statistics", "id": vid_id, "key": YT_KEY}).json()
                stats = s_res['items'][0]['statistics'] if s_res.get('items') else {}
                title, tags = extract_and_clean_title(item['snippet']['title'])
                
                cur.execute("""
                    INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, video_url, year, month, published_at, synced_at)
                    VALUES (gen_random_uuid(), 'youtube', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, synced_at=NOW()
                """, (vid_id, ch['name'], ch['channel_id'], ch['owner'], ch['team_traffic'], title, tags, int(stats.get('viewCount', 0)), int(stats.get('likeCount', 0)), int(stats.get('commentCount', 0)), f"https://youtube.com/watch?v={vid_id}", pub_at.year, pub_at.month, pub_at))
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi YT: {e}")

def sync_meta(ch_meta):
    print(f"[*] Đang quét Meta tháng {CURRENT_MONTH}...")
    try:
        r_pgs = requests.get("https://graph.facebook.com/v19.0/me/accounts", params={"fields": "name,access_token,id,fan_count,instagram_business_account{id,username,followers_count}", "access_token": META_TOKEN, "limit": 200}).json()
        pages = r_pgs.get('data', [])
        conn = db_conn(); cur = conn.cursor()
        for pg in pages:
            m = ch_meta.get(pg['id']) or ch_meta.get(pg['name'].lower().strip())
            # Facebook
            f_res = requests.get(f"https://graph.facebook.com/v19.0/{pg['id']}/posts", params={"fields": "id,message,created_time,permalink_url,attachments{media},reactions.summary(true),comments.summary(true),insights.metric(post_impressions_unique)", "access_token": pg['access_token'], "limit": 15}).json()
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

                cur.execute("""
                    INSERT INTO social_video_report (id, platform, post_id, channel_name, username, owner, team, title, hashtags, views, likes, comments, followers, video_url, year, month, published_at, synced_at)
                    VALUES (gen_random_uuid(), 'facebook', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (platform, post_id) DO UPDATE SET views=EXCLUDED.views, likes=EXCLUDED.likes, comments=EXCLUDED.comments, followers=EXCLUDED.followers, video_url=EXCLUDED.video_url, owner=EXCLUDED.owner, team=EXCLUDED.team, synced_at=NOW()
                """, (p['id'], pg['name'], pg['id'], m['owner'] if m else None, m['team_traffic'] if m else None, title, tags, views, p.get('reactions', {}).get('summary', {}).get('total_count', 0), p.get('comments', {}).get('summary', {}).get('total_count', 0), pg.get('fan_count', 0), v_url, pub_at.year, pub_at.month, pub_at))
            # Instagram
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
            print(f"    + Đã quét xong: {pg['name']}")
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi Meta: {e}")

def main():
    ch_meta = get_channel_metadata()
    sync_youtube(ch_meta)
    sync_meta(ch_meta)

if __name__ == "__main__":
    main()
