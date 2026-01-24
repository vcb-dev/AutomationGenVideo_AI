from django.db import models
from django.utils import timezone

class TrackedChannel(models.Model):
    channel_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    threshold_likes = models.IntegerField(default=1000)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

class ReportedVideo(models.Model):
    video_id = models.CharField(max_length=255, unique=True)
    timestamp = models.DateTimeField(default=timezone.now)
    channel = models.ForeignKey(TrackedChannel, on_delete=models.CASCADE, null=True, blank=True)
    likes_at_report = models.IntegerField(default=0)

    def __str__(self):
        return self.video_id

class SearchCache(models.Model):
    keyword = models.CharField(max_length=255, unique=True)
    raw_json = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def is_expired(self):
        return timezone.now() > self.expires_at

    def __str__(self):
        return self.keyword
