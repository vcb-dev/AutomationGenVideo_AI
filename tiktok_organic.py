"""
Crawl TikTok organic data (video views, likes, comments, shares, followers)
và lưu vào bảng social_video_report.

Cách dùng:
  python tiktok_organic.py                    # tháng hiện tại
  python tiktok_organic.py --year 2026 --month 4   # tháng cụ thể
  python tiktok_organic.py --html             # xuất thêm HTML dashboard
"""
import os, sys, re, json, time, argparse, uuid
import requests, psycopg2, psycopg2.extras
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(ENV_PATH)

APP_ID     = os.getenv('TIKTOK_APP_ID',     '7629607469592870929')
APP_SECRET = os.getenv('TIKTOK_APP_SECRET', '158587a761bfefcd2799a585c98c2134aa38b95a')
BC_ID      = os.getenv('TIKTOK_BC_ID',      '7274810417535270913')
ADS_TOKEN  = os.getenv('TIKTOK_ACCESS_TOKEN')
DATABASE_URL = (os.getenv('DIRECT_URL') or
                os.getenv('DATABASE_URL', '').replace('?pgbouncer=true', ''))

BASE_URL   = 'https://business-api.tiktok.com/open_api/v1.3'
TZ_VN      = timezone(timedelta(hours=7))

