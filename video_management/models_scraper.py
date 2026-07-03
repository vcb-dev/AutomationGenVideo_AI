"""
Models for the Facebook Fanpage & Reels Scraping System.

Architecture:
- SearchKeyword: Keywords for Google discovery + autocomplete suggestions
- ScrapedFanpage: Discovered Facebook pages (non-official, niche)
- FanpageMetricsHistory: Timeseries snapshots of page followers/likes
- FacebookReel: Individual Reels/Videos scraped from pages
- ReelMetricsHistory: Timeseries snapshots of reel views/likes/comments/shares
"""

from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.utils import timezone


class ScrapingStatus(models.TextChoices):
    IDLE = 'idle', 'Idle'
    PROCESSING = 'processing', 'Processing'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'


# ═══════════════════════════════════════════════════════════
#  1. SEARCH KEYWORD (Smart Search & Google Discovery)
# ═══════════════════════════════════════════════════════════

class SearchKeyword(models.Model):
    """Keywords for page discovery (Google) and autocomplete suggestions.

    - raw_keyword: exact query user typed (may contain operators like site:facebook.com)
    - cleaned_keyword: human-readable version for UI display/autocomplete
    - hit_count: popularity counter for sorting suggestions
    - is_google_active: whether this keyword is used in automated Google scraping
    """
    raw_keyword = models.TextField(
        help_text="Full Google search query including operators"
    )
    cleaned_keyword = models.CharField(
        max_length=500,
        db_index=True,
        help_text="Clean keyword for display/autocomplete (e.g. 'đá quý')"
    )
    cleaned_keyword_ascii = models.CharField(
        max_length=500,
        db_index=True,
        blank=True,
        help_text="Unaccented version for fast prefix/substring search"
    )
    hit_count = models.IntegerField(
        default=0,
        db_index=True,
        help_text="Search frequency — higher = more popular suggestion"
    )
    is_google_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Include in periodic Google scraping runs"
    )
    last_searched_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Last time this keyword was used in Google discovery"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_search_keywords'
        ordering = ['-hit_count', '-updated_at']
        indexes = [
            models.Index(fields=['cleaned_keyword_ascii', '-hit_count'],
                         name='idx_keyword_ascii_popularity'),
            models.Index(fields=['is_google_active', 'last_searched_at'],
                         name='idx_keyword_google_schedule'),
        ]

    def __str__(self):
        return f"{self.cleaned_keyword} (hits: {self.hit_count})"

    def save(self, *args, **kwargs):
        if not self.cleaned_keyword_ascii:
            from unidecode import unidecode
            self.cleaned_keyword_ascii = unidecode(self.cleaned_keyword).lower()
        super().save(*args, **kwargs)


# ═══════════════════════════════════════════════════════════
#  2. SCRAPED FANPAGE (Core Entity)
# ═══════════════════════════════════════════════════════════

