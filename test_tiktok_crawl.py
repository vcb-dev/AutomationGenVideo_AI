"""
Test crawl dữ liệu TikTok các kênh trong bảng huyk_channels.
Thời gian: từ ngày đầu tháng đến ngày hôm nay.

Chạy: python test_tiktok_crawl.py
      python test_tiktok_crawl.py --team "Team K1"
      python test_tiktok_crawl.py --month 4  (tháng khác)
"""
import os
import sys
import json
import time
import argparse
import requests
import psycopg2
import psycopg2.extras
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── Config ────────────────────────────────────────────────────────────────────
APIFY_TOKEN   = os.getenv('APIFY_API_TOKEN', '')
DATABASE_URL  = (os.getenv('DIRECT_URL') or
                 os.getenv('DATABASE_URL', '').replace('?pgbouncer=true', ''))
VIDEOS_PER_CH = 30   # Số video fetch mỗi kênh
DELAY_SEC     = 1.5  # Nghỉ giữa các kênh để tránh rate limit

# ── Màu terminal ─────────────────────────────────────────────────────────────
G  = "\033[92m"  # xanh lá
Y  = "\033[93m"  # vàng
R  = "\033[91m"  # đỏ
B  = "\033[94m"  # xanh dương
W  = "\033[97m"  # trắng
DIM = "\033[2m"
NC = "\033[0m"


def fmt(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)


def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_tiktok_channels(team: str = None) -> list:
    """Lấy danh sách kênh TikTok từ huyk_channels."""
    conn = db_conn()
    cur = conn.cursor()
    params = ['%tiktok%', 'Đang hoạt động']
    extra = ""
    if team:
        extra = " AND LOWER(team_traffic) LIKE LOWER(%s)"
        params.append(f"%{team}%")
    cur.execute(f"""
        SELECT name, channel_id, link_channel, team_traffic AS team, owner, email
        FROM huyk_channels
        WHERE LOWER(platform) LIKE %s AND status = %s {extra}
        ORDER BY team_traffic, name
    """, params)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def extract_username(channel_id: str, link: str) -> str:
    """Lấy TikTok username từ channel_id hoặc link."""
    uid = (channel_id or '').strip().strip('\n').lstrip('@')
    if uid:
        return uid
    if 'tiktok.com/@' in (link or ''):
        return link.split('tiktok.com/@')[-1].split('?')[0].strip().strip('/')
    return ''


def apify_fetch(username: str, max_videos: int = 30) -> list:
    """Gọi Apify scrape TikTok profile."""
    r = requests.post(
        "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN, "timeout": 120},
        json={"profiles": [username], "resultsPerPage": max_videos},
        timeout=150,
    )
    r.raise_for_status()
    return r.json() or []


def parse_video(item: dict) -> dict:
    """Parse 1 video từ Apify response."""
    iso = item.get('createTimeISO') or ''
    ts  = item.get('createTime', 0)
    if iso:
        pub = iso[:10]
    elif ts:
        pub = datetime.utcfromtimestamp(int(ts)).strftime('%Y-%m-%d')
    else:
        pub = ''
    return {
        'video_id':  item.get('id', ''),
        'title':     (item.get('text') or '')[:100],
        'url':       item.get('webVideoUrl') or item.get('videoUrl', ''),
        'published': pub,
        'views':     int(item.get('playCount', 0)),
        'likes':     int(item.get('diggCount', 0)),
        'comments':  int(item.get('commentCount', 0)),
        'shares':    int(item.get('shareCount', 0)),
    }