NOW = datetime.now(TZ_VN)
CURRENT_YEAR  = NOW.year
CURRENT_MONTH = NOW.month

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_channel_metadata(conn) -> dict:
    """Lấy owner, team_traffic của từng kênh TikTok từ huyk_channels."""
    meta = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, owner, team_traffic
            FROM huyk_channels
            WHERE LOWER(platform) LIKE '%tiktok%'
              AND status IN ('Đang hoạt động', 'ON')
        """)
        for row in cur.fetchall():
            meta[row['name'].strip().lower()] = {
                'owner': row['owner'] or '',
                'team':  row['team_traffic'] or '',
            }
    return meta


# ---------------------------------------------------------------------------
# TikTok API helpers
# ---------------------------------------------------------------------------
def tt_get(path: str, params: dict, token: str) -> dict:
    """GET request tới TikTok Business API."""
    r = requests.get(
        f"{BASE_URL}{path}",
        params=params,
        headers={'Access-Token': token},
        timeout=20,
    )
    return r.json()


def tt_post(path: str, payload: dict, token: str) -> dict:
    """POST request tới TikTok Business API."""
    r = requests.post(
        f"{BASE_URL}{path}",
        json=payload,
        headers={'Access-Token': token, 'Content-Type': 'application/json'},
        timeout=20,
    )
    return r.json()


# ---------------------------------------------------------------------------
# Bước 1 – Lấy danh sách kênh TikTok qua Ads token (BC asset)
# ---------------------------------------------------------------------------
def get_tt_accounts_from_bc() -> list:
    """Lấy tất cả TikTok account trong Business Center."""
    if not ADS_TOKEN:
        print("[!] Thiếu TIKTOK_ACCESS_TOKEN trong .env")
        return []

    accounts = []
    cursor   = None

    while True:
        params = {
            'bc_id':      BC_ID,
            'asset_type': 'TT_ACCOUNT',
            'page_size':  50,
        }
        if cursor:
            params['cursor'] = cursor

        res = tt_get('/bc/asset/get/', params, ADS_TOKEN)
        if res.get('code') != 0:
            print(f"[!] Lỗi lấy danh sách kênh BC: {res.get('message')}")
            break

        data = res.get('data', {})
        accounts.extend(data.get('list', []))

        # Phân trang
        page_info = data.get('page_info', {})
        if not page_info.get('has_more'):
            break
        cursor = page_info.get('cursor')
        time.sleep(0.3)

    print(f"  Tìm thấy {len(accounts)} kênh TikTok trong BC.")
    return accounts


# ---------------------------------------------------------------------------
# Bước 2 – Lấy thông tin account (followers) bằng organic token
# ---------------------------------------------------------------------------
def get_account_info(business_id: str, token: str) -> dict:
    """Lấy followers, display_name từ /business/get/."""
    res = tt_get('/business/get/', {'business_id': business_id}, token)
    if res.get('code') != 0:
        print(f"    [!] Lỗi /business/get/ cho {business_id}: {res.get('message')}")
        return {}
    d = res.get('data', {})
    return {
        'display_name':  d.get('display_name', ''),
        'followers':     d.get('follower_count', 0),
        'profile_views': d.get('profile_views', 0),
    }


# ---------------------------------------------------------------------------
# Bước 3 – Lấy danh sách video tháng target
# ---------------------------------------------------------------------------
def get_videos(business_id: str, token: str, year: int, month: int) -> list:
    """Lấy video trong tháng year/month từ /business/video/list/."""
    # Tính khoảng thời gian (Unix timestamp, UTC)
    start_dt = datetime(year, month, 1, tzinfo=TZ_VN)
    # Ngày cuối tháng
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, tzinfo=TZ_VN)
    else:
        end_dt = datetime(year, month + 1, 1, tzinfo=TZ_VN)

    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    videos = []
    cursor = None

    while True:
        payload = {
            'business_id': business_id,
            'fields': json.dumps([
                'item_id', 'caption', 'video_views', 'likes',
                'comment_count', 'share_count', 'create_time',
                'video_cover_url', 'share_url', 'reach',
            ]),
            'filters': json.dumps({
                'create_time': {'min': start_ts, 'max': end_ts},
            }),
            'page_size': 50,
        }
        if cursor:
            payload['cursor'] = cursor

        res = tt_post('/business/video/list/', payload, token)

        if res.get('code') != 0:
            print(f"    [!] Lỗi /business/video/list/: {res.get('message')} (code {res.get('code')})")
            break

        data = res.get('data', {})
        batch = data.get('videos', []) or data.get('list', [])
        videos.extend(batch)

        if not data.get('has_more'):
            break
        cursor = data.get('cursor')
        time.sleep(0.2)

    return videos


# ---------------------------------------------------------------------------
# Bước 4 – Lấy insights chi tiết cho từng video
# ---------------------------------------------------------------------------
def get_video_insights(business_id: str, video_ids: list, token: str) -> dict:
    """Gọi /business/video/insights/ theo batch tối đa 20 video."""
    insights_map = {}
    batch_size = 20

    for i in range(0, len(video_ids), batch_size):
        batch = video_ids[i:i + batch_size]
        payload = {
            'business_id': business_id,
            'item_ids':    batch,
            'fields':      json.dumps(['average_watch_time', 'full_video_watched_rate', 'reach']),
        }
        res = tt_post('/business/video/insights/', payload, token)
        if res.get('code') != 0:
            print(f"    [!] Lỗi insights batch: {res.get('message')}")
            continue

        for item in res.get('data', {}).get('list', []):
            insights_map[str(item['item_id'])] = {
                'watch_time':       item.get('average_watch_time', 0),
                'completion_rate':  item.get('full_video_watched_rate', 0),
                'reach':            item.get('reach', 0),
            }
        time.sleep(0.2)

    return insights_map


# ---------------------------------------------------------------------------
# Bước 5 – Extract hashtags từ caption
# ---------------------------------------------------------------------------
def extract_caption(text: str):
    if not text:
        return '', []
    hashtags = re.findall(r'#(\w+)', text)
    clean    = re.sub(r'#\w+', '', text).strip()
    return clean, hashtags


# ---------------------------------------------------------------------------
# Bước 6 – Upsert vào social_video_report
# ---------------------------------------------------------------------------
def upsert_video(cur, row: dict):
    cur.execute("""
        INSERT INTO social_video_report
          (id, platform, post_id, channel_name, username, owner, team,
           title, hashtags, views, likes, comments, shares, followers,
           video_url, year, month, published_at, source, synced_at)
        VALUES
          (gen_random_uuid(), 'tiktok', %(post_id)s, %(channel_name)s,
           %(username)s, %(owner)s, %(team)s,
           %(title)s, %(hashtags)s, %(views)s, %(likes)s, %(comments)s,
           %(shares)s, %(followers)s,
           %(video_url)s, %(year)s, %(month)s, %(published_at)s,
           'api', NOW())
        ON CONFLICT (platform, post_id) DO UPDATE SET
          views       = EXCLUDED.views,
          likes       = EXCLUDED.likes,
          comments    = EXCLUDED.comments,
          shares      = EXCLUDED.shares,
          followers   = EXCLUDED.followers,
          title       = EXCLUDED.title,
          synced_at   = NOW()
    """, row)


# ---------------------------------------------------------------------------
# Main – chạy toàn bộ pipeline
# ---------------------------------------------------------------------------
def run(year: int, month: int, export_html: bool = False):
    print(f"\n{'='*60}")
    print(f"  TikTok Organic Sync — {month}/{year}")
    print(f"{'='*60}\n")

    # Kiểm tra organic token
    organic_token = os.getenv('TIKTOK_ORGANIC_TOKEN')
    if not organic_token:
        print("[!] Chưa có TIKTOK_ORGANIC_TOKEN trong .env")
        print("    Chạy: python get_organic_token.py để lấy token trước.")
        sys.exit(1)

    # Kết nối DB
    conn = db_conn()
    meta = get_channel_metadata(conn)
    print(f"  Metadata {len(meta)} kênh TikTok từ huyk_channels.\n")

    # Lấy danh sách kênh từ BC
    accounts = get_tt_accounts_from_bc()
    if not accounts:
        print("[!] Không có kênh nào. Dừng.")
        return

    all_results = []
    total_saved = 0
    cur = conn.cursor()

    for acc in accounts:
        business_id  = str(acc.get('asset_id', ''))
        display_name = acc.get('asset_name', business_id)
        name_key     = display_name.strip().lower()

        print(f"► Kênh: {display_name} ({business_id})")

        # Lấy thông tin account (followers)
        info = get_account_info(business_id, organic_token)
        followers = info.get('followers', 0)
        if info.get('display_name'):
            display_name = info['display_name']

        # Metadata từ huyk_channels
        ch_meta  = meta.get(name_key, {})
        owner    = ch_meta.get('owner', '')
        team     = ch_meta.get('team', '')

        # Lấy video tháng này
        videos = get_videos(business_id, organic_token, year, month)
        print(f"  → {len(videos)} video trong {month}/{year}")

        if not videos:
            continue

        # Lấy insights
        video_ids = [str(v.get('item_id', '')) for v in videos]
        insights  = get_video_insights(business_id, video_ids, organic_token)

        saved_count = 0
        for v in videos:
            vid_id    = str(v.get('item_id', ''))
            caption   = v.get('caption', '')
            title, tags = extract_caption(caption)
            views     = v.get('video_views', 0) or v.get('reach', 0)
            likes     = v.get('likes', 0)
            comments  = v.get('comment_count', 0)
            shares    = v.get('share_count', 0)
            create_ts = v.get('create_time', 0)
            published = (datetime.fromtimestamp(create_ts, tz=TZ_VN)
                         if create_ts else datetime.now(TZ_VN))
            video_url = v.get('share_url', f'https://www.tiktok.com/@{business_id}/video/{vid_id}')

            # Merge insights
            ins = insights.get(vid_id, {})
            if ins.get('reach') and not views:
                views = ins['reach']

            row = {
                'post_id':      vid_id,
                'channel_name': display_name,
                'username':     business_id,
                'owner':        owner,
                'team':         team,
                'title':        title[:500] if title else caption[:500],
                'hashtags':     tags,
                'views':        int(views or 0),
                'likes':        int(likes or 0),
                'comments':     int(comments or 0),
                'shares':       int(shares or 0),
                'followers':    int(followers or 0),
                'video_url':    video_url,
                'year':         year,
                'month':        month,
                'published_at': published,
            }

            try:
                upsert_video(cur, row)
                saved_count += 1
                all_results.append({**row, 'watch_time': ins.get('watch_time', 0)})
            except Exception as e:
                print(f"    [!] Lỗi upsert video {vid_id}: {e}")
                conn.rollback()

        conn.commit()
        total_saved += saved_count
        print(f"  ✓ Đã lưu {saved_count} video vào social_video_report")

    cur.close()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  Hoàn thành: {total_saved} video từ {len(accounts)} kênh")
    print(f"{'='*60}\n")

    # Xuất JSON
    json_path = os.path.join(os.path.dirname(__file__),
                             f'tiktok_organic_{year}_{month:02d}.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"✓ Đã xuất JSON: {json_path}")

    # Xuất HTML dashboard
    if export_html:
        export_dashboard(all_results, year, month)


# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------
def export_dashboard(data: list, year: int, month: int):
    if not data:
        return

    rows_html = ''
    for i, v in enumerate(sorted(data, key=lambda x: x['views'], reverse=True), 1):
        watch = f"{v.get('watch_time', 0):.1f}s" if v.get('watch_time') else '—'
        rows_html += f"""
        <tr>
          <td>{i}</td>
          <td><a href="{v['video_url']}" target="_blank">{(v['title'] or v.get('channel_name',''))[:60]}</a></td>
          <td>{v['channel_name']}</td>
          <td>{v['views']:,}</td>
          <td>{v['likes']:,}</td>
          <td>{v['comments']:,}</td>
          <td>{v['shares']:,}</td>
          <td>{watch}</td>
          <td>{v['team'] or '—'}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>TikTok Organic Report {month}/{year}</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 24px; background: #f9fafb; color: #111; }}
  h1 {{ color: #fe2c55; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  th {{ background: #fe2c55; color: #fff; padding: 10px 12px; text-align: left; font-size: 13px; }}
  td {{ padding: 9px 12px; font-size: 13px; border-bottom: 1px solid #f0f0f0; }}
  tr:hover td {{ background: #fff5f6; }}
  a {{ color: #fe2c55; text-decoration: none; }}
</style>
</head>
<body>
<h1>TikTok Organic — {month}/{year}</h1>
<p>Tổng: {len(data)} video | Xuất lúc {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
<table>
  <thead>
    <tr>
      <th>#</th><th>Video</th><th>Kênh</th>
      <th>Views</th><th>Likes</th><th>Comments</th><th>Shares</th>
      <th>Watch time</th><th>Team</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>"""

    html_path = os.path.join(os.path.dirname(__file__),
                             f'tiktok_organic_{year}_{month:02d}.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✓ Đã xuất HTML dashboard: {html_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TikTok Organic Sync')
    parser.add_argument('--year',  type=int, default=CURRENT_YEAR)
    parser.add_argument('--month', type=int, default=CURRENT_MONTH)
    parser.add_argument('--html',  action='store_true', help='Xuất HTML dashboard')
    args = parser.parse_args()

    run(year=args.year, month=args.month, export_html=args.html)
