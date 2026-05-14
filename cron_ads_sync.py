import os, requests, psycopg2, psycopg2.extras, re
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load env
load_dotenv()

META_TOKEN = os.getenv('META_ACCESS_TOKEN')
TIKTOK_TOKEN  = os.getenv('TIKTOK_ADS_ACCESS_TOKEN') or os.getenv('TIKTOK_ACCESS_TOKEN')
# Hardcode advertiser ID đã xác nhận hoạt động — override env nếu env sai
TIKTOK_ADV_ID = "7512289961128001543"
DATABASE_URL = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def parse_metadata(name):
    team = None; owner = None; content = None
    team_match = re.search(r"Team\s+([\w\s]+?)(?=\s+-|$)", name, re.IGNORECASE)
    if team_match: team = team_match.group(1).strip()
    content_match = re.search(r"\b(A[1-5])\b", name)
    if content_match: content = content_match.group(1)
    parts = [p.strip() for p in name.split("-")]
    if len(parts) > 0: owner = parts[0]
    tags = re.findall(r"\[(.*?)\]", name)
    if len(tags) >= 1: team = tags[0]
    if len(tags) >= 2: owner = tags[1]
    return team, owner, content

def sync_meta_ads():
    print("[*] Đang quét Meta Ads...")
    try:
        r_accounts = requests.get("https://graph.facebook.com/v19.0/me/adaccounts", params={"fields": "name,adaccount_id", "access_token": META_TOKEN}).json()
        conn = db_conn(); cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for acc in r_accounts.get('data', []):
            r_ins = requests.get(f"https://graph.facebook.com/v19.0/{acc['id']}/insights", params={"level": "campaign", "fields": "campaign_id,campaign_name,spend,impressions,reach,clicks,actions", "time_range": "{'since':'" + yesterday + "','until':'" + today + "'}", "access_token": META_TOKEN}).json()
            for item in r_ins.get('data', []):
                team, owner, content = parse_metadata(item['campaign_name'])
                mess = 0
                for action in item.get('actions', []):
                    if action['action_type'] == 'messaging_conversation_started_7d': mess = int(action['value'])
                cur.execute("""
                    INSERT INTO ads_campaign_stats (id, platform, account_id, account_name, campaign_id, campaign_name, team, owner, content_type, spend, impressions, reach, mess_count, clicks, date_start, date_stop, year, month, synced_at)
                    VALUES (gen_random_uuid(), 'meta', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (campaign_id, date_start, date_stop) DO UPDATE SET spend=EXCLUDED.spend, impressions=EXCLUDED.impressions, mess_count=EXCLUDED.mess_count, clicks=EXCLUDED.clicks, team=EXCLUDED.team, owner=EXCLUDED.owner, content_type=EXCLUDED.content_type, synced_at=NOW()
                """, (acc['id'], acc['name'], item['campaign_id'], item['campaign_name'], team, owner, content, float(item['spend']), int(item['impressions']), int(item['reach']), mess, int(item['clicks']), yesterday, today, datetime.now().year, datetime.now().month))
            print(f"    + Đã lưu Meta: {acc['name']}")
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi Meta Ads: {e}")

def sync_tiktok_ads():
    if not TIKTOK_TOKEN: return
    print("[*] Đang quét TikTok Ads...")
    try:
        adv_ids = [TIKTOK_ADV_ID] if TIKTOK_ADV_ID else []
        if not adv_ids:
            r_acc = requests.get("https://business-api.tiktok.com/open_api/v1.3/advertiser/info/", params={"access_token": TIKTOK_TOKEN}).json()
            if r_acc.get('code') == 0: adv_ids = [a['advertiser_id'] for a in r_acc.get('data', {}).get('list', [])]
        if not adv_ids: return
        headers = {"Access-Token": TIKTOK_TOKEN}
        conn = db_conn(); cur = conn.cursor()
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        for aid in adv_ids:
            params = {"advertiser_id": aid, "report_type": "BASIC", "data_level": "AUCTION_CAMPAIGN", "dimensions": '["campaign_id","stat_time_day"]', "metrics": '["campaign_name","spend","impressions","reach","clicks","conversions"]', "start_date": yesterday, "end_date": today}
            r_rep = requests.get("https://business-api.tiktok.com/open_api/v1.3/report/integrated/get/", params=params, headers=headers).json()
            for row in r_rep.get('data', {}).get('list', []):
                m = row['metrics']; d = row['dimensions']
                team, owner, content = parse_metadata(m['campaign_name'])
                cur.execute("""
                    INSERT INTO ads_campaign_stats (id, platform, account_id, account_name, campaign_id, campaign_name, team, owner, content_type, spend, impressions, reach, mess_count, clicks, date_start, date_stop, year, month, synced_at)
                    VALUES (gen_random_uuid(), 'tiktok', %s, 'TikTok Account', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (campaign_id, date_start, date_stop) DO UPDATE SET spend=EXCLUDED.spend, impressions=EXCLUDED.impressions, clicks=EXCLUDED.clicks, team=EXCLUDED.team, owner=EXCLUDED.owner, content_type=EXCLUDED.content_type, synced_at=NOW()
                """, (aid, d['campaign_id'], m['campaign_name'], team, owner, content, float(m['spend']), int(m['impressions']), int(m['reach']), int(m.get('conversions', 0)), int(m['clicks']), d['stat_time_day'], d['stat_time_day'], datetime.now().year, datetime.now().month))
            print(f"    + Đã lưu TikTok Advertiser: {aid}")
        conn.commit(); conn.close()
    except Exception as e: print(f" [!] Lỗi TikTok Ads: {e}")

def main():
    sync_meta_ads()
    sync_tiktok_ads()

if __name__ == "__main__":
    main()