class ScrapedFanpage(models.Model):
    """Facebook Fanpage discovered via Google search or manual input.

    Deduplication: profile_id is the unique Facebook numeric ID.
    """
    # ── Identity ──
    profile_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Facebook numeric profile ID (unique, used for dedup)"
    )
    name = models.CharField(
        max_length=500,
        help_text="Page display name"
    )
    handle = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        help_text="Facebook handle/username (e.g. 'katjewelry')"
    )
    page_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="Clean canonical URL (e.g. https://www.facebook.com/katjewelry)"
    )
    avatar_url = models.TextField(blank=True)
    avatar_drive_url = models.TextField(blank=True)
    header_image_url = models.TextField(blank=True)
    header_image_drive_url = models.TextField(blank=True)
    is_verified = models.BooleanField(null=True, blank=True)

    # ── Latest Metrics Snapshot (denormalized for fast list queries) ──
    followers_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)

    # ── UI & Automation Flags ──
    is_visible_on_ui = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Show this page on frontend dashboard"
    )
    is_periodic_crawl = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Include in automated cron-job reel scraping"
    )
    crawl_interval_days = models.IntegerField(
        default=1,
        help_text="How often (days) to re-scrape reels for this page"
    )

    is_bookmarked = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Ưu tiên hiển thị lên đầu (lưu/bookmark)"
    )
    is_initial_scraped = models.BooleanField(
        default=False,
        help_text="True nếu đã cào lượt đầu (300 reels). Lần sau chỉ cào 10 mới."
    )

    # ── Scraping State ──
    scraping_status = models.CharField(
        max_length=20,
        choices=ScrapingStatus.choices,
        default=ScrapingStatus.IDLE,
    )
    last_scraped_at = models.DateTimeField(
        null=True, blank=True,
        db_index=True,
        help_text="Last successful reel scrape timestamp"
    )
    scrape_error = models.TextField(blank=True, null=True)

    # ── Discovery Link (M2M via Prisma join table) ──
    discovered_keywords = models.ManyToManyField(
        SearchKeyword,
        through='FanpageKeywordLink',
        blank=True,
        related_name='discovered_pages',
        help_text="Which search keywords led to discovering this page"
    )

    # ── Timestamps ──
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_fanpages'
        ordering = ['-followers_count']
        indexes = [
            models.Index(fields=['is_periodic_crawl', 'last_scraped_at'],
                         name='idx_fanpage_crawl_schedule'),
            models.Index(fields=['is_visible_on_ui', '-followers_count'],
                         name='idx_fanpage_ui_list'),
            models.Index(fields=['-created_at'],
                         name='idx_fanpage_newest'),
        ]

    def __str__(self):
        return f"{self.name} (@{self.handle})" if self.handle else self.name


# ═══════════════════════════════════════════════════════════
#  3. FANPAGE METRICS HISTORY (Timeseries)
# ═══════════════════════════════════════════════════════════

class FanpageMetricsHistory(models.Model):
    """Point-in-time snapshot of a page's followers and likes.

    Used for trend graphs (e.g. follower growth over weeks).
    """
    fanpage = models.ForeignKey(
        ScrapedFanpage,
        on_delete=models.CASCADE,
        related_name='metrics_history'
    )
    followers_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        managed = False
        db_table = 'scraper_fanpage_metrics_history'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['fanpage', '-captured_at'],
                         name='idx_fp_metrics_timeline'),
        ]

    def __str__(self):
        return f"{self.fanpage.name} @ {self.captured_at:%Y-%m-%d}: {self.followers_count} followers"


# ═══════════════════════════════════════════════════════════
#  4. FACEBOOK REEL (Core Entity)
# ═══════════════════════════════════════════════════════════

class FacebookReel(models.Model):
    """Individual Reel/Video scraped from a Facebook Fanpage.

    Deduplication: post_id is unique across all reels.
    Incremental scraping: date_posted index allows "stop at last known date" logic.
    """
    fanpage = models.ForeignKey(
        ScrapedFanpage,
        on_delete=models.CASCADE,
        related_name='reels'
    )

    # ── Identity (Dedup) ──
    post_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Facebook post ID (unique, primary dedup key)"
    )
    shortcode = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        help_text="Reel shortcode from URL (e.g. '1332404888982806')"
    )

    # ── Content ──
    url = models.URLField(
        max_length=1000,
        help_text="Direct reel URL (e.g. https://www.facebook.com/reel/...)"
    )
    content = models.TextField(
        blank=True,
        help_text="Reel caption/description text"
    )
    hashtags = ArrayField(
        models.CharField(max_length=200),
        default=list,
        blank=True,
        help_text="Extracted hashtags as list"
    )
    video_url = models.TextField(
        blank=True,
        help_text="Direct video stream URL (.mp4)"
    )
    thumbnail_url = models.TextField(blank=True)
    thumbnail_drive_url = models.TextField(blank=True)
    duration_seconds = models.FloatField(
        null=True, blank=True,
        help_text="Video length in seconds"
    )
    has_audio = models.BooleanField(
        default=True,
        help_text="Whether video has audio track available"
    )

    # ── Publishing Time (from Facebook, not our DB insert time) ──
    date_posted = models.DateTimeField(
        db_index=True,
        help_text="When the reel was published on Facebook (used for incremental scraping)"
    )

    # ── Latest Metrics Snapshot (denormalized for fast sorting/filtering) ──
    views_count = models.BigIntegerField(default=0, db_index=True)
    likes_count = models.BigIntegerField(default=0)
    comments_count = models.BigIntegerField(default=0)
    shares_count = models.BigIntegerField(default=0)

    # ── System ──
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'scraper_facebook_reels'
        ordering = ['-date_posted']
        indexes = [
            models.Index(fields=['fanpage', '-date_posted'],
                         name='idx_reel_page_date'),
            models.Index(fields=['-views_count'],
                         name='idx_reel_views_desc'),
            models.Index(fields=['fanpage', '-views_count'],
                         name='idx_reel_page_views'),
        ]

    def __str__(self):
        return f"Reel {self.shortcode} ({self.views_count} views)"


