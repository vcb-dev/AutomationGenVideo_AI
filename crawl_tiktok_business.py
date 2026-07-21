import os, requests, psycopg2, psycopg2.extras, json
from datetime import datetime
from dotenv import load_dotenv

# Load env from current dir
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# Config
TOKEN = os.getenv('TIKTOK_ACCESS_TOKEN')
BC_ID = "7274810417535270913"
DATABASE_URL = os.getenv('DIRECT_URL') or os.getenv('DATABASE_URL','').replace('?pgbouncer=true','')

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_tt_accounts():
    """Lấy danh sách 34 kênh từ Business Center"""
    url = f"https://business-api.tiktok.com/open_api/v1.3/bc/asset/get/?bc_id={BC_ID}&asset_type=TT_ACCOUNT"
    r = requests.get(url, headers={"Access-Token": TOKEN})
    return r.json().get('data', {}).get('list', [])

def crawl_and_save():
    if not TOKEN:
        print("Lỗi: Thiếu TIKTOK_ACCESS_TOKEN trong .env")
        return

    accounts = get_tt_accounts()
    if not accounts:
        print("Không tìm thấy kênh nào hoặc lỗi API.")
        return
        
    print(f"Tìm thấy {len(accounts)} kênh TikTok.")
    
    conn = db_conn()
    cur = conn.cursor()
    
    total_saved = 0
    for acc in accounts:
        asset_id = acc['asset_id']
        name = acc['asset_name']
        print(f"Đang quét kênh: {name} ({asset_id})...")
        
        # Thử lấy video (Sử dụng endpoint Business Video List)
        video_url = f"https://business-api.tiktok.com/open_api/v1.3/business/video/list/"
        params = {
            "business_id": asset_id,
            "fields": json.dumps(["item_id", "caption", "video_views", "likes", "comment_count", "share_count", "create_time"])
        }
        try:
            vr = requests.get(video_url, params=params, headers={"Access-Token": TOKEN}, timeout=15)
            res_json = vr.json()
            
            if vr.status_code != 200 or res_json.get('code') != 0:
                print(f"  [!] Lỗi: {res_json.get('message')} (Code: {res_json.get('code')})")
                continue
                
            videos = res_json.get('data', {}).get('list', [])
            print(f"  -> Tìm thấy {len(videos)} video.")
            
            for v in videos:
                # Lọc theo tháng 5/2026
                dt = datetime.fromtimestamp(v['create_time'])
                # Chúng ta cứ lấy hết video mới nhất để test, nếu cần lọc tháng 5 thì bỏ comment bên dưới
                # if dt.year != 2026 or dt.month != 5: continue
                    
                cur.execute("""
                    INSERT INTO social_video_report 
                    (id, platform, post_id, channel_name, username, views, likes, comments, shares, year, month, published_at, source, synced_at)
                    VALUES (gen_random_uuid(), 'tiktok', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'api_business', NOW())
                    ON CONFLICT (platform, post_id) DO UPDATE SET 
                        views=EXCLUDED.views, 
                        likes=EXCLUDED.likes,
                        comments=EXCLUDED.comments,
                        shares=EXCLUDED.shares,
                        synced_at=NOW()
                """, (
                    v['item_id'], name, name, int(v.get('video_views', 0)), int(v.get('likes', 0)),
                    int(v.get('comment_count', 0)), int(v.get('share_count', 0)), dt.year, dt.month, dt,
                ))
                total_saved += 1
        except Exception as e:
            print(f"  [!] Lỗi khi xử lý kênh {name}: {e}")
            
    conn.commit()
    conn.close()
    print(f"\nHoàn tất! Đã lưu {total_saved} video vào SocialVideoReport.")

if __name__ == "__main__":
    crawl_and_save()
