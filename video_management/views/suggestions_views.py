"""
Search Suggestions API Views
Provides autocomplete/suggestions for search queries using AI and historical data
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import Q, F
from django.utils import timezone
from datetime import timedelta
import logging
import re

from video_management.models import SearchQuery, TrendingKeyword

logger = logging.getLogger(__name__)


@api_view(['GET'])
def get_search_suggestions(request):
    """
    Get search suggestions based on query input.
    
    Combines:
    1. Search history (user's past searches)
    2. Trending keywords (from scraped videos)
    3. AI-generated suggestions (always as fallback)
    
    Query params:
        - q: Search query (min 2 chars)
        - platform: Platform filter (TIKTOK, INSTAGRAM, etc.)
        - limit: Max results (default 10)
    
    Returns:
        {
            "success": true,
            "query": "mèo",
            "suggestions": [
                {"text": "mèo cute", "type": "history", "count": 5},
                {"text": "mèo trắng", "type": "trending", "score": 95.5},
                {"text": "vòng tay mèo", "type": "ai"}
            ]
        }
    """
    query = request.GET.get('q', '').strip()
    platform = request.GET.get('platform', 'TIKTOK').upper()
    limit = int(request.GET.get('limit', 10))
    
    # Validate input
    if not query or len(query) < 2:
        return Response({
            'success': True,
            'query': query,
            'suggestions': []
        })
    
    # Normalize platform
    platform_map = {
        'TIKTOK': 'TIKTOK',
        'INSTAGRAM': 'INSTAGRAM',
        'FACEBOOK': 'FACEBOOK',
        'DOUYIN': 'DOUYIN',
        'XIAOHONGSHU': 'XIAOHONGSHU'
    }
    platform = platform_map.get(platform, 'TIKTOK')
    
    suggestions = []
    
    # 1. Get from search history
    try:
        history = SearchQuery.objects.filter(
            query__icontains=query,
            platform=platform
        ).order_by('-search_count', '-last_searched')[:5]
        
        for h in history:
            suggestions.append({
                'text': h.query,
                'type': 'history',
                'count': h.search_count,
                'priority': 100  # Highest priority
            })
        
        logger.info(f"Found {len(history)} history suggestions for '{query}'")
    except Exception as e:
        logger.error(f"Error fetching search history: {e}")
    
    # 2. Get from trending keywords
    try:
        trending = TrendingKeyword.objects.filter(
            keyword__icontains=query,
            platform=platform
        ).order_by('-trend_score')[:5]
        
        for t in trending:
            suggestions.append({
                'text': t.keyword,
                'type': 'trending',
                'score': float(t.trend_score),
                'priority': 50
            })
        
        logger.info(f"Found {len(trending)} trending suggestions for '{query}'")
    except Exception as e:
        logger.error(f"Error fetching trending keywords: {e}")
    
    # 3. AI-generated suggestions (ALWAYS - ensures we always have results)
    try:
        ai_suggestions = generate_ai_suggestions(query, platform)
        for ai_sug in ai_suggestions:
            suggestions.append({
                'text': ai_sug,
                'type': 'ai',
                'priority': 10
            })
        
        logger.info(f"Generated {len(ai_suggestions)} AI suggestions for '{query}'")
    except Exception as e:
        logger.error(f"AI suggestions failed: {e}")
        # Fallback to simple variations
        fallback = [
            f"{query} hot",
            f"{query} trending",
            f"{query} đẹp",
            f"{query} mới nhất",
            f"{query} viral"
        ]
        for fb in fallback:
            suggestions.append({
                'text': fb,
                'type': 'ai',
                'priority': 5
            })
    
    # 4. Deduplicate & rank
    unique_suggestions = []
    seen = set()
    
    # Sort by priority (history > trending > ai)
    suggestions.sort(key=lambda x: x['priority'], reverse=True)
    
    for sug in suggestions:
        text_lower = sug['text'].lower().strip()
        if text_lower not in seen and text_lower != query.lower():
            seen.add(text_lower)
            # Remove priority from response
            sug.pop('priority', None)
            unique_suggestions.append(sug)
    
    # Return top N
    final_suggestions = unique_suggestions[:limit]
    
    return Response({
        'success': True,
        'query': query,
        'platform': platform,
        'suggestions': final_suggestions,
        'total': len(final_suggestions)
    })


@api_view(['POST'])
def track_search(request):
    """
    Track a search query for suggestions and analytics.
    
    Call this when user actually performs a search (not just typing).
    
    Body:
        {
            "query": "mèo cute",
            "platform": "TIKTOK",
            "result_count": 15  // optional
        }
    
    Returns:
        {"success": true, "tracked": true}
    """
    query = request.data.get('query', '').strip()
    platform = request.data.get('platform', 'TIKTOK').upper()
    result_count = request.data.get('result_count', 0)
    
    if not query:
        return Response({
            'success': False,
            'error': 'Query is required'
        }, status=400)
    
    try:
        # Update or create search query record
        search_query, created = SearchQuery.objects.get_or_create(
            query=query,
            platform=platform,
            defaults={'result_count': result_count}
        )
        
        if not created:
            # Increment search count
            search_query.search_count = F('search_count') + 1
            if result_count > 0:
                search_query.result_count = result_count
            search_query.save()
            search_query.refresh_from_db()
        
        logger.info(f"Tracked search: '{query}' ({platform}) - Count: {search_query.search_count}")
        
        return Response({
            'success': True,
            'tracked': True,
            'search_count': search_query.search_count
        })
    
    except Exception as e:
        logger.error(f"Error tracking search: {e}")
        return Response({
            'success': False,
            'error': str(e)
        }, status=500)


def generate_ai_suggestions(query: str, platform: str) -> list:
    """
    Generate search suggestions using Gemini AI.
    
    Args:
        query: User's search query
        platform: Platform (TIKTOK, INSTAGRAM, etc.)
    
    Returns:
        List of suggestion strings (10-12 items)
    """
    import google.genai as genai
    from google.genai import types
    from django.conf import settings
    
    try:
        # Configure Gemini with new package
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        
        # Context-aware prompt
        context_map = {
            'TIKTOK': 'TikTok videos về kim hoàn, trang sức vàng bạc, phụ kiện thời trang',
            'INSTAGRAM': 'Instagram Reels về jewelry, fashion, accessories',
            'FACEBOOK': 'Facebook videos về trang sức, phụ kiện, lifestyle',
            'DOUYIN': 'Douyin videos về trang sức, thời trang Trung Quốc',
            'XIAOHONGSHU': 'Xiaohongshu posts về beauty, fashion, jewelry'
        }
        
        context = context_map.get(platform, 'social media content về trang sức')
        
        prompt = f"""Bạn là chuyên gia về {context}.
User đang tìm kiếm: "{query}"

Hãy gợi ý CHÍNH XÁC 12 từ khóa tìm kiếm liên quan, phổ biến và hấp dẫn.

YÊU CẦU BẮT BUỘC:
- Mỗi gợi ý trên 1 dòng riêng biệt
- Ngắn gọn (2-5 từ)
- Phải liên quan đến "{query}"
- Phù hợp với ngành kim hoàn/trang sức/phụ kiện
- Bằng tiếng Việt
- KHÔNG đánh số
- KHÔNG dấu đầu dòng
- KHÔNG thêm chú thích
- CHỈ trả về 12 dòng gợi ý

VÍ DỤ với "mèo":
mèo cute
mèo trắng may mắn
vòng tay mèo
charm mèo bạc
mèo thần tài
nhẫn hình mèo
dây chuyền mèo
mèo phong thủy
mặt dây mèo vàng
bông tai mèo
lắc tay mèo bạc
trang sức mèo

BÂY GIỜ hãy gợi ý 12 từ khóa cho "{query}" (CHỈ trả về 12 dòng, không giải thích):"""
        
        # Generate with new API
        response = client.models.generate_content(
            model='gemini-2.5-flash',  # Latest available model (2026)
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.9,
                top_p=0.95,
                top_k=40,
            )
        )
        
        suggestions_text = response.text.strip()
        
        # Parse suggestions
        suggestions = []
        for line in suggestions_text.split('\n'):
            line = line.strip()
            
            # Skip empty lines
            if not line:
                continue
            
            # Remove numbering (1. 2. etc)
            line = re.sub(r'^\d+[\.\)]\s*', '', line)
            # Remove bullet points
            line = re.sub(r'^[-•*]\s*', '', line)
            # Remove quotes
            line = line.strip('"\'')
            # Remove extra whitespace
            line = ' '.join(line.split())
            
            if line and len(line) > 2 and len(line) < 100:
                suggestions.append(line)
        
        logger.info(f"AI generated {len(suggestions)} suggestions for '{query}'")
        
        # If we got less than 8, add variations
        if len(suggestions) < 8:
            variations = [
                f"{query} hot",
                f"{query} trending",
                f"{query} đẹp",
                f"{query} giá rẻ",
                f"{query} chất lượng",
                f"{query} mới nhất",
                f"{query} viral",
                f"{query} nổi bật",
                f"{query} cao cấp",
                f"{query} sang trọng"
            ]
            # Add variations that aren't already in suggestions
            for var in variations:
                if var.lower() not in [s.lower() for s in suggestions]:
                    suggestions.append(var)
                    if len(suggestions) >= 12:
                        break
        
        # Return top 12
        return suggestions[:12]
        
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        
        # Fallback: Generate 10 variations
        return [
            f"{query} hot",
            f"{query} trending",
            f"{query} đẹp",
            f"{query} mới nhất",
            f"{query} viral",
            f"{query} giá rẻ",
            f"{query} chất lượng",
            f"{query} cao cấp",
            f"{query} nổi bật",
            f"{query} sang trọng"
        ]