# ═══════════════════════════════════════════════════════════
#  5. REEL METRICS HISTORY (Timeseries)
# ═══════════════════════════════════════════════════════════

class ReelMetricsHistory(models.Model):
    """Point-in-time snapshot of a reel's engagement metrics.

    Allows tracking viral growth curves (views over hours/days).
    """
    reel = models.ForeignKey(
        FacebookReel,
        on_delete=models.CASCADE,
        related_name='metrics_history'
    )
    views_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)
    comments_count = models.BigIntegerField(default=0)
    shares_count = models.BigIntegerField(default=0)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        managed = False
        db_table = 'scraper_reel_metrics_history'
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['reel', '-captured_at'],
                         name='idx_reel_metrics_timeline'),
        ]

    def __str__(self):
        return f"Reel {self.reel.shortcode} @ {self.captured_at:%Y-%m-%d}: {self.views_count} views"


# ═══════════════════════════════════════════════════════════
#  6. M2M THROUGH MODEL (Fanpage ↔ Keyword)
# ═══════════════════════════════════════════════════════════

class FanpageKeywordLink(models.Model):
    """Join table mapping to Prisma's scraper_fanpage_keywords."""
    fanpage = models.ForeignKey(ScrapedFanpage, on_delete=models.CASCADE, db_column='fanpage_id')
    keyword = models.ForeignKey(SearchKeyword, on_delete=models.CASCADE, db_column='keyword_id')

    class Meta:
        managed = False
        db_table = 'scraper_fanpage_keywords'
        unique_together = [['fanpage', 'keyword']]


# ═══════════════════════════════════════════════════════════
#  7. TIKTOK VIDEO (Video TikTok — author inline)
# ═══════════════════════════════════════════════════════════

class DouyinVideo(models.Model):
    """Video Douyin được cào bằng keyword search qua TikHub."""
    post_id = models.CharField(max_length=50, unique=True, db_index=True)
    shortcode = models.CharField(max_length=50, unique=True, db_index=True)
    url = models.URLField(max_length=1000)
    description = models.TextField(blank=True)
    hashtags = ArrayField(models.CharField(max_length=200), default=list, blank=True)
    preview_image = models.TextField(blank=True)
    video_duration = models.IntegerField(default=0)
    region = models.CharField(max_length=10, blank=True)

    author_id = models.CharField(max_length=50, blank=True, db_index=True)
    author_username = models.CharField(max_length=255, blank=True)
    author_display_name = models.CharField(max_length=500, blank=True)
    author_avatar = models.TextField(blank=True)
    author_followers = models.BigIntegerField(default=0)
    author_is_verified = models.BooleanField(default=False)

    digg_count = models.BigIntegerField(default=0)
    comment_count = models.BigIntegerField(default=0)
    share_count = models.BigIntegerField(default=0)
    collect_count = models.BigIntegerField(default=0)

    music_title = models.CharField(max_length=500, blank=True)
    music_author = models.CharField(max_length=255, blank=True)

    search_keyword = models.CharField(max_length=500, blank=True, db_index=True)
    date_posted = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'scraper_douyin_videos'
        ordering = ['-date_posted']
        indexes = [
            models.Index(fields=['-date_posted'], name='idx_douyin_date_desc'),
        ]

    def __str__(self):
        return f"Douyin {self.shortcode}"


