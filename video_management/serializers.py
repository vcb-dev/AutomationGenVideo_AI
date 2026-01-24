from rest_framework import serializers
from .models import TrackedChannel, ReportedVideo, SearchCache

class TrackedChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackedChannel
        fields = '__all__'

class ReportedVideoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReportedVideo
        fields = '__all__'

class SearchCacheSerializer(serializers.ModelSerializer):
    class Meta:
        model = SearchCache
        fields = '__all__'

class DouyinSearchSerializer(serializers.Serializer):
    keyword = serializers.CharField(required=True)
    sort_by = serializers.ChoiceField(choices=['likes', 'views', 'like_count', 'view_count'], default='likes', required=False)
    min_likes = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    min_views = serializers.IntegerField(required=False, allow_null=True, min_value=0)

class VideoDownloadSerializer(serializers.Serializer):
    url = serializers.URLField(required=True)

class MusicPostsSerializer(serializers.Serializer):
    music_id = serializers.CharField(required=True, help_text="Music ID (ví dụ: '7224128604890990593')")
    count = serializers.IntegerField(required=False, default=30, min_value=1, max_value=100)
    cursor = serializers.IntegerField(required=False, default=0, min_value=0)
    min_likes = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    min_views = serializers.IntegerField(required=False, allow_null=True, min_value=0)
