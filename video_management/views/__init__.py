"""
Views package initialization.
"""

from .search_views import (
    SearchView,
    SearchStatusView,
    SearchHistoryView,
    UserVideosView,
    VideosByChannelView,
)
from .channel_views import (
    ChannelListCreateView,
    ChannelDetailView,
    ChannelCheckView,
    ChannelCheckByUsernameView,
)
from .stats_views import (
    StatsView,
    HealthCheckView,
)
from .mix_video_views import (
    mix_videos,
    mix_videos_auto,
    mix_videos_upload,
    mix_status,
    mix_cancel,
    scan_folder,
    scan_folder_batch,
    video_cache_manage,
)

from .smart_mix_video_views import (
    smart_mix,
    smart_mix_status,
    index_folders,
    cache_stats,
    get_voices,
    generate_audio_from_script,
)

__all__ = [
    # Search views
    'SearchView',
    'SearchStatusView',
    'SearchHistoryView',
    'UserVideosView',
    'VideosByChannelView',
    # Channel views
    'ChannelListCreateView',
    'ChannelDetailView',
    'ChannelCheckView',
    'ChannelCheckByUsernameView',
    # Stats views
    'StatsView',
    'HealthCheckView',
    # Mix views (DEPRECATED - use smart_mix instead!)
    'mix_videos',
    'mix_videos_auto',
    'mix_videos_upload',
    'mix_status',
    'mix_cancel',
    'scan_folder',
    'scan_folder_batch',
    'video_cache_manage',
    # Smart Mix views (NEW - 20-30x faster!)
    'smart_mix',
    'smart_mix_status',
    'index_folders',
    'cache_stats',
    'get_voices',
    'generate_audio_from_script',
]
