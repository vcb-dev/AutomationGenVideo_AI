import os, requests, psycopg2, psycopg2.extras, json, re, calendar
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load env
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Config
META_TOKEN = os.getenv('META_ACCESS_TOKEN')
TT_TOKEN = os.getenv('TIKTOK_ACCESS_TOKEN')
YT_KEY = os.getenv('YOUTUBE_API_KEY')
DATABASE_URL = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_current_month_range():
    now = datetime.now()
    first_day = now.replace(day=1).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    return first_day, today

def parse_metadata(name):
    """Bóc tách Team và Owner từ tên chiến dịch"""
    team = None; owner = None
    m_team = re.search(r'(K\d+)', name, re.I)
    if m_team: team = f"Team {m_team.group(1).upper()}"
    elif "Đồ da" in name: team = "Team Đồ da"
    owners = ["Việt", "Nhạn", "HuyK", "Hiền", "Nam"]
    for o in owners:
        if o.lower() in name.lower():
            owner = o; break
    return team, owner

# ── Part 1: Meta Ads (Ghi đè dữ liệu tháng hiện tại) ───────────────────────
def sync_meta_ads():
    print("[*] Đang đồng bộ Meta Ads (Tháng hiện tại)...")
    try:
        r_accounts = requests.get(f"https://graph.facebook.com/v19.0/me/adaccounts", 
                                 params={"fields": "name,account_id", "access_token": META_TOKEN})
        accounts = r_accounts.json().get('data', [])
        
        conn = db_conn(); cur = conn.cursor()
        for acc in accounts:
            acc_id = acc['id']
            # Lấy insights "this_month" để ghi đè số liệu mới nhất
            r_ins = requests.get(f"https://graph.facebook.com/v19.0/{acc_id}/insights", 
                                params={
                                    "level": "campaign",
                                    "fields": "campaign_id,campaign_name,spend,impressions,clicks,reach,actions",
                                    "date_preset": "this_month",
                                    "access_token": META_TOKEN
                                })
            insights = r_ins.json().get('data', [])
            for ins in insights:
                actions = {a['action_type']: int(a['value']) for a in ins.get('actions', [])}
                mess = actions.get('messenger_conversation_started_7d', 0) or actions.get('onsite_conversion.messaging_conversation_started_7d', 0)
                likes = actions.get('post_reaction', 0)
                comments = actions.get('comment', 0)
                shares = actions.get('post_share', 0)
                
                team, owner = parse_metadata(ins['campaign_name'])
                dt_start = datetime.strptime(ins['date_start'], "%Y-%m-%d")
                
                cur.execute("""
                    INSERT INTO ads_campaign_stats 
                    (id, account_id, account_name, campaign_id, campaign_name, team, owner, spend, impressions, reach, clicks, mess_count, like_count, comment_count, share_count, date_start, date_stop, year, month, synced_at)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (campaign_id, date_start, date_stop) DO UPDATE SET 
                        spend=EXCLUDED.spend, 
                        impressions=EXCLUDED.impressions,
                        reach=EXCLUDED.reach,
                        clicks=EXCLUDED.clicks,
                        mess_count=EXCLUDED.mess_count, 
                        like_count=EXCLUDED.like_count,
                        comment_count=EXCLUDED.comment_count,
                        share_count=EXCLUDED.share_count,
                        synced_at=NOW()
                """, (
                    acc_id, acc['name'], ins['campaign_id'], ins['campaign_name'], team, owner,
                    float(ins['spend']), int(ins['impressions']), int(ins['reach']), int(ins['clicks']),
                    mess, likes, comments, shares, ins['date_start'], ins['date_stop'], dt_start.year, dt_start.month
                ))
        conn.commit(); conn.close()
        print(f"  -> Hoàn tất Meta Ads.")
    except Exception as e:
        print(f"  [!] Lỗi Meta Ads: {e}")

# ── Part 2: TikTok Ads (Ghi đè dữ liệu tháng hiện tại) ─────────────────────
def sync_tiktok_ads():
    print("[*] Đang đồng bộ TikTok Ads (Tháng hiện tại)...")
    try:
        first_day, today = get_current_month_range()
        r_acc = requests.get("https://business-api.tiktok.com/open_api/v1.3/advertiser/info/", 
                             headers={"Access-Token": TT_TOKEN})
        advertisers = r_acc.json().get('data', {}).get('list', [])
        
        conn = db_conn(); cur = conn.cursor()
        for adv in advertisers:
            adv_id = adv['advertiser_id']
            r_rep = requests.get("https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/", 
                                params={
                                    "advertiser_id": adv_id,
                                    "report_type": "BASIC",
                                    "data_level": "AUCTION_CAMPAIGN",
                                    "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
                                    "metrics": json.dumps(["spend", "impressions", "clicks", "reach", "conversion", "likes", "comments", "shares"]),
                                    "start_date": first_day,
                                    "end_date": today
                                }, headers={"Access-Token": TT_TOKEN})
            
            rows = r_rep.json().get('data', {}).get('list', [])
            for row in rows:
                metrics = row['metrics']
                camp_name = f"TikTok Campaign {row['dimensions']['campaign_id']}" 
                team, owner = parse_metadata(camp_name)
                dt = datetime.strptime(row['dimensions']['stat_time_day'], "%Y-%m-%d")
                
                cur.execute("""
                    INSERT INTO ads_campaign_stats 
                    (id, account_id, account_name, campaign_id, campaign_name, team, owner, spend, impressions, reach, clicks, mess_count, like_count, comment_count, share_count, date_start, date_stop, year, month, synced_at)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (campaign_id, date_start, date_stop) DO UPDATE SET 
                        spend=EXCLUDED.spend, 
                        impressions=EXCLUDED.impressions,
                        reach=EXCLUDED.reach,
                        clicks=EXCLUDED.clicks,
                        mess_count=EXCLUDED.mess_count,
                        like_count=EXCLUDED.like_count,
                        synced_at=NOW()
                """, (
                    adv_id, adv['name'], row['dimensions']['campaign_id'], camp_name, team, owner,
                    float(metrics['spend']), int(metrics['impressions']), int(metrics['reach']), int(metrics['clicks']),
                    int(metrics['conversion']), int(metrics['likes']), int(metrics['comments']), int(metrics['shares']),
                    row['dimensions']['stat_time_day'], row['dimensions']['stat_time_day'], dt.year, dt.month
                ))
        conn.commit(); conn.close()
        print(f"  -> Hoàn tất TikTok Ads.")
    except Exception as e:
        print(f"  [!] Lỗi TikTok Ads: {e}")

# ── Part 3: Social Insights (Ghi đè dữ liệu tháng hiện tại) ────────────────
def sync_social_insights():
    print("[*] Đang đồng bộ Social Insights (Traffic)...")
    # Gọi script Meta đã tối ưu ở V2
    from cron_meta_insights import main as meta_main
    meta_main()

def main():
    print(f"=== KHỞI ĐỘNG ĐỒNG BỘ TOÀN DIỆN (REALTIME) [{datetime.now()}] ===")
    sync_meta_ads()
    sync_tiktok_ads()
    sync_social_insights()
    print(f"=== HOÀN TẤT [{datetime.now()}] ===")

if __name__ == "__main__":
    main()
