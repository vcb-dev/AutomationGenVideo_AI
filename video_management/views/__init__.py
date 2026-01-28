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
)
from .stats_views import (
    StatsView,
    HealthCheckView,
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
    # Stats views
    'StatsView',
    'HealthCheckView',
]
