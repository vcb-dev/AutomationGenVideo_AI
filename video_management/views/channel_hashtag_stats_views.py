from rest_framework.decorators import api_view

from rest_framework.response import Response

from rest_framework import status

from video_management.models import ScrapedVideo, TrackedChannel

from video_management.models import Platform

from django.db.models import Q

import re

from datetime import datetime





@api_view(['GET', 'POST'])
def get_channel_hashtag_stats(request):
    """
    Get hashtag statistics for a specific channel
    Uses existing videos in DB (from previous searches)
    Count videos with company hashtags: a1, a2, a3, a4, a5
    """
    COMPANY_HASHTAGS = ['a1', 'a2', 'a3', 'a4', 'a5']
    
    try:
        # Get params from QueryString (GET) or Body (POST)
        username = request.data.get('username') or request.query_params.get('username')
        platform_str = request.data.get('platform') or request.query_params.get('platform') or 'TIKTOK'
        
        if not username:
            return Response(
                {'error': 'Username is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        print(f"Stats request for {username} on {platform_str}")
        
        # Get all videos for this channel from DB using author_username
        existing_videos = ScrapedVideo.objects.filter(author_username__iexact=username)
        video_count = existing_videos.count()
        
        # Determine platform enum
        platform_map = {
            'TIKTOK': Platform.TIKTOK,
            'INSTAGRAM': Platform.INSTAGRAM,
            'FACEBOOK': Platform.FACEBOOK,
        }
        platform_enum = platform_map.get(platform_str.upper(), Platform.TIKTOK)
        
        # NOTE: We don't have total_channel_videos info here without TrackedChannel
        # But we can try to guess or use video_count
        total_channel_videos = video_count 
        
        if total_channel_videos == 0 or total_channel_videos < video_count:
            total_channel_videos = video_count
        
        # Calculate coverage (default 100% since we assume we have what we have)
        coverage_percentage = 100.0
        
        # Count videos for each hashtag
        hashtag_stats = []
        total_videos = existing_videos.count()
        
        for hashtag in COMPANY_HASHTAGS:
            # Search for hashtag in multiple places:
            # 1. In the hashtags JSON array (most accurate)
            # 2. In description text
            # 3. In title text
            
            count = existing_videos.filter(
                # Search in hashtags JSON field (array contains)
                Q(hashtags__icontains=hashtag) |
                Q(hashtags__icontains=hashtag.upper()) |
                # Search in description
                Q(description__icontains=f'#{hashtag}') |
                Q(description__icontains=f'#{hashtag.upper()}') |
                Q(description__icontains=f' {hashtag} ') |
                Q(description__icontains=f' {hashtag.upper()} ') |
                # Search in title
                Q(title__icontains=f'#{hashtag}') |
                Q(title__icontains=f'#{hashtag.upper()}') |
                Q(title__icontains=f' {hashtag} ') |
                Q(title__icontains=f' {hashtag.upper()} ')
            ).count()
            
            percentage = (count / total_videos * 100) if total_videos > 0 else 0
            
            hashtag_stats.append({
                'hashtag': hashtag,
                'count': count,
                'percentage': round(percentage, 2)
            })
        
        # Sort by count descending
        hashtag_stats.sort(key=lambda x: x['count'], reverse=True)
        
        # Get last updated time
        last_updated = None
        if total_videos > 0:
            latest_video = existing_videos.order_by('-created_at').first()
            if latest_video:
                last_updated = latest_video.created_at.isoformat()
        
        return Response({
            'channel_username': username,
            'total_videos': total_videos,
            'total_channel_videos': total_channel_videos,
            'analyzed_videos': total_videos,
            'coverage_percentage': 100, # Assume 100% since we don't have external total
            'hashtag_stats': hashtag_stats,
            'last_updated': last_updated,
            'message': f'Analyzed {total_videos} videos'
        })
        
    except Exception as e:
        print(f"Error in get_channel_hashtag_stats: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
