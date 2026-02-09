from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone


class SearchQuery(models.Model):
    """
    Track user search queries for suggestions and analytics
    """
    PLATFORM_CHOICES = [
        ('TIKTOK', 'TikTok'),
        ('INSTAGRAM', 'Instagram'),
        ('FACEBOOK', 'Facebook'),
        ('DOUYIN', 'Douyin'),
        ('XIAOHONGSHU', 'Xiaohongshu'),
    ]
    
    query = models.CharField(max_length=255, db_index=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, db_index=True)
    search_count = models.IntegerField(default=1, validators=[MinValueValidator(0)])
    result_count = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    last_searched = models.DateTimeField(auto_now=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Optional: Track user if you have authentication
    # user_id = models.IntegerField(null=True, blank=True, db_index=True)
    
    class Meta:
        db_table = 'video_management_searchquery'
        verbose_name = 'Search Query'
        verbose_name_plural = 'Search Queries'
        indexes = [
            models.Index(fields=['query', 'platform']),
            models.Index(fields=['platform', '-search_count']),
            models.Index(fields=['platform', '-last_searched']),
        ]
        unique_together = ['query', 'platform']
    
    def __str__(self):
        return f"{self.query} ({self.platform}) - {self.search_count} searches"


class TrendingKeyword(models.Model):
    """
    Store trending keywords extracted from videos
    Updated periodically by background job
    """
    PLATFORM_CHOICES = SearchQuery.PLATFORM_CHOICES
    
    keyword = models.CharField(max_length=255, db_index=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES, db_index=True)
    trend_score = models.FloatField(default=0, validators=[MinValueValidator(0)])
    video_count = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    total_views = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    total_likes = models.BigIntegerField(default=0, validators=[MinValueValidator(0)])
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        db_table = 'video_management_trendingkeyword'
        verbose_name = 'Trending Keyword'
        verbose_name_plural = 'Trending Keywords'
        indexes = [
            models.Index(fields=['keyword', 'platform']),
            models.Index(fields=['platform', '-trend_score']),
        ]
        unique_together = ['keyword', 'platform']
    
    def __str__(self):
        return f"{self.keyword} ({self.platform}) - Score: {self.trend_score}"
