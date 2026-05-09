"""
Test Apify token + crawl thử 1 kênh TikTok.
Chạy: python test_apify_token.py
"""
import requests
import json
from datetime import date, datetime

TOKEN         = "apify_api_D5lEOg867BKZlfxHjhkPPBGVubjBGp1Vq2vH"
TEST_USERNAME = "huyk.xuongkimhoan2"  # Kênh test
TEST_MONTH    = 5
TEST_YEAR     = 2026

G   = "\033[92m"
Y   = "\033[93m"
R   = "\033[91m"
B   = "\033[94m"
W   = "\033[97m"
DIM = "\033[2m"
NC  = "\033[0m"

def log(msg): print(f"  {msg}")
def ok(msg):  print(f"  {G}✓{NC} {msg}")
def err(msg): print(f"  {R}✗{NC} {msg}")
def info(msg):print(f"  {Y}→{NC} {msg}")

print(f"\n{B}{'═'*55}{NC}")
print(f"{W}  Apify Token Test{NC}")
print(f"{B}{'═'*55}{NC}\n")

# ── Bước 1: Kiểm tra account ─────────────────────────────
print(f"{W}[1/3] Kiểm tra account...{NC}")
try:
    r = requests.get("https://api.apify.com/v2/users/me",
                     params={"token": TOKEN}, timeout=10)
    r.raise_for_status()
    d = r.json().get("data", {})
    plan     = d.get("plan", {})
    features = d.get("effectivePlatformFeatures", {})
    actors   = features.get("ACTORS", {})

    ok(f"Account : {W}{d.get('username')}{NC} ({d.get('email')})")
    ok(f"Plan    : {W}{plan.get('id')}{NC} — ${plan.get('monthlyUsageCreditsUsd', 0)}/tháng credits")

    if actors.get("isEnabled"):
        ok(f"ACTORS  : {G}Hoạt động bình thường ✅{NC}")
    else:
        err(f"ACTORS  : {R}{actors.get('disabledReason', 'Disabled')}{NC}")
        print(f"\n{R}Token này không dùng được. Kiểm tra billing tại console.apify.com{NC}\n")
        exit(1)

except Exception as e:
    err(f"Không kết nối được Apify: {e}")
    exit(1)

# ── Bước 2: Test fetch TikTok với smart filter ────────────
from datetime import date
import calendar

date_from = f"{TEST_YEAR}-{TEST_MONTH:02d}-01"
date_to   = date.today().strftime("%Y-%m-%d")

print(f"\n{W}[2/3] Test fetch TikTok @{TEST_USERNAME}{NC}")
info(f"Tháng cần: {TEST_MONTH}/{TEST_YEAR} ({date_from} → {date_to})")
info(f"Gọi actor: clockworks~free-tiktok-scraper (30 videos)")
info(f"Có thể mất 30-60 giây...")

try:
    start = datetime.now()
    r = requests.post(
        "https://api.apify.com/v2/acts/clockworks~free-tiktok-scraper/run-sync-get-dataset-items",
        params={"token": TOKEN, "timeout": 120},
        json={"profiles": [TEST_USERNAME], "resultsPerPage": 30},
        timeout=150,
    )
    elapsed = (datetime.now() - start).seconds

    if r.status_code == 403:
        err(f"403 Forbidden — account bị khóa (kiểm tra billing)")
        exit(1)

    r.raise_for_status()
    all_items = r.json() or []
    ok(f"Apify trả về {W}{len(all_items)}{NC} videos thô (mất {elapsed}s)")

    # Smart filter
    in_range, skipped_future, stopped_at = [], [], None
    for item in all_items:
        iso = (item.get("createTimeISO") or "")[:10]
        ts  = item.get("createTime", 0)
        if not iso and ts:
            iso = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        if iso < date_from:
            stopped_at = iso
            break
        if iso > date_to:
            skipped_future.append(iso)
            continue
        in_range.append({"date": iso, "views": item.get("playCount",0),
                         "likes": item.get("diggCount",0),
                         "comments": item.get("commentCount",0),
                         "title": (item.get("text") or "")[:50]})

    ok(f"Sau filter: {G}{len(in_range)}{NC} videos trong tháng {TEST_MONTH}/{TEST_YEAR}")
    if stopped_at:
        ok(f"Smart-stop: dừng tại video ngày {Y}{stopped_at}{NC} (cũ hơn tháng cần → không fetch tiếp)")
    if skipped_future:
        info(f"Bỏ qua {len(skipped_future)} videos chưa đến kỳ")

    if in_range:
        total_views = sum(v["views"] for v in in_range)
        total_likes = sum(v["likes"] for v in in_range)
        total_cmt   = sum(v["comments"] for v in in_range)
        print(f"\n{W}  Tổng tháng {TEST_MONTH}/{TEST_YEAR}:{NC}")
        print(f"    Views    : {G}{total_views:,}{NC}")
        print(f"    Likes    : {total_likes:,}")
        print(f"    Comments : {total_cmt:,}")
        print(f"\n{W}  Top 5 videos:{NC}")
        for i, v in enumerate(sorted(in_range, key=lambda x: x["views"], reverse=True)[:5], 1):
            print(f"  {i}. {v['date']} | {G}{v['views']:>8,}{NC} views | {v['likes']:>6,} likes | {v['title']}")
    else:
        info(f"Không có video nào trong tháng {TEST_MONTH}/{TEST_YEAR}")

except Exception as e:
    err(f"Lỗi fetch: {e}")
    exit(1)

# ── Bước 3: Tính chi phí ─────────────────────────────────
print(f"\n{W}[3/3] Ước tính chi phí...{NC}")
ok(f"50 kênh × 30 videos ≈ {Y}~$0.075/lần fetch{NC} (~1.700đ)")
ok(f"Free plan $5/tháng → đủ chạy {Y}~66 lần{NC} (2 lần/ngày)")

print(f"\n{G}{'═'*55}{NC}")
print(f"{G}  Token hoạt động tốt! ✅{NC}")
print(f"{G}{'═'*55}{NC}\n")

# Ghi token vào .env nếu test pass
print(f"{Y}Bạn có muốn cập nhật token này vào .env không?{NC}")
print(f"  → Thêm vào .env: {W}APIFY_API_TOKEN={TOKEN}{NC}\n")
