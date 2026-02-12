from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import datetime, timedelta
from ..services.apify_service import create_scraper
from ..models import Platform
import logging

logger = logging.getLogger(__name__)

@api_view(['POST'])
def check_hashtag_count(request):
    """
    Check the number of videos with a specific hashtag on a specific date.
    
    POST /api/video-management/hashtags/check/
    Body:
    {
        "hashtag": "vcb",
        "platform": "tiktok",
        "date": "2026-02-12",
        "username": "tuannguyen_vcb"
    }
    """
    hashtag = request.data.get('hashtag', '').strip()
    platform_str = request.data.get('platform', 'TIKTOK').upper()
    check_date_str = request.data.get('date') # Format: YYYY-MM-DD
    username = request.data.get('username', '').strip()
    
    if not hashtag:
        return Response({
            'success': False,
            'error': 'Hashtag is required'
        }, status=status.HTTP_400_BAD_REQUEST)
    
    # Default to today if no date provided
    if not check_date_str:
        check_date = timezone.now().date()
    else:
        try:
            check_date = datetime.strptime(check_date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({
                'success': False,
                'error': 'Invalid date format. Use YYYY-MM-DD'
            }, status=status.HTTP_400_BAD_REQUEST)

    logger.info(f"Checking hashtag #{hashtag} on {platform_str} for date {check_date} (User: {username or 'All'})")
    
    try:
        scraper = create_scraper(platform_str)
        
        # Build search term: ensure hashtag is correctly formatted for search
        search_hashtag = hashtag if hashtag.startswith('#') else f"#{hashtag}"
        clean_hashtag = search_hashtag.lower()
        
        # Fetch results
        if username:
            # Strategy: Fetch latest videos from this specific user
            logger.info(f"Fetching videos for specific channel: @{username}")
            # Increase limit to 100 to ensure we find videos from a few days ago if needed
            raw_results = scraper.get_user_videos(username, max_results=100)
        else:
            # Strategy: Search platform-wide by hashtag
            raw_results = scraper.search_videos(search_hashtag, max_results=100)
        
        matching_videos = []
        for raw in raw_results:
            try:
                norm = scraper.normalize_video_data(raw)
                if norm and norm.get('published_at'):
                    # 1. Check Date
                    pub_datetime = norm['published_at']
                    pub_date = pub_datetime.date()
                    
                    if pub_date != check_date:
                        continue
                        
                    # 2. Check Hashtag (if searching by user) or Title/Desc
                    content = f"{norm.get('title', '')} {norm.get('description', '')}".lower()
                    
                    # Robust hashtag matching
                    has_hashtag = False
                    
                    # Direct text check
                    if clean_hashtag in content:
                        has_hashtag = True
                    
                    # Check in normalized hashtag list
                    if not has_hashtag:
                        pure_tag = hashtag.lower().lstrip('#')
                        norm_hashtags = [h.lower().lstrip('#') for h in norm.get('hashtags', [])]
                        if pure_tag in norm_hashtags:
                            has_hashtag = True
                    
                    # Special check for some actors that put hashtags in challenges/other fields
                    if not has_hashtag and 'challenges' in raw:
                        challenges = raw.get('challenges', [])
                        if isinstance(challenges, list):
                            for c in challenges:
                                c_title = str(c.get('title', '')).lower()
                                if c_title == hashtag.lower().lstrip('#'):
                                    has_hashtag = True
                                    break

                    if has_hashtag:
                        matching_videos.append({
                            'video_id': norm.get('video_id'),
                            'title': norm.get('title'),
                            'video_url': norm.get('video_url'),
                            'thumbnail_url': norm.get('thumbnail_url'),
                            'author_username': norm.get('author_username'),
                            'likes_count': norm.get('likes_count'),
                            'views_count': norm.get('views_count'),
                            'published_at': pub_datetime.isoformat()
                        })
            except Exception as e:
                logger.warning(f"Error normalizing video in hashtag check: {e}")
                continue
        
        return Response({
            'success': True,
            'hashtag': hashtag,
            'username': username,
            'platform': platform_str,
            'date': check_date.isoformat(),
            'count': len(matching_videos),
            'results': matching_videos
        })
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error checking hashtag: {error_msg}", exc_info=True)
        
        # Friendly message for Apify limit issues
        if "limit exceeded" in error_msg.lower() or "usage hard limit" in error_msg.lower():
            return Response({
                'success': False,
                'error': 'Tài khoản Apify của bạn đã hết dung lượng (credit) tháng này. Vui lòng kiểm tra lại gói cước hoặc đổi Token mới trong file .env.'
            }, status=status.HTTP_402_PAYMENT_REQUIRED)
        
        # Friendly message for Invalid ID/URL errors (Common when user enters Nickname instead of Username)
        if "FAILED" in error_msg or "Invalid URLs" in error_msg:
             return Response({
                'success': False,
                'error': 'Không tìm thấy Kênh! Vui lòng nhập đúng "ID người dùng" (Unique ID) bắt đầu bằng dấu @, không phải Biệt danh (Nickname).'
            }, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'success': False,
            'error': f'Lỗi hệ thống quét video: {error_msg}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