# ═══════════════════════════════════════════════════════════
#  DOUYIN PROFILE (User theo dõi)
# ═══════════════════════════════════════════════════════════

class DouyinProfile(models.Model):
    """Tài khoản Douyin được theo dõi — cào video qua sec_user_id."""
    sec_user_id = models.CharField(max_length=255, unique=True, db_index=True)
    uid = models.CharField(max_length=50, blank=True, db_index=True)
    username = models.CharField(max_length=255, blank=True, db_index=True)
    nickname = models.CharField(max_length=500, blank=True)
    avatar_url = models.TextField(blank=True)
    avatar_drive_url = models.TextField(blank=True)
    biography = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)

    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)
    videos_count = models.IntegerField(default=0)

    is_bookmarked = models.BooleanField(default=False)
    is_tracked = models.BooleanField(default=False)
    is_owned = models.BooleanField(default=False, db_index=True)
    is_initial_scraped = models.BooleanField(default=False)
    last_scraped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scraping_status = models.CharField(
        max_length=20, choices=ScrapingStatus.choices, default=ScrapingStatus.IDLE,
    )
    scrape_error = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_douyin_profiles'
        ordering = ['-followers_count']

    def __str__(self):
        return f"@{self.username} ({self.followers_count} followers)"


class TikTokVideo(models.Model):
    """Individual TikTok video discovered via keyword search."""
    post_id = models.CharField(max_length=50, unique=True, db_index=True)
    shortcode = models.CharField(max_length=50, unique=True, db_index=True)
    url = models.URLField(max_length=1000)
    description = models.TextField(blank=True)
    hashtags = ArrayField(
        models.CharField(max_length=200), default=list, blank=True,
    )
    video_url = models.TextField(blank=True)
    cdn_url = models.TextField(blank=True)
    preview_image = models.TextField(blank=True)
    video_duration = models.IntegerField(default=0)
    region = models.CharField(max_length=10, blank=True)

    # Author (inline — chưa tách model riêng)
    author_id = models.CharField(max_length=50, blank=True, db_index=True)
    author_username = models.CharField(max_length=255, blank=True)
    author_display_name = models.CharField(max_length=500, blank=True)
    author_avatar = models.TextField(blank=True)
    author_url = models.URLField(max_length=1000, blank=True)
    author_followers = models.BigIntegerField(default=0)
    author_is_verified = models.BooleanField(default=False)

    # Metrics
    play_count = models.BigIntegerField(default=0, db_index=True)
    digg_count = models.BigIntegerField(default=0)
    comment_count = models.BigIntegerField(default=0)
    share_count = models.BigIntegerField(default=0)
    collect_count = models.BigIntegerField(default=0)

    # Music
    music_title = models.CharField(max_length=500, blank=True)
    music_author = models.CharField(max_length=255, blank=True)
    original_sound = models.TextField(blank=True)

    # Discovery
    search_keyword = models.CharField(max_length=500, blank=True, db_index=True)
    date_posted = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = 'scraper_tiktok_videos'
        ordering = ['-date_posted']
        indexes = [
            models.Index(fields=['-play_count'], name='idx_tiktok_plays_desc'),
            models.Index(fields=['-date_posted'], name='idx_tiktok_date_desc'),
        ]

    def __str__(self):
        return f"TikTok {self.shortcode} ({self.play_count} plays)"


# ═══════════════════════════════════════════════════════════
#  8. TIKTOK PROFILE (User theo dõi)
# ═══════════════════════════════════════════════════════════

