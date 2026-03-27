import json
import logging
from datetime import datetime

from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..services.apify_service import create_scraper
from .facebook_analysis_views import _parse_insights_json, _is_placeholder
from ..models import ChannelAnalysis

logger = logging.getLogger(__name__)


def _safe_int(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def _coerce_platform(p: str) -> str:
    s = (p or '').strip().lower()
    if s in ('tiktok', 'tik_tok'):
        return 'tiktok'
    if s in ('instagram', 'ig'):
        return 'instagram'
    if s in ('facebook', 'fb'):
        return 'facebook'
    if s in ('douyin',):
        return 'douyin'
    if s in ('xiaohongshu', 'xhs'):
        return 'xiaohongshu'
    return s


def _extract_username_from_url(platform: str, raw: str) -> str:
    """
    Accept username OR full profile URL. Normalize to identifier used by scrapers.
    """
    s = (raw or '').strip()
    if not s:
        return ''

    # --- Only treat as URL if it actually looks like one ---
    # A real URL must start with http(s):// or contain a known social domain
    SOCIAL_DOMAINS = ('facebook.com', 'fb.com', 'instagram.com', 'tiktok.com', 'douyin.com', 'xiaohongshu.com')
    is_url = s.startswith('http') or any(d in s for d in SOCIAL_DOMAINS)

    if not is_url:
        # Plain username (may contain dots like nambling.jv or huyk.takumi) — return as-is
        return s.replace('@', '').strip()

    try:
        from urllib.parse import urlparse
        u = s
        if not u.startswith('http'):
            u = 'https://' + u.lstrip('/')
        parsed = urlparse(u)
        parts = [p for p in (parsed.path or '').split('/') if p]

        if platform == 'tiktok':
            if not parts:
                return ''
            first = parts[0]
            return (first[1:] if first.startswith('@') else first).replace('@', '').strip()

        if platform == 'instagram':
            if not parts:
                return ''
            first = parts[0]
            # ignore /reel/... /p/...
            if first in ('reel', 'p'):
                return ''
            return first.replace('@', '').strip()

        if platform == 'facebook':
            if not parts:
                return ''
            return parts[0].strip()

        return (parts[0] if parts else '').strip()
    except Exception:
        return s.replace('@', '').strip()


def _viral_score(p: dict) -> int:
    # Heuristic: shares > comments > likes, plus a bit of views
    likes = _safe_int(p.get('likes'))
    comments = _safe_int(p.get('comments'))
    shares = _safe_int(p.get('shares'))
    views = _safe_int(p.get('views'))
    return likes + comments * 2 + shares * 3 + int(views / 100)


@csrf_exempt
@api_view(['POST'])
def channel_metrics_generic(request):
    """
    POST /api/channel/metrics/
    Body: { platform, username?, url?, max_posts?, start_date?, end_date?, force_refresh? }
    """
    platform = _coerce_platform(request.data.get('platform'))
    max_posts = min(_safe_int(request.data.get('max_posts') or 30) or 30, 100)
    max_posts = min(max(max_posts, 10), 500)
    force_refresh = str(request.data.get('force_refresh', 'false')).lower() == 'true'
    start_date_str = (request.data.get('start_date') or '').strip()
    end_date_str = (request.data.get('end_date') or '').strip()

    # Parse date range
    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
        if end_date_str:
            end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except ValueError:
        pass

    raw_user = request.data.get('username') or request.data.get('url') or ''
    username = _extract_username_from_url(platform, raw_user)
    if not platform or not username:
        return Response({'success': False, 'error': 'platform and username/url are required'}, status=status.HTTP_400_BAD_REQUEST)

    if not force_refresh:
        try:
            cached = ChannelAnalysis.objects.get(platform=platform, username=username)
            if cached.metrics:
                meta = cached.metrics.get('meta', {})
                cached_start = meta.get('start_date') or ''
                cached_end = meta.get('end_date') or ''
                # Always return cached data if force_refresh is false (when user clicks "Xem")
                # Even if dates don't match perfectly, they want to view the old data.
                logger.info(f"Returning Db CACHED metrics for {platform}:{username} (Stored dates: {cached_start} - {cached_end}, Requested: {start_date_str} - {end_date_str})")
                return Response({
                    'success': True,
                    'metrics': cached.metrics,
                    'scanned_count': meta.get('scanned_count', meta.get('posts_analyzed', 0))
                }, status=status.HTTP_200_OK)
        except ChannelAnalysis.DoesNotExist:
            pass

    try:
        scraper = create_scraper(platform)
        # Pass until_date=None: let Apify fetch max_posts*3 raw items, Python date-filter below does the slicing.
        # Passing start_date_str as until_date incorrectly uses it as an API cutoff for platforms that support it.
        raw_results = list(scraper.get_user_videos(username, max_results=max_posts * 3, until_date=None) or [])

        # Detect Apify-level errors (items with only error keys)
        apify_error_items = [r for r in raw_results if set(r.keys()) <= {'error', 'errorDescription', 'requestedUrl'}]
        raw_results = [r for r in raw_results if r not in apify_error_items]

        if not raw_results and apify_error_items:
            # Extract the most descriptive error message from Apify
            logger.warning(f"Apify error item for {platform}/{username}: {apify_error_items[0]}")
            apify_msg = (apify_error_items[0].get('errorDescription') or apify_error_items[0].get('error') or '').strip()
            friendly = f'Không thể lấy bài đăng từ @{username} trên {platform.upper()}.'
            if apify_msg:
                friendly += f' Lý do: {apify_msg}'
            else:
                friendly += ' Tài khoản có thể bị ẩn, đặt chế độ riêng tư, hoặc username không tồn tại.'
            return Response({'success': False, 'error': friendly}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        normalized = [scraper.normalize_video_data(v) for v in raw_results]
        normalized = [p for p in normalized if p]

        # Filter by date range
        if start_dt or end_dt:
            filtered = []
            for p in normalized:
                dt = p.get('published_at')
                if dt is None:
                    continue
                if hasattr(dt, 'replace'):
                    pdt = dt.replace(tzinfo=None) if hasattr(dt, 'tzinfo') else dt
                else:
                    try:
                        pdt = datetime.fromisoformat(str(dt).replace('Z', ''))
                    except Exception:
                        continue
                if start_dt and pdt < start_dt:
                    continue
                if end_dt and pdt > end_dt:
                    continue
                filtered.append(p)
            normalized = filtered[:max_posts]
        else:
            normalized = normalized[:max_posts]

        scanned_count = len(normalized)

        if scanned_count == 0:
            date_hint = ''
            if start_date_str or end_date_str:
                date_hint = f' trong khoảng {start_date_str or ""} — {end_date_str or ""}'
            return Response(
                {'success': False, 'error': f'Không có bài đăng nào{date_hint} cho @{username}. Thử thu hẹp bộ lọc ngày hoặc tăng số lượng bài.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        enriched = []
        for p in normalized:
            ts = None
            dt = p.get('published_at')
            if dt:
                try:
                    ts = int(dt.timestamp())
                except Exception:
                    ts = None
            enriched.append(
                {
                    'id': str(p.get('video_id') or ''),
                    'url': p.get('video_url') or '',
                    'text': (p.get('description') or p.get('title') or '')[:1200],
                    'timestamp': ts or 0,
                    'likes': _safe_int(p.get('likes_count')),
                    'comments': _safe_int(p.get('comments_count')),
                    'shares': _safe_int(p.get('shares_count')),
                    'views': _safe_int(p.get('views_count')),
                    'is_video': True,
                    'format': 'video',
                }
            )

        for p in enriched:
            p['viral_score'] = _viral_score(p)

        top_viral = sorted(enriched, key=lambda x: x.get('viral_score') or 0, reverse=True)[:10]

        by_date = {}
        for p in enriched:
            ts = p.get('timestamp') or 0
            if not ts:
                continue
            d = datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d')
            by_date.setdefault(d, []).append(p)

        if by_date:
            min_date = min(by_date.keys())
            max_date = max(by_date.keys())
            from datetime import timedelta
            curr = datetime.strptime(min_date, '%Y-%m-%d')
            end_obj = datetime.strptime(max_date, '%Y-%m-%d')
            while curr <= end_obj:
                d_str = curr.strftime('%Y-%m-%d')
                if d_str not in by_date:
                    by_date[d_str] = []
                curr += timedelta(days=1)

        posting_frequency = [{'date': d, 'count': len(lst)} for d, lst in sorted(by_date.items())]
        avg_engagement_by_day = []
        for d, lst in sorted(by_date.items()):
            n = max(1, len(lst))
            avg_engagement_by_day.append(
                {
                    'date': d,
                    'posts': len(lst),
                    'avgLikes': sum(p['likes'] for p in lst) / n,
                    'avgComments': sum(p['comments'] for p in lst) / n,
                    'avgShares': sum(p['shares'] for p in lst) / n,
                    'avgViews': sum(p.get('views') or 0 for p in lst) / n,
                }
            )

        # Hashtag A1-A5 stats: scan post text/description for content-line hashtags
        CONTENT_HASHTAGS = ['a1', 'a2', 'a3', 'a4', 'a5']
        hashtag_stats = []
        for tag in CONTENT_HASHTAGS:
            count = sum(
                1 for p in enriched
                if any(pat in (' ' + (p.get('text') or '') + ' ').lower() for pat in [f'#{tag}', f' {tag} '])
            )
            hashtag_stats.append({'hashtag': tag.upper(), 'count': count})
        hashtag_stats.sort(key=lambda x: x['count'], reverse=True)

        metrics_data = {
            'top_viral_posts': top_viral,
            'ad_posts': [],
            'hashtag_stats': hashtag_stats,
            'charts': {
                'avg_engagement_by_day': avg_engagement_by_day,
                'format_distribution': [{'format': 'video', 'count': len(enriched)}],
                'posting_frequency': posting_frequency,
                'ad_format_distribution': [],
            },
            'meta': {'posts_analyzed': len(enriched), 'scanned_count': scanned_count, 'ad_posts_found': 0},
        }

        metrics_data['meta']['start_date'] = start_date_str
        metrics_data['meta']['end_date'] = end_date_str
        metrics_data['meta']['scanned_count'] = scanned_count

        # Always cache the latest result for this platform+username
        obj, _ = ChannelAnalysis.objects.get_or_create(platform=platform, username=username)
        obj.metrics = metrics_data
        obj.max_posts = max_posts
        obj.save(update_fields=['metrics', 'max_posts', 'updated_at'])

        return Response(
            {
                'success': True,
                'metrics': metrics_data,
                'scanned_count': scanned_count,
            },
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.error(f"❌ Generic channel metrics failed: {str(e)}", exc_info=True)
        return Response({'success': False, 'error': f'Failed to compute channel metrics: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@csrf_exempt
@api_view(['POST'])
def channel_insights_generic(request):
    """
    POST /api/channel/insights/
    Body: { platform, username?, url?, max_posts?, start_date?, end_date?, language?, force_refresh? }
    """
    platform = _coerce_platform(request.data.get('platform'))
    language = (request.data.get('language') or 'vi').strip() or 'vi'
    max_posts = min(_safe_int(request.data.get('max_posts') or 30) or 30, 100)
    max_posts = min(max(max_posts, 10), 500)
    force_refresh = str(request.data.get('force_refresh', 'false')).lower() == 'true'
    start_date_str = (request.data.get('start_date') or '').strip()
    end_date_str = (request.data.get('end_date') or '').strip()

    # Parse date range
    start_dt = None
    end_dt = None
    try:
        if start_date_str:
            start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
        if end_date_str:
            end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
    except ValueError:
        pass

    raw_user = request.data.get('username') or request.data.get('url') or ''
    username = _extract_username_from_url(platform, raw_user)
    if not platform or not username:
        return Response({'success': False, 'error': 'platform and username/url are required'}, status=status.HTTP_400_BAD_REQUEST)

    if not force_refresh:
        try:
            cached = ChannelAnalysis.objects.get(platform=platform, username=username)
            if cached.insights:
                meta = cached.insights.get('meta', {})
                cached_start = meta.get('start_date') or ''
                cached_end = meta.get('end_date') or ''
                # Always return cached data if force_refresh is false
                logger.info(f"Returning Db CACHED insights for {platform}:{username} (Stored dates: {cached_start} - {cached_end}, Requested: {start_date_str} - {end_date_str})")
                return Response({
                    'success': True,
                    'insights': cached.insights,
                    'scanned_count': meta.get('scanned_count', 0)
                }, status=status.HTTP_200_OK)
        except ChannelAnalysis.DoesNotExist:
            pass

    try:
        scraper = create_scraper(platform)
        # Pass until_date=None: let Apify fetch max_posts*3 raw items, Python date-filter below does the slicing.
        raw_results = list(scraper.get_user_videos(username, max_results=max_posts * 3, until_date=None) or [])

        # Detect Apify-level errors (items with only error keys)
        apify_error_items = [r for r in raw_results if set(r.keys()) <= {'error', 'errorDescription', 'requestedUrl'}]
        raw_results = [r for r in raw_results if r not in apify_error_items]

        if not raw_results and apify_error_items:
            apify_msg = (apify_error_items[0].get('errorDescription') or apify_error_items[0].get('error') or '').strip()
            friendly = f'Không thể lấy bài đăng từ @{username} trên {platform.upper()}.'
            if apify_msg:
                friendly += f' Lý do: {apify_msg}'
            else:
                friendly += ' Tài khoản có thể bị ẩn, đặt chế độ riêng tư, hoặc username không tồn tại.'
            return Response({'success': False, 'error': friendly}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        normalized = [scraper.normalize_video_data(v) for v in raw_results]
        normalized = [p for p in normalized if p]

        # Filter by date range
        if start_dt or end_dt:
            filtered = []
            for p in normalized:
                dt = p.get('published_at')
                if dt is None:
                    continue
                if hasattr(dt, 'replace'):
                    pdt = dt.replace(tzinfo=None) if hasattr(dt, 'tzinfo') else dt
                else:
                    try:
                        pdt = datetime.fromisoformat(str(dt).replace('Z', ''))
                    except Exception:
                        continue
                if start_dt and pdt < start_dt:
                    continue
                if end_dt and pdt > end_dt:
                    continue
                filtered.append(p)
            normalized = filtered[:max_posts]
        else:
            normalized = normalized[:max_posts]

        scanned_count = len(normalized)

        if scanned_count == 0:
            date_hint = ''
            if start_date_str or end_date_str:
                date_hint = f' trong khoảng {start_date_str or ""} — {end_date_str or ""}'
            return Response(
                {'success': False, 'error': f'Không có bài đăng nào{date_hint} cho @{username}. Thử thu hẹp bộ lọc ngày hoặc tăng số lượng bài.'},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        compact_posts = []
        for p in normalized[:30]:
            ts = None
            dt = p.get('published_at')
            if dt:
                try:
                    ts = int(dt.timestamp())
                except Exception:
                    ts = None
            compact_posts.append(
                {
                    'id': str(p.get('video_id') or ''),
                    'url': p.get('video_url') or '',
                    'text': (p.get('description') or p.get('title') or '')[:800],
                    'timestamp': ts or 0,
                    'likes': _safe_int(p.get('likes_count')),
                    'comments': _safe_int(p.get('comments_count')),
                    'shares': _safe_int(p.get('shares_count')),
                    'views': _safe_int(p.get('views_count')),
                    'is_video': True,
                    'format': 'video',
                    'is_ad': False,
                }
            )

        # summary
        top_by_eng = sorted(compact_posts, key=lambda x: (x.get('likes', 0) + x.get('comments', 0) * 2 + x.get('shares', 0) * 3), reverse=True)[:3]
        context = {
            'platform': platform,
            'username': username,
            'channel_summary': {
                'total_posts_analyzed': len(compact_posts),
                'scanned_count': scanned_count,
                'date_range': {'start': start_date_str or None, 'end': end_date_str or None},
                'video_posts': len(compact_posts),
                'text_posts': 0,
                'top_3_engaged': [{'text': p['text'][:300], 'likes': p['likes'], 'comments': p['comments']} for p in top_by_eng],
            },
            'posts': compact_posts,
        }

        import google.genai as genai
        from google.genai import types

        api_key = getattr(settings, 'GEMINI_API_KEY', '')
        if not api_key:
            return Response({'success': False, 'error': 'GEMINI_API_KEY is not configured'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        client = genai.Client(api_key=api_key)
        model_name = getattr(settings, 'GEMINI_MODEL', 'gemini-2.5-flash')

        section_keys = [
            'Định vị Thương hiệu',
            'Giọng nói Thương hiệu',
            'Khách hàng Mục tiêu',
            'Tuyến Nội dung',
            'Công thức Nội dung',
            'Phân tích Reel',
            'Chiến lược Quảng cáo',
            'Phễu Marketing',
            'Tương tác & Bình luận',
            'Tóm tắt Chiến lược',
            'Điểm mạnh',
            'Điểm yếu & Cơ hội',
            'Đề xuất hành động',
        ]

        insights_schema = {
            "type": "object",
            "properties": {k: {"type": "string"} for k in section_keys},
            "required": section_keys,
            "additionalProperties": False,
        }

        prompt = f"""
Bạn là chuyên gia marketing phân tích kênh đối thủ trên nền tảng {platform.upper()}.

QUY TRÌNH (bắt buộc):
1. HIỂU KÊnh: Đọc kỹ JSON dữ liệu kênh (bài đăng/video + likes/comments/shares/views). Nắm rõ: kênh làm gì, phục vụ ai, giọng văn, loại nội dung chủ đạo, bài nào tương tác cao.
2. PHÂN TÍCH CỤ THỂ: Viết phân tích RIÊNG CHO KÊnh NÀY. Mỗi mục phải có ví dụ/số liệu cụ thể nếu có.

YÊU CẦU ĐẦU RA:
- Viết bằng tiếng {language}.
- CHỈ trả về JSON hợp lệ (không markdown). Đúng keys:
{json.dumps({k: "string" for k in section_keys}, ensure_ascii=False)}
- Mỗi mục viết 3-5 gạch đầu dòng chi tiết (bắt đầu bằng dấu "- "). Mạch lạc, TRỌN VẸN CÂU, TUYỆT ĐỐI KHÔNG BỊ CẮT CHỮ GIỮA CHỪNG.
- CỤ THỂ HOÁ: Bắt buộc lấy ví dụ và dẫn chứng từ dữ liệu JSON cung cấp.
- Nếu thiếu dữ liệu thật sự: ghi "Chưa đủ dữ liệu để đánh giá".
- Không bịa số.

DỮ LIỆU KÊnh (JSON):
{json.dumps(context, ensure_ascii=False)}
""".strip()

        def _call(temp: float):
            return client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temp,
                    top_p=0.9,
                    top_k=40,
                    max_output_tokens=8192,
                    response_mime_type="application/json",
                    response_json_schema=insights_schema,
                ),
            )

        response = _call(0.6)
        text = (response.text or '').strip()
        insights = _parse_insights_json(text, section_keys)

        placeholder_count = sum(1 for v in insights.values() if _is_placeholder(v))
        if (not text) or placeholder_count >= max(6, int(len(section_keys) * 0.5)):
            response2 = _call(0.2)
            text2 = (response2.text or '').strip()
            insights2 = _parse_insights_json(text2, section_keys)
            placeholder_count2 = sum(1 for v in insights2.values() if _is_placeholder(v))
            if text2 and placeholder_count2 < placeholder_count:
                text, insights = text2, insights2

        all_placeholder = all(_is_placeholder(v) for v in insights.values())
        if all_placeholder or not text:
            return Response(
                {
                    'success': False,
                    'error': 'AI không tạo được phân tích (phản hồi rỗng hoặc không đúng định dạng).',
                    'code': 'EMPTY_RESPONSE',
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Always cache the latest result for this platform+username
        obj, _ = ChannelAnalysis.objects.get_or_create(platform=platform, username=username)
        insights['meta'] = {
            'start_date': start_date_str,
            'end_date': end_date_str,
            'scanned_count': scanned_count
        }
        obj.insights = insights
        obj.max_posts = max_posts
        obj.save(update_fields=['insights', 'max_posts', 'updated_at'])

        return Response({'success': True, 'insights': insights, 'insights_text': text, 'scanned_count': scanned_count}, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"❌ Generic channel insights failed: {str(e)}", exc_info=True)
        return Response({'success': False, 'error': f'Failed to generate insights: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
