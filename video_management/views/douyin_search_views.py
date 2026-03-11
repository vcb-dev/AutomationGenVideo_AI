"""
Douyin Search Views

API endpoints for searching Douyin videos by keyword or hashtag.
Real-time search without database persistence.
"""

import logging
import random
import time
import datetime
import concurrent.futures
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from ..services.douyin_scraper import DouyinScraperService

logger = logging.getLogger(__name__)


@api_view(['POST'])
def search_douyin_videos(request):
    """
    Search Douyin videos by keyword or hashtag.
    Hỗ trợ "next batch": mỗi lần gọi với batchIndex tăng dần sẽ trả về pool video mới.
    """
    def _get_related_keywords(base: str) -> list:
        base = base.strip().lower()
        if not base: return []
        # Các hậu tố đa dạng để mở rộng pool kết quả (vừa quốc tế vừa địa phương)
        suffixes = ['2025', 'hot', 'style', 'vlog', 'viral']
        results = [base]
        for s in suffixes:
            results.append(f"{base} {s}")
        return results

    try:
        # Extract parameters
        search_term = request.data.get('searchTerm', '').strip()
        search_type = request.data.get('searchType', 'keyword')
        max_posts = int(request.data.get('maxPosts', 6))
        sort_by = request.data.get('sortBy') or None  # None = auto chọn theo batch
        publish_time = request.data.get('publishTime', 'all')
        # --- Next Batch support ---
        batch_index = int(request.data.get('batchIndex', 0))   # 0 = lần đầu, 1,2,... = batch tiếp theo
        seen_ids = set(request.data.get('seenIds', []) or [])   # set video_id đã hiển thị trên FE
        
        # Validation
        if not search_term:
            return Response({
                'success': False,
                'error': 'searchTerm is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if search_type not in ['keyword', 'hashtag']:
            return Response({
                'success': False,
                'error': 'searchType must be "keyword" or "hashtag"'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if sort_by and sort_by not in ['general', 'most_liked', 'latest']:
            return Response({
                'success': False,
                'error': 'sortBy must be "general", "most_liked", or "latest"'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if publish_time not in ['all', 'last_day', 'last_week', 'last_half_year']:
            return Response({
                'success': False,
                'error': 'publishTime must be "all", "last_day", "last_week", or "last_half_year"'
            }, status=status.HTTP_400_BAD_REQUEST)

        # --- Chọn sort_by theo batch (đa dạng kết quả mỗi lần Next Batch) ---
        BATCH_SORT_CYCLE = ['most_liked', 'latest', 'general']
        if not sort_by:
            sort_by = BATCH_SORT_CYCLE[batch_index % len(BATCH_SORT_CYCLE)]

        # Giới hạn fetch_limit max 35 để tiết kiệm Apify credits
        fetch_limit = 35

        logger.info(
            f"Douyin search request - Term: {search_term}, "
            f"Type: {search_type}, FetchLimit: {fetch_limit}"
        )
        
        # Initialize scraper
        scraper = DouyinScraperService()
        
        sort_by = sort_by or 'most_liked'

        # Nếu là keyword, thử mở rộng ra các keyword liên quan để lấy pool đa dạng hơn
        query_terms = [search_term]
        if search_type == 'keyword' and len(search_term) < 10:
             query_terms = _get_related_keywords(search_term)[:3] # Lấy 3 keyword (gốc + 2 phụ)

        videos = []
        
        def _fetch_batch(term):
            try:
                return scraper.search_videos(
                    search_term=term,
                    search_type=search_type,
                    max_posts=fetch_limit // len(query_terms),
                    sort_by=sort_by,
                    publish_time=publish_time
                )
            except Exception as e:
                logger.warning(f"Failed to fetch batch for '{term}': {e}")
                return []

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(_fetch_batch, t) for t in query_terms]
            for future in concurrent.futures.as_completed(futures):
                videos.extend(future.result())

        # Deduplicate results by video_id
        seen_vids = set()
        unique_videos = []
        for v in videos:
            vid = v.get('video_id')
            if vid and vid not in seen_vids:
                seen_vids.add(vid)
                unique_videos.append(v)
        videos = unique_videos
        
        # --- Keyword Relevance Filter ---
        # Khi search_type='keyword': lọc video có caption/hashtag chứa search_term
        # Khi search_type='hashtag': actor đã filter sẵn, không cần lọc thêm
        if search_type == 'keyword' and search_term.strip() and videos:
            kw = search_term.strip().lower()
            def _douyin_relevant(v: dict) -> bool:
                caption = str(v.get('caption', '') or v.get('description', '') or '').lower()
                if kw in caption:
                    return True
                for tag in (v.get('hashtags', []) or []):
                    if kw in str(tag).lower():
                        return True
                return False

            before_kw = len(videos)
            relevant = [v for v in videos if _douyin_relevant(v)]
            logger.info(f"Douyin keyword filter: {before_kw} -> {len(relevant)} (keyword='{kw}')")
            if len(relevant) >= max(before_kw * 0.3, 1):
                videos = relevant
            else:
                # Filter quá chặt: sort relevant lên trước, giữ tất cả
                logger.info("Douyin keyword filter quá chặt, sort by relevance thay vì bỏ")
                videos = sorted(videos, key=lambda v: 0 if _douyin_relevant(v) else 1)

        # --- Engagement Quality Filter: viral ≥ 500 likes hoặc ≥ 50 comments ---
        VIRAL_MIN_LIKES = 500
        VIRAL_MIN_COMMENTS = 50
        viral = [
            v for v in videos
            if (v.get('likes_count') is not None and v.get('likes_count', 0) >= VIRAL_MIN_LIKES)
            or (v.get('comments_count') or 0) >= VIRAL_MIN_COMMENTS
        ]
        videos = viral if viral else videos

        # --- Trend Score Sort: viral + gần đây → điểm cao → lên đầu ---

        def _trend_score(v: dict) -> float:
            likes = v.get('likes_count', 0) or 0
            views = v.get('views_count', 0) or 0
            comments = v.get('comments_count', 0) or 0
            engagement = likes + (views * 0.05) + (comments * 3)

            recency_mult = 1.0
            raw_date = v.get('published_at')
            if raw_date:
                try:
                    published = datetime.datetime.fromisoformat(str(raw_date).replace('Z', '+00:00'))
                    if published.tzinfo:
                        published = published.replace(tzinfo=None)
                    days_ago = (datetime.datetime.now() - published).days
                    if days_ago <= 7:   recency_mult = 2.0
                    elif days_ago <= 30: recency_mult = 1.5
                    elif days_ago <= 90: recency_mult = 1.2
                    else:               recency_mult = 0.8
                except Exception:
                    pass
            return engagement * recency_mult

        videos = sorted(videos, key=_trend_score, reverse=True)
        logger.info(f"Douyin: sorted {len(videos)} videos by trend_score (viral + recency)")

        # --- Loại bỏ video đã thấy (Next Batch: seenIds từ FE) ---
        if seen_ids:
            before_filter = len(videos)
            videos = [v for v in videos if str(v.get('video_id', '')) not in seen_ids]
            logger.info(f"Douyin: filtered out {before_filter - len(videos)} already-seen videos")

        # --- Random Sample (variety per search) ---
        # Seed khác nhau mỗi batch để đảm bảo thứ tự video đa dạng
        seed_val = (int(time.time() * 1000) + batch_index * 999983) % (2**31)
        rng = random.Random(seed_val)
        RETURN_COUNT = 30
        if len(videos) > RETURN_COUNT:
            # Ưu tiên top trend nhưng vẫn lấy ngẫu nhiên 30 bài
            pool_to_sample = videos[:max(RETURN_COUNT + 10, len(videos)//2)]
            videos = rng.sample(pool_to_sample, min(RETURN_COUNT, len(pool_to_sample)))
        else:
            rng.shuffle(videos)

        # hasMore = True nếu pool gốc còn dư video (có thể fetch tiếp)
        # Heuristic: nếu actor trả đủ fetch_limit (35) thì khả năng còn data
        has_more = fetch_limit >= 35

        logger.info(f"Successfully retrieved {len(videos)} Douyin videos (batch={batch_index}, sort={sort_by})")
        
        return Response({
            'success': True,
            'data': {
                'videos': videos,
                'total': len(videos),
                'searchTerm': search_term,
                'searchType': search_type,
                'sortBy': sort_by,
                'publishTime': publish_time,
                'batchIndex': batch_index,
                'hasMore': has_more,
            }
        }, status=status.HTTP_200_OK)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=status.HTTP_400_BAD_REQUEST)
        
    except Exception as e:
        logger.error(f"Douyin search failed: {str(e)}", exc_info=True)
        return Response({
            'success': False,
            'error': f'Search failed: {str(e)}'
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