class TikTokProfile(models.Model):
    """TikTok user profile — lấy từ BrightData profile dataset.

    Flow: User nhập profile URL → lấy info → đánh dấu theo dõi →
    cron định kỳ kéo videos mới + cập nhật metrics.
    """
    # ── Identity ──
    profile_id = models.CharField(
        max_length=50, unique=True, db_index=True,
        help_text="TikTok numeric user ID (e.g. '6985352736884933633')"
    )
    sec_uid = models.CharField(
        max_length=255, blank=True, db_index=True,
        help_text="TikTok sec_uid — permanent, không đổi khi user thay username. Dùng cho periodic scrape."
    )
    username = models.CharField(max_length=255, unique=True, db_index=True)
    nickname = models.CharField(max_length=500, blank=True)
    url = models.URLField(max_length=1000)
    avatar_url = models.TextField(blank=True)
    avatar_url_hd = models.TextField(blank=True)
    avatar_drive_url = models.TextField(blank=True)
    biography = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    is_private = models.BooleanField(default=False)
    predicted_lang = models.CharField(max_length=10, blank=True)

    # ── Metrics (snapshot mới nhất) ──
    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)
    videos_count = models.IntegerField(default=0)

    # ── Engagement rates ──
    avg_engagement_rate = models.FloatField(default=0)
    like_engagement_rate = models.FloatField(default=0)
    comment_engagement_rate = models.FloatField(default=0)

    # ── Account metadata ──
    account_created_at = models.DateTimeField(null=True, blank=True)
    is_commerce_user = models.BooleanField(default=False)

    # ── Tracking flags ──
    is_tracked = models.BooleanField(
        default=False, db_index=True,
        help_text="Đánh dấu theo dõi — cron sẽ kéo videos mới định kỳ"
    )
    is_bookmarked = models.BooleanField(default=False, db_index=True)
    is_owned = models.BooleanField(default=False, db_index=True)
    is_initial_scraped = models.BooleanField(
        default=False,
        help_text="True nếu đã cào lượt đầu (1000 posts). Lần sau chỉ cào 1 tháng gần nhất."
    )
    last_scraped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scraping_status = models.CharField(
        max_length=20, choices=ScrapingStatus.choices, default=ScrapingStatus.IDLE,
    )
    scrape_error = models.TextField(blank=True, null=True)

    # ── Timestamps ──
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_tiktok_profiles'
        ordering = ['-followers_count']

    def __str__(self):
        return f"@{self.username} ({self.followers_count} followers)"


# ═══════════════════════════════════════════════════════════
#  9. TIKTOK PROFILE VIDEO (Video của user theo dõi)
# ═══════════════════════════════════════════════════════════

class TikTokProfileVideo(models.Model):
    """Video thuộc về 1 TikTok profile đang theo dõi.

    Tách riêng khỏi TikTokVideo (keyword search) vì:
    - Source khác (profile dataset vs keyword dataset)
    - Lifecycle khác (cào định kỳ theo user vs cào 1 lần theo keyword)
    - Fields khác (không có search_keyword, có favorites_count)
    """
    profile = models.ForeignKey(
        TikTokProfile, on_delete=models.CASCADE, related_name='videos',
    )
    video_id = models.CharField(max_length=50, unique=True, db_index=True)
    shortcode = models.CharField(max_length=50, blank=True)
    url = models.URLField(max_length=1000)
    description = models.TextField(blank=True)
    hashtags = ArrayField(
        models.CharField(max_length=200), default=list, blank=True,
    )
    cover_image = models.TextField(blank=True)
    video_duration = models.IntegerField(default=0)
    region = models.CharField(max_length=10, blank=True)
    post_type = models.CharField(
        max_length=20, default='video',
        help_text="video hoặc photo"
    )

    # ── Metrics (cập nhật định kỳ) ──
    play_count = models.BigIntegerField(default=0, db_index=True)
    digg_count = models.BigIntegerField(default=0)
    comment_count = models.BigIntegerField(default=0)
    share_count = models.BigIntegerField(default=0)
    favorites_count = models.BigIntegerField(default=0)

    # ── Music ──
    music_title = models.CharField(max_length=500, blank=True)
    music_author = models.CharField(max_length=255, blank=True)
    original_sound = models.TextField(blank=True)

    date_posted = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_tiktok_profile_videos'
        ordering = ['-date_posted']
        indexes = [
            models.Index(fields=['profile', '-date_posted'], name='idx_ttprofile_vid_date'),
            models.Index(fields=['-play_count'], name='idx_ttprofile_vid_plays'),
        ]

    def __str__(self):
        return f"TikTok {self.video_id} ({self.play_count} plays)"


