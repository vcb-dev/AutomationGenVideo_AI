"""
Chạy crawl social insights thủ công với log chi tiết.

Cách dùng:
  python run_crawl.py                        # tháng hiện tại, tất cả platform
  python run_crawl.py --month 5 --year 2026  # tháng cụ thể
  python run_crawl.py --platform tiktok      # chỉ TikTok
  python run_crawl.py --platform youtube     # chỉ YouTube
  python run_crawl.py --platform facebook    # chỉ Facebook
  python run_crawl.py --platform instagram   # chỉ Instagram
"""
import os, sys, time, argparse, logging
from datetime import date

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django
django.setup()

# ── Màu terminal ─────────────────────────────────────────────────────────────
G="\033[92m"; Y="\033[93m"; R="\033[91m"; B="\033[94m"; W="\033[97m"; D="\033[2m"; NC="\033[0m"

# ── Logging với màu ──────────────────────────────────────────────────────────
class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    D,
        logging.INFO:     W,
        logging.WARNING:  Y,
        logging.ERROR:    R,
        logging.CRITICAL: R,
    }
    def format(self, record):
        color = self.COLORS.get(record.levelno, NC)
        msg   = super().format(record)
        name  = record.name.split('.')[-1]
        if 'TikTok' in msg or 'tiktok' in msg.lower():   color = Y
        if 'YouTube' in msg or 'youtube' in msg.lower():  color = R
        if 'Facebook' in msg or 'facebook' in msg.lower(): color = B
        if 'Instagram' in msg or 'instagram' in msg.lower(): color = "\033[95m"
        if 'saved' in msg or 'Done' in msg: color = G
        if 'error' in msg.lower() or 'Error' in msg: color = R
        return f"{color}{msg}{NC}"

handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter('%(asctime)s [%(levelname)s] %(name)s — %(message)s', '%H:%M:%S'))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger('run_crawl')

# Tắt log thừa
for noisy in ['urllib3','requests','httpx']:
    logging.getLogger(noisy).setLevel(logging.WARNING)


def fmt(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)


def run(year, month, platform=None):
    from video_management.services import social_crawl_service as svc

    today = date.today()
    year  = year  or today.year
    month = month or today.month

    print(f"\n{B}{'═'*60}{NC}")
    print(f"{W}  Social Crawl — Tháng {month}/{year}{NC}")
    if platform: print(f"  Platform : {Y}{platform}{NC}")
    print(f"{B}{'═'*60}{NC}\n")

    start_all = time.time()
    results   = {}

    platforms = [platform] if platform else ['tiktok', 'youtube', 'facebook', 'instagram']

    for plat in platforms:
        print(f"\n{W}  ── {plat.upper()} ──────────────────────────────{NC}")
        start = time.time()

        try:
            if plat == 'tiktok':
                r = svc.crawl_tiktok(year, month)
            elif plat == 'youtube':
                r = svc.crawl_youtube(year, month)
            elif plat == 'facebook':
                r = svc.crawl_facebook(year, month)
            elif plat == 'instagram':
                r = svc.crawl_instagram(year, month)
            else:
                print(f"  {R}Platform không hợp lệ: {plat}{NC}")
                continue

            elapsed = time.time() - start
            if r.get('skipped'):
                print(f"  {Y}⚠ Bỏ qua: {r['skipped']}{NC}")
            else:
                print(f"  {G}✓{NC} Kênh: {W}{r.get('channels',0)}{NC} | "
                      f"Lưu DB: {G}{r.get('saved',0)}{NC} posts | "
                      f"Thời gian: {D}{elapsed:.1f}s{NC}")
            results[plat] = r

        except Exception as e:
            print(f"  {R}✗ Lỗi: {e}{NC}")
            results[plat] = {'error': str(e)}

    # ── Tổng kết ─────────────────────────────────────────────────────────────
    total_elapsed = time.time() - start_all
    total_saved   = sum(r.get('saved', 0) for r in results.values() if isinstance(r, dict))

    print(f"\n{B}{'─'*60}{NC}")
    print(f"{W}  TỔNG KẾT{NC}")
    print(f"{B}{'─'*60}{NC}")
    print(f"  {'Platform':<12} {'Kênh':>6} {'Posts lưu':>10}")
    print(f"  {'─'*12} {'─'*6} {'─'*10}")
    for plat, r in results.items():
        if r.get('skipped'):
            print(f"  {plat:<12} {Y}SKIP — {r['skipped'][:30]}{NC}")
        elif r.get('error'):
            print(f"  {plat:<12} {R}ERROR — {r['error'][:30]}{NC}")
        else:
            print(f"  {plat:<12} {r.get('channels',0):>6} {G}{r.get('saved',0):>10}{NC}")
    print(f"  {'─'*12} {'─'*6} {'─'*10}")
    print(f"  {'TỔNG':<12} {'':>6} {G}{total_saved:>10}{NC}")
    print(f"\n  Tổng thời gian: {D}{total_elapsed:.1f}s{NC}")
    print(f"  Bảng DB       : {G}social_video_report{NC}")
    print(f"{B}{'═'*60}{NC}\n")

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Crawl social media insights')
    parser.add_argument('--month',    type=int, default=None)
    parser.add_argument('--year',     type=int, default=None)
    parser.add_argument('--platform', type=str, default=None,
                        choices=['tiktok','youtube','facebook','instagram'],
                        help='Chỉ crawl 1 platform')
    args = parser.parse_args()
    run(year=args.year, month=args.month, platform=args.platform)