def run(year: int, month: int, team: str = None):
    today     = date.today()
    date_from = f"{year}-{month:02d}-01"
    date_to   = today.strftime('%Y-%m-%d')

    print(f"\n{B}{'═'*60}{NC}")
    print(f"{W}  TikTok Crawl — {month}/{year}  ({date_from} → {date_to}){NC}")
    if team:
        print(f"  Filter team: {Y}{team}{NC}")
    print(f"{B}{'═'*60}{NC}\n")

    if not APIFY_TOKEN or APIFY_TOKEN == 'your_apify_api_token_here':
        print(f"{R}❌ APIFY_API_TOKEN chưa được cấu hình trong .env{NC}")
        sys.exit(1)

    channels = get_tiktok_channels(team)
    print(f"{G}✓{NC} Tìm thấy {W}{len(channels)}{NC} kênh TikTok đang hoạt động\n")

    if not channels:
        print(f"{Y}Không có kênh nào.{NC}")
        return

    # ── Fetch từng kênh ───────────────────────────────────────────────────────
    all_videos   = []
    channel_stats = []
    errors        = []

    for idx, ch in enumerate(channels, 1):
        username = extract_username(ch.get('channel_id',''), ch.get('link_channel',''))
        ch_name  = ch.get('name', username)
        owner    = ch.get('owner', '')
        team_ch  = ch.get('team', '')

        if not username:
            print(f"  {DIM}[{idx:>2}/{len(channels)}] SKIP {ch_name} — không có username{NC}")
            continue

        print(f"  {DIM}[{idx:>2}/{len(channels)}]{NC} {W}{ch_name}{NC} {DIM}(@{username}){NC} ", end='', flush=True)

        try:
            items = apify_fetch(username, VIDEOS_PER_CH)
            videos_in_range = []
            for item in items:
                v = parse_video(item)
                v['channel'] = ch_name
                v['owner']   = owner
                v['team']    = team_ch
                if date_from <= v['published'] <= date_to:
                    videos_in_range.append(v)

            total_views    = sum(v['views']    for v in videos_in_range)
            total_likes    = sum(v['likes']    for v in videos_in_range)
            total_comments = sum(v['comments'] for v in videos_in_range)

            print(f"{G}✓{NC} {len(videos_in_range)} videos | "
                  f"views={G}{fmt(total_views)}{NC} | "
                  f"likes={fmt(total_likes)} | "
                  f"cmt={fmt(total_comments)}")

            channel_stats.append({
                'channel':        ch_name,
                'username':       username,
                'owner':          owner,
                'team':           team_ch,
                'video_count':    len(videos_in_range),
                'total_views':    total_views,
                'total_likes':    total_likes,
                'total_comments': total_comments,
                'total_shares':   sum(v['shares'] for v in videos_in_range),
            })
            all_videos.extend(videos_in_range)

        except Exception as e:
            print(f"{R}✗ {e}{NC}")
            errors.append({'channel': ch_name, 'username': username, 'error': str(e)})

        time.sleep(DELAY_SEC)

    # ── Tổng kết ──────────────────────────────────────────────────────────────
    grand_views    = sum(c['total_views']    for c in channel_stats)
    grand_likes    = sum(c['total_likes']    for c in channel_stats)
    grand_comments = sum(c['total_comments'] for c in channel_stats)
    grand_videos   = sum(c['video_count']    for c in channel_stats)

    print(f"\n{B}{'─'*60}{NC}")
    print(f"{W}  TỔNG KẾT — Tháng {month}/{year}{NC}")
    print(f"{B}{'─'*60}{NC}")
    print(f"  Kênh thành công : {G}{len(channel_stats)}{NC} / {len(channels)}")
    print(f"  Kênh lỗi        : {R}{len(errors)}{NC}")
    print(f"  Tổng video      : {W}{grand_videos}{NC}")
    print(f"  Tổng views      : {G}{fmt(grand_views)}{NC}")
    print(f"  Tổng likes      : {fmt(grand_likes)}")
    print(f"  Tổng comments   : {fmt(grand_comments)}")

    # Top 10 views
    top_views = sorted(all_videos, key=lambda x: x['views'], reverse=True)[:10]
    print(f"\n{W}  TOP 10 VIDEO VIEWS CAO NHẤT:{NC}")
    for i, v in enumerate(top_views, 1):
        print(f"  {i:>2}. {G}{fmt(v['views'])}{NC} views | {v['channel'][:25]:<25} | {v['title'][:45]}")

    # Top 10 likes
    top_likes = sorted(all_videos, key=lambda x: x['likes'], reverse=True)[:10]
    print(f"\n{W}  TOP 10 VIDEO LIKES CAO NHẤT:{NC}")
    for i, v in enumerate(top_likes, 1):
        print(f"  {i:>2}. {G}{fmt(v['likes'])}{NC} likes  | {v['channel'][:25]:<25} | {v['title'][:45]}")

    # Top 10 comments
    top_comments = sorted(all_videos, key=lambda x: x['comments'], reverse=True)[:10]
    print(f"\n{W}  TOP 10 VIDEO COMMENTS CAO NHẤT:{NC}")
    for i, v in enumerate(top_comments, 1):
        print(f"  {i:>2}. {G}{fmt(v['comments'])}{NC} cmt   | {v['channel'][:25]:<25} | {v['title'][:45]}")

    # Breakdown theo team
    team_stats: dict = {}
    for c in channel_stats:
        t = c['team'] or 'Chưa phân team'
        if t not in team_stats:
            team_stats[t] = {'channels': 0, 'videos': 0, 'views': 0, 'likes': 0, 'comments': 0}
        team_stats[t]['channels'] += 1
        team_stats[t]['videos']   += c['video_count']
        team_stats[t]['views']    += c['total_views']
        team_stats[t]['likes']    += c['total_likes']
        team_stats[t]['comments'] += c['total_comments']

    print(f"\n{W}  BREAKDOWN THEO TEAM:{NC}")
    for t, s in sorted(team_stats.items(), key=lambda x: x[1]['views'], reverse=True):
        print(f"  {Y}{t:<20}{NC} | {s['channels']} kênh | {s['videos']} videos | views={G}{fmt(s['views'])}{NC}")

    # Lưu kết quả ra JSON
    output = {
        'period':     f"{year}-{month:02d}-01 → {date_to}",
        'fetched_at': datetime.now().isoformat(),
        'summary': {
            'channels_ok':      len(channel_stats),
            'channels_error':   len(errors),
            'total_videos':     grand_videos,
            'total_views':      grand_views,
            'total_likes':      grand_likes,
            'total_comments':   grand_comments,
        },
        'top_10_views':    top_views,
        'top_10_likes':    top_likes,
        'top_10_comments': top_comments,
        'by_channel':      sorted(channel_stats, key=lambda x: x['total_views'], reverse=True),
        'by_team':         [{'team': t, **s} for t, s in sorted(team_stats.items(), key=lambda x: x[1]['views'], reverse=True)],
        'errors':          errors,
    }

    out_file = f"tiktok_report_{year}_{month:02d}.json"
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{G}✓{NC} Kết quả đã lưu → {W}{out_file}{NC}")
    print(f"{B}{'═'*60}{NC}\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawl TikTok data cho tháng hiện tại')
    parser.add_argument('--month', type=int, default=date.today().month,  help='Tháng (mặc định: tháng hiện tại)')
    parser.add_argument('--year',  type=int, default=date.today().year,   help='Năm (mặc định: năm hiện tại)')
    parser.add_argument('--team',  type=str, default=None,                help='Lọc theo team, vd: "Team K1"')
    args = parser.parse_args()

    run(year=args.year, month=args.month, team=args.team)