# ═══════════════════════════════════════════════════════════
#  10. TIKTOK PROFILE METRICS HISTORY (Timeseries)
# ═══════════════════════════════════════════════════════════

class TikTokProfileMetrics(models.Model):
    """Snapshot metrics của profile theo thời gian (trend graph)."""
    profile = models.ForeignKey(
        TikTokProfile, on_delete=models.CASCADE, related_name='metrics_history',
    )
    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    likes_count = models.BigIntegerField(default=0)
    videos_count = models.IntegerField(default=0)
    avg_engagement_rate = models.FloatField(default=0)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        managed = False
        db_table = 'scraper_tiktok_profile_metrics'
        ordering = ['-captured_at']

    def __str__(self):
        return f"@{self.profile.username} @ {self.captured_at:%Y-%m-%d}"


# ═══════════════════════════════════════════════════════════
#  11. INSTAGRAM PROFILE (User theo dõi)
# ═══════════════════════════════════════════════════════════

class InstagramProfile(models.Model):
    """Instagram user profile — lấy từ TikHub fetch_user_info + fetch_user_reels.

    Flow: User nhập username → fetch user info (follower counts, bio, v.v.) →
    fetch reels → đánh dấu theo dõi → cron định kỳ lặp lại.
    """
    instagram_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    username = models.CharField(max_length=255, unique=True, db_index=True)
    full_name = models.CharField(max_length=500, blank=True)
    url = models.URLField(max_length=1000)
    avatar_url = models.TextField(blank=True)
    hd_avatar_url = models.TextField(blank=True)
    avatar_drive_url = models.TextField(blank=True)
    biography = models.TextField(blank=True)
    bio_links = models.JSONField(default=list, blank=True)
    external_url = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    is_private = models.BooleanField(default=False)
    is_business = models.BooleanField(default=False)
    category = models.CharField(max_length=255, blank=True)

    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    posts_count = models.IntegerField(default=0)

    is_tracked = models.BooleanField(
        default=False, db_index=True,
        help_text="Đánh dấu theo dõi — cron sẽ kéo reels mới định kỳ"
    )
    is_bookmarked = models.BooleanField(default=False, db_index=True)
    is_owned = models.BooleanField(default=False, db_index=True)
    is_initial_scraped = models.BooleanField(
        default=False,
        help_text="True nếu đã cào lượt đầu. Lần sau chỉ cào 1 tháng gần nhất."
    )
    last_scraped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scraping_status = models.CharField(
        max_length=20, choices=ScrapingStatus.choices, default=ScrapingStatus.IDLE,
    )
    scrape_error = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_instagram_profiles'
        ordering = ['-followers_count']

    def __str__(self):
        return f"@{self.username} ({self.followers_count} followers)"


# ═══════════════════════════════════════════════════════════
#  12. INSTAGRAM REEL (Video của user theo dõi)
# ═══════════════════════════════════════════════════════════

