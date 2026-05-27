"""
lock_monthly_report.py
══════════════════════════════════════════════════════════════════════════
Chạy vào ngày 7 hàng tháng để chốt số liệu traffic tháng trước vào
bảng social_video_snapshot — giải quyết vấn đề video đăng cuối tháng
chưa kịp tích lũy view trước khi báo cáo.

Timeline:
    Tháng 4:  cron_meta_insights.py crawl hàng ngày
    Ngày 1-6/5: tiếp tục cập nhật view cho video buffer (24/4 - 30/4)
    Ngày 7/5:  script này chốt tháng 4 → số liệu ổn định, công bằng

Chạy thủ công:
    python lock_monthly_report.py               # chốt tháng trước
    python lock_monthly_report.py 2026 4        # chốt tháng 4/2026 cụ thể
    python lock_monthly_report.py 2026 4 --force  # ghi đè nếu đã chốt rồi

Cron (ngày 7 hàng tháng lúc 06:00):
    0 6 7 * * cd /path/to/AutomationGenVideo_AI && python lock_monthly_report.py
══════════════════════════════════════════════════════════════════════════
"""
import os, sys, psycopg2, psycopg2.extras
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DATABASE_URL = (
    os.getenv('DIRECT_URL')
    or os.getenv('DATABASE_URL', '').replace('?pgbouncer=true', '')
)

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ─────────────────────────────────────────────────────────────────────────────

def lock_month(year: int, month: int, force: bool = False) -> int:
    """
    Chốt số liệu của (year, month) vào social_video_snapshot.

    Trả về số bản ghi đã được upsert.
    force=True: chạy lại dù đã chốt trước đó (overwrite số cũ bằng số mới nhất).
    """
    conn = db_conn()
    cur  = conn.cursor()

    # ── 1. Kiểm tra đã chốt chưa ────────────────────────────────────────────
    if not force:
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM social_video_snapshot
            WHERE report_year = %s AND report_month = %s
        """, (year, month))
        existing = cur.fetchone()['cnt']
        if existing > 0:
            print(
                f"[!] Tháng {month}/{year} đã được chốt ({existing} bản ghi).\n"
                f"    Dùng --force để ghi đè với số liệu mới nhất."
            )
            conn.close()
            return 0

    # ── 2. Đếm video sẽ chốt ────────────────────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) AS total
        FROM social_video_report
        WHERE year = %s AND month = %s AND is_active = TRUE
    """, (year, month))
    total = cur.fetchone()['total']

    if total == 0:
        print(f"[!] Không có video active nào cho tháng {month}/{year}. Bỏ qua.")
        conn.close()
        return 0

    print(f"[*] Chốt tháng {month}/{year}: {total} video đang active...")

    # ── 3. Upsert vào snapshot ──────────────────────────────────────────────
    cur.execute("""
        INSERT INTO social_video_snapshot
            (platform, post_id, channel_name, username, owner, team,
             title, views_locked, likes_locked, comments_locked,
             shares_locked, followers_locked, published_at,
             report_year, report_month, locked_at)
        SELECT
            platform, post_id, channel_name, username, owner, team,
            title,
            views    AS views_locked,
            likes    AS likes_locked,
            comments AS comments_locked,
            shares   AS shares_locked,
            followers AS followers_locked,
            published_at,
            year     AS report_year,
            month    AS report_month,
            NOW()    AS locked_at
        FROM social_video_report
        WHERE year = %s AND month = %s AND is_active = TRUE
        ON CONFLICT (platform, post_id, report_year, report_month)
        DO UPDATE SET
            views_locked     = EXCLUDED.views_locked,
            likes_locked     = EXCLUDED.likes_locked,
            comments_locked  = EXCLUDED.comments_locked,
            shares_locked    = EXCLUDED.shares_locked,
            followers_locked = EXCLUDED.followers_locked,
            title            = EXCLUDED.title,
            locked_at        = NOW()
    """, (year, month))

    saved = cur.rowcount
    conn.commit()
    conn.close()

    print(f"✅ Đã chốt {saved} video tháng {month}/{year} vào social_video_snapshot.")
    print(f"   locked_at = {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    return saved


def show_summary(year: int, month: int):
    """In tóm tắt số liệu đã chốt để kiểm tra."""
    conn = db_conn()
    cur  = conn.cursor()
    cur.execute("""
        SELECT
            platform,
            COUNT(*)            AS video_count,
            SUM(views_locked)   AS total_views,
            SUM(likes_locked)   AS total_likes,
            SUM(comments_locked)AS total_comments,
            locked_at::date     AS locked_date
        FROM social_video_snapshot
        WHERE report_year = %s AND report_month = %s
        GROUP BY platform, locked_at::date
        ORDER BY total_views DESC
    """, (year, month))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        print(f"  (Chưa có snapshot tháng {month}/{year})")
        return

    print(f"\n  📊 Snapshot tháng {month}/{year}:")
    print(f"  {'Platform':<12} {'Videos':>7} {'Views':>12} {'Likes':>10} {'Comments':>10} {'Locked on'}")
    print(f"  {'-'*67}")
    for r in rows:
        print(
            f"  {r['platform']:<12} {r['video_count']:>7,} "
            f"{int(r['total_views'] or 0):>12,} "
            f"{int(r['total_likes'] or 0):>10,} "
            f"{int(r['total_comments'] or 0):>10,} "
            f"  {r['locked_date']}"
        )


# ─────────────────────────────────────────────────────────────────────────────

def main():
    force = '--force' in sys.argv
    args  = [a for a in sys.argv[1:] if not a.startswith('--')]

    now = datetime.now()

    # Xác định tháng cần chốt
    if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
        lock_year  = int(args[0])
        lock_month_num = int(args[1])
        print(f"[i] Chốt tháng chỉ định: {lock_month_num}/{lock_year}")
    else:
        # Mặc định: tháng trước
        if now.month == 1:
            lock_year, lock_month_num = now.year - 1, 12
        else:
            lock_year, lock_month_num = now.year, now.month - 1
        print(f"[i] Chốt tháng trước: {lock_month_num}/{lock_year}")

    if force:
        print("[i] Chế độ --force: sẽ ghi đè snapshot cũ nếu có.")

    saved = lock_month(lock_year, lock_month_num, force=force)
    if saved > 0:
        show_summary(lock_year, lock_month_num)


if __name__ == '__main__':
    main()
