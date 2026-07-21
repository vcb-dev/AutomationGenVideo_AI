from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta
from video_management.models import ScrapedVideo
from video_management.serializers import VideoSerializer
from django.db.models import Q


@api_view(['GET'])
def search_videos_by_hashtag(request):
    """
    Search videos by hashtag from the last N days (default 30)
    Case-insensitive search.
    Deprecated: Use channel-specific stats instead.
    """
    hashtag = request.GET.get('hashtag', '').strip()
    days = int(request.GET.get('days', 30))
    
    if not hashtag:
        return Response(
            {'error': 'Hashtag parameter is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Remove # if present and convert to lowercase
    clean_hashtag = hashtag.lstrip('#').lower()
    
    # Calculate date range
    end_date = timezone.now()
    start_date = end_date - timedelta(days=days)
    
    # Search videos with hashtag in description (case-insensitive)
    videos = ScrapedVideo.objects.filter(
        Q(description__icontains=f'#{clean_hashtag}') | 
        Q(description__icontains=clean_hashtag),
        published_at__gte=start_date,
        published_at__lte=end_date
    ).order_by('-published_at')[:100]
    
    serializer = VideoSerializer(videos, many=True)
    
    return Response({
        'hashtag': clean_hashtag,
        'total_results': videos.count(),
        'search_period': f'{days} days',
        'date_range': {
            'from': start_date.isoformat(),
            'to': end_date.isoformat()
        },
        'videos': serializer.data
    })