class InstagramReel(models.Model):
    """Reel thuộc về 1 Instagram profile đang theo dõi."""
    profile = models.ForeignKey(
        InstagramProfile, on_delete=models.CASCADE, related_name='reels',
    )
    post_id = models.CharField(max_length=50, unique=True, db_index=True)
    shortcode = models.CharField(max_length=50, unique=True, db_index=True)
    url = models.URLField(max_length=1000)
    description = models.TextField(blank=True)
    hashtags = ArrayField(
        models.CharField(max_length=200), default=list, blank=True,
    )
    thumbnail_url = models.TextField(blank=True)
    thumbnail_drive_url = models.TextField(blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    is_paid_partnership = models.BooleanField(default=False)

    views_count = models.BigIntegerField(default=0)
    play_count = models.BigIntegerField(default=0, db_index=True)
    likes_count = models.BigIntegerField(default=0)
    comments_count = models.BigIntegerField(default=0)

    date_posted = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_instagram_reels'
        ordering = ['-date_posted']
        indexes = [
            models.Index(fields=['profile', '-date_posted'], name='idx_insta_reel_date'),
            models.Index(fields=['-play_count'], name='idx_insta_reel_plays'),
        ]

    def __str__(self):
        return f"Reel {self.shortcode} ({self.play_count} plays)"


# ═══════════════════════════════════════════════════════════
#  13. INSTAGRAM PROFILE METRICS HISTORY (Timeseries)
# ═══════════════════════════════════════════════════════════

class InstagramProfileMetrics(models.Model):
    """Snapshot metrics của Instagram profile theo thời gian."""
    profile = models.ForeignKey(
        InstagramProfile, on_delete=models.CASCADE, related_name='metrics_history',
    )
    followers_count = models.BigIntegerField(default=0)
    following_count = models.BigIntegerField(default=0)
    posts_count = models.IntegerField(default=0)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        managed = False
        db_table = 'scraper_instagram_profile_metrics'
        ordering = ['-captured_at']


# ═══════════════════════════════════════════════════════════
#  14. XIAOHONGSHU USER PROFILE
# ═══════════════════════════════════════════════════════════

class XiaohongshuProfile(models.Model):
    """User profile trên Xiaohongshu — theo dõi qua TikHub."""
    user_id = models.CharField(max_length=100, unique=True, db_index=True)
    nickname = models.CharField(max_length=500, blank=True)
    avatar_url = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    is_tracked = models.BooleanField(default=True, db_index=True)
    is_bookmarked = models.BooleanField(default=False)
    is_owned = models.BooleanField(default=False, db_index=True)
    is_initial_scraped = models.BooleanField(default=False)
    last_scraped_at = models.DateTimeField(null=True, blank=True)
    scraping_status = models.CharField(max_length=20, default='idle', db_index=True)
    scrape_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_xiaohongshu_profiles'
        ordering = ['-created_at']

    def __str__(self):
        return f"XHS @{self.nickname or self.user_id}"


# ═══════════════════════════════════════════════════════════
#  15. XIAOHONGSHU VIDEO NOTE (keyword search + profile)
# ═══════════════════════════════════════════════════════════

class XiaohongshuVideo(models.Model):
    """Video note từ Xiaohongshu — keyword search hoặc cào từ profile."""
    note_id = models.CharField(max_length=50, unique=True, db_index=True)
    url = models.URLField(max_length=2000)
    title = models.CharField(max_length=1000, blank=True)
    description = models.TextField(blank=True)
    thumbnail_url = models.TextField(blank=True)
    thumbnail_drive_url = models.TextField(blank=True)
    author_id = models.CharField(max_length=100, blank=True)
    author_name = models.CharField(max_length=500, blank=True)
    author_avatar = models.TextField(blank=True)
    duration_seconds = models.IntegerField(default=0)

    liked_count = models.BigIntegerField(default=0, db_index=True)
    collected_count = models.BigIntegerField(default=0)
    comments_count = models.BigIntegerField(default=0)
    shared_count = models.BigIntegerField(default=0)

    keywords = ArrayField(models.CharField(max_length=200), default=list, blank=True)

    profile = models.ForeignKey(
        XiaohongshuProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='videos',
    )

    date_posted = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'scraper_xiaohongshu_videos'
        ordering = ['-liked_count']
        indexes = [
            models.Index(fields=['-date_posted'], name='idx_xhs_vid_date'),
            models.Index(fields=['-liked_count'], name='idx_xhs_vid_likes'),
        ]

    def __str__(self):
        return f"XHS {self.note_id} ({self.liked_count} likes)"
