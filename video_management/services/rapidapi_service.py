"""
Tikhub TikTok Service - Thay thế Playwright scraper
Sử dụng Tikhub API để lấy dữ liệu từ TikTok/Douyin
Theo tài liệu: https://docs.tikhub.io/370212779e0
"""
import requests
import logging
import re
import json
import urllib.parse
from typing import List, Dict, Optional
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

class TikhubService:
    """Service để gọi Tikhub TikTok API"""
    
    def __init__(self):
        # Sử dụng Tikhub API
        self.api_key = getattr(settings, 'TIKHUB_API_KEY', '')
        self.base_url = getattr(settings, 'TIKHUB_API_BASE_URL', 'https://api.tikhub.io')
        
        if not self.api_key:
            logger.warning("TIKHUB_API_KEY not configured in settings")
        
        # Tikhub API sử dụng Bearer token authentication
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
    
    def normalize_count(self, count_str):
        """
        Convert counts like '1.2w', '1.2万', '1,234', or integer to integers.
        Handles both string and numeric inputs.
        """
        if count_str is None:
            return 0
        
        # If already a number
        if isinstance(count_str, (int, float)):
            return int(count_str)
        
        # Convert to string and process
        count_str = str(count_str).strip().lower()
        if not count_str or count_str == '0':
            return 0
        
        # Remove commas and whitespace
        count_str = count_str.replace(',', '').replace(' ', '')
        
        # Match number and suffix (supporting w, k, m and Chinese 万)
        match = re.search(r'(\d+\.?\d*)([wmk万]?)', count_str)
        if not match:
            return 0
            
        num = float(match.group(1))
        suffix = match.group(2)
        
        if suffix in ['w', '万']:
            return int(num * 10000)
        elif suffix == 'k':
            return int(num * 1000)
        elif suffix == 'm':
            return int(num * 1000000)
        return int(num)
    
    def get_music_posts(self, music_id: str, count: int = 30, cursor: int = 0, 
                       min_likes: int = 0, min_views: int = 0) -> List[Dict]:
        """
        Lấy posts theo musicId sử dụng SociaVault API
        
        Args:
            music_id: Music ID (ví dụ: '7224128604890990593')
            count: Số lượng posts muốn lấy (mặc định: 30)
            cursor: Pagination cursor (mặc định: 0)
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            
        Returns:
            List of formatted video dictionaries
        """
        if not self.api_key:
            logger.error("TIKHUB_API_KEY not configured")
            return []
        
        try:
            # Tikhub có thể có endpoint khác cho music posts
            # Tạm thời sử dụng search với music_id như keyword
            logger.warning("Tikhub may not have dedicated music posts endpoint, using search instead")
            return self.search_videos(keyword=music_id, min_likes=min_likes, min_views=min_views, target_count=count)
            
        except Exception as e:
            logger.error(f"Tikhub get_music_posts error: {e}", exc_info=True)
            return []
    
    def search_videos(self, keyword: str, min_likes: int = 0, min_views: int = 0, 
                     target_count: int = 30, offset: int = 0) -> Dict:
        """
        Search videos using Tikhub TikTok API
        
        Args:
            keyword: Search keyword (có thể là keyword hoặc user_id)
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            target_count: Target number of videos to return
            offset: Pagination offset
            
        Returns:
            Dict với keys:
            - videos: List of formatted video dictionaries
            - cursor: Cursor mới cho pagination (nếu có)
        """
        if not self.api_key:
            logger.error("TIKTOK_API_KEY not configured")
            return []
        
        results = []
        current_offset = offset
        max_requests = 10  # Limit số lần request để tránh rate limit
        final_cursor = offset  # Cursor cuối cùng từ response
        
        try:
            for request_num in range(max_requests):
                if len(results) >= target_count:
                    break
                
                # Sử dụng Tikhub API để search videos
                # Endpoint chính thức: /api/v1/douyin/search/fetch_video_search_v1
                # Theo tài liệu: https://docs.tikhub.io/370212779e0
                endpoint_url = f"{self.base_url}/api/v1/douyin/search/fetch_video_search_v1"
                
                # Tikhub API yêu cầu POST với JSON body (theo documentation)
                # Lưu search_id và backtrace để dùng cho pagination
                search_id = getattr(self, '_last_search_id', '')
                backtrace = getattr(self, '_last_backtrace', '')
                
                # Lần đầu tiên: cursor = 0, search_id = '', backtrace = ''
                if current_offset == 0:
                    search_id = ''
                    backtrace = ''
                
                success = False
                try:
                    # Tạo payload JSON theo format Tikhub API (theo documentation)
                    payload = {
                        'keyword': keyword,
                        'cursor': current_offset,
                        'sort_type': '0',  # 0 = 综合排序, 1 = 最多点赞, 2 = 最新发布
                        'publish_time': '0',  # 0 = 不限, 1 = 最近一天, 7 = 最近一周, 180 = 最近半年
                        'filter_duration': '0',  # 0 = 不限, 0-1 = 一分钟以内, 1-5 = 一到五分钟, 5-10000 = 五分钟以上
                        'content_type': '0',  # 0 = 不限, 1 = 视频, 2 = 图片, 3 = 文章
                        'search_id': search_id,  # Từ response trước đó
                        'backtrace': backtrace  # Từ response trước đó
                    }
                        
                    logger.info(f"Tikhub API POST request {request_num + 1}: {endpoint_url}")
                    logger.info(f"Payload: keyword='{keyword}', cursor={current_offset}, search_id='{search_id[:20] if search_id else ''}...'")
                    
                    # Gửi POST request với JSON body
                    response = requests.post(
                        endpoint_url,
                        headers=self.headers,
                        json=payload,  # POST với JSON body
                        timeout=10.0
                    )
                    
                    logger.info(f"Response status: {response.status_code}, URL: {endpoint_url}")
                    
                    # Handle specific status codes
                    if response.status_code == 401:
                        logger.error("Tikhub: Authentication failed - Invalid API key")
                        continue  # Thử request tiếp theo
                    
                    elif response.status_code == 402:
                        logger.error("Tikhub: Payment Required - Insufficient balance")
                        raise Exception("Insufficient balance. Please add credits to your Tikhub account.")
                    
                    elif response.status_code == 403:
                        logger.warning(f"Tikhub: Forbidden - may not have access")
                        continue  # Thử request tiếp theo
                    
                    elif response.status_code == 429:
                        logger.error("Tikhub: Rate limit exceeded")
                        raise Exception("Rate limit exceeded. Please wait a moment and try again.")
                    
                    elif response.status_code == 404:
                        logger.warning(f"Endpoint {endpoint_url} not found (404). Response body: {response.text[:500]}")
                        continue  # Thử request tiếp theo
                    
                    elif response.status_code == 204:
                        logger.warning(f"Tikhub returned 204 No Content")
                        continue  # Thử request tiếp theo
                    
                    elif response.status_code != 200:
                        logger.warning(f"Tikhub error {response.status_code}: {response.text[:200]}")
                        continue  # Thử request tiếp theo
                        
                    # Parse JSON response
                    # Theo tài liệu: response có structure: {code, data, ...}
                    # data có thể là JSON string hoặc object
                    try:
                        response_data = response.json()
                        logger.info(f"Tikhub response type: {type(response_data)}")
                        if isinstance(response_data, dict):
                            logger.info(f"Tikhub response keys: {list(response_data.keys())}")
                            
                            # Theo tài liệu: data field có thể là JSON string
                            data_field = response_data.get('data')
                            if isinstance(data_field, str):
                                # Parse JSON string
                                data = json.loads(data_field)
                                logger.info(f"Parsed data from JSON string, keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
                            elif isinstance(data_field, dict):
                                data = data_field
                            else:
                                data = response_data
                            
                            # Log structure để debug
                            logger.info(f"Tikhub data structure: {str(data)[:1000]}")
                        else:
                            data = response_data
                    except ValueError as json_error:
                        logger.warning(f"Failed to parse JSON: {json_error}, response: {response.text[:500]}")
                        continue  # Thử request tiếp theo
                    except json.JSONDecodeError as json_error:
                        logger.warning(f"Failed to parse data JSON string: {json_error}")
                        continue
                    
                    # Handle different response formats from Tikhub API
                    # Theo tài liệu: data.data[] chứa danh sách items, mỗi item có aweme_info
                    videos = self._extract_videos_from_response(data)
                    logger.info(f"Extracted {len(videos)} videos from API response")
                        
                    if not videos:
                        logger.warning(f"No videos extracted. Response: {str(data)[:500]}")
                        continue  # Thử request tiếp theo
                    
                    # Log mẫu dữ liệu video đầu tiên để debug
                    if len(videos) > 0:
                        logger.info(f"Mẫu dữ liệu video đầu tiên: {str(videos[0])[:500]}")
                        logger.info(f"Keys trong video đầu tiên: {list(videos[0].keys()) if isinstance(videos[0], dict) else 'Not a dict'}")
                    
                    # Filter and format videos
                    formatted_count = 0
                    skipped_count = 0
                    for idx, video_data in enumerate(videos):
                        formatted_video = self._format_video(video_data)
                        
                        if not formatted_video:
                            skipped_count += 1
                            if idx == 0:  # Log lý do skip video đầu tiên
                                logger.warning(f"Failed to format first video. Video data: {str(video_data)[:300]}")
                            continue
                        
                        formatted_count += 1
                        
                        # Apply filters
                        if formatted_video.get('likes', 0) >= min_likes and formatted_video.get('views', 0) >= min_views:
                            results.append(formatted_video)
                            
                            if len(results) >= target_count:
                                break
                    
                    logger.info(f"Formatted {formatted_count} videos, skipped {skipped_count} videos, {len(results)} passed filters")
                    
                    # Nếu có kết quả, đánh dấu thành công
                    if len(results) > 0:
                        success = True
                        logger.info(f"Successfully got {len(results)} videos from Tikhub endpoint: {endpoint_url}")
                        
                        # Update cursor, search_id, backtrace cho pagination tiếp theo
                        # Theo tài liệu: data.cursor, data.has_more, data.search_id, data.backtrace
                        if isinstance(data, dict):
                            # Lấy cursor từ response
                            response_cursor = data.get('cursor')
                            if response_cursor is not None:
                                current_offset = response_cursor
                                final_cursor = response_cursor
                                logger.info(f"Updated cursor from response: {final_cursor}")
                            
                            # Lấy search_id và backtrace cho pagination tiếp theo
                            response_search_id = data.get('search_id', '')
                            response_backtrace = data.get('backtrace', '')
                            if response_search_id:
                                self._last_search_id = response_search_id
                                logger.info(f"Updated search_id: {response_search_id[:20]}...")
                            if response_backtrace:
                                self._last_backtrace = response_backtrace
                                logger.info(f"Updated backtrace: {response_backtrace[:20]}...")
                            
                            # Check has_more flag
                            has_more = data.get('has_more', 0)
                            if has_more == 0:  # 0 = không còn data
                                logger.info("No more videos available (has_more=0)")
                                break
                            
                            # Nếu không có cursor, tăng offset
                            if response_cursor is None:
                                current_offset += len(videos) if videos else 20
                                final_cursor = current_offset
                        else:
                            # Fallback: tăng offset
                            current_offset += len(videos) if videos else 20
                            final_cursor = current_offset
                        
                        # If we got fewer videos than requested, we've reached the end
                        if len(videos) < 20:
                            logger.info("Reached end of available videos")
                            break
                        
                        break  # Thành công
                        
                except requests.exceptions.Timeout:
                    logger.warning(f"Tikhub request timed out for {endpoint_url}")
                    break  # Timeout, không thử tiếp
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Tikhub request error for {endpoint_url}: {e}")
                    break  # Request error, không thử tiếp
                except Exception as e:
                    # Nếu là rate limit hoặc payment required, không thử tiếp
                    if "Rate limit" in str(e) or "Insufficient balance" in str(e):
                        raise
                    logger.warning(f"Tikhub processing error for {endpoint_url}: {e}")
                    break  # Processing error, không thử tiếp
                
                if success:
                    break  # Đã thành công
                
                if not success:
                    logger.warning(f"Tikhub API request {request_num + 1} failed")
                    break  # Không thành công, không thử tiếp
                
                
                # If we got fewer videos than requested, we've reached the end
                if len(results) < target_count and request_num == 0:
                    logger.info("Reached end of available videos")
                    break
            
            logger.info(f"Tikhub search completed. Found {len(results)} videos after filtering, cursor: {final_cursor}")
            
            # Trả về dict với videos và cursor
            return {
                'videos': results[:target_count] if results else [],
                'cursor': final_cursor  # Trả về cursor mới nhất từ response
            }
            
        except Exception as e:
            logger.error(f"Tikhub search error: {e}", exc_info=True)
            return {
                'videos': [],
                'cursor': offset
            }
    
    def _extract_videos_from_response(self, data: Dict) -> List[Dict]:
        """
        Extract video list from Tikhub API response
        Theo tài liệu: https://docs.tikhub.io/370212779e0
        Response structure: data.data[] - danh sách items
        Mỗi item có: type, aweme_info
        Quan trọng: Filter các item có aweme_info = null
        """
        videos = []
        
        logger.debug(f"Extracting videos from response type: {type(data)}")
        
        # Try different possible response structures
        if isinstance(data, dict):
            logger.debug(f"Response is dict with keys: {list(data.keys())}")
            
            # Theo tài liệu Tikhub: data.data[] chứa danh sách items
            if 'data' in data:
                data_content = data['data']
                logger.debug(f"Found 'data' key, type: {type(data_content)}")
                
                if isinstance(data_content, list):
                    # data['data'] là list trực tiếp (danh sách items)
                    raw_items = data_content
                    logger.debug(f"data['data'] is a list with {len(raw_items)} items")
                elif isinstance(data_content, dict):
                    logger.debug(f"data['data'] is dict with keys: {list(data_content.keys())}")
                    # Có thể có nested structure: data.data.data[] hoặc data.data.items[]
                    raw_items = (
                        data_content.get('data', []) or  # Nested data.data.data[]
                        data_content.get('items', []) or  # data.data.items[]
                        data_content.get('aweme_list', []) or  # Fallback
                        data_content.get('videos', []) or  # Fallback
                        data_content.get('list', []) or  # Fallback
                        []
                    )
                else:
                    raw_items = []
                
                # Theo tài liệu: mỗi item có type và aweme_info
                # Filter: Bỏ qua các item có aweme_info = null
                for item in raw_items:
                    if isinstance(item, dict):
                        # Tikhub format: item có aweme_info field
                        if 'aweme_info' in item:
                            # Nếu có aweme_info và không null, lấy aweme_info
                            aweme_info = item.get('aweme_info')
                            if aweme_info is not None and isinstance(aweme_info, dict):
                                videos.append(aweme_info)
                            # Nếu aweme_info = null, bỏ qua (filter)
                        else:
                            # Nếu không có aweme_info, có thể item chính là video data (aweme_info)
                            videos.append(item)
                    else:
                        videos.append(item)
                
                if videos:
                    logger.debug(f"Found {len(videos)} videos in data['data'] after filtering null aweme_info (Tikhub format)")
            elif 'result' in data:
                result_content = data['result']
                logger.debug(f"Found 'result' key, type: {type(result_content)}")
                if isinstance(result_content, list):
                    videos = result_content
                elif isinstance(result_content, dict):
                    logger.debug(f"result is dict with keys: {list(result_content.keys())}")
                    videos = (
                        result_content.get('videos', []) or
                        result_content.get('items', []) or
                        result_content.get('list', []) or
                        result_content.get('aweme_list', []) or
                        result_content.get('itemList', []) or
                        []
                    )
            elif 'body' in data:
                body_content = data['body']
                logger.debug(f"Found 'body' key, type: {type(body_content)}")
                if isinstance(body_content, dict):
                    videos = (
                        body_content.get('itemList', []) or
                        body_content.get('videos', []) or
                        body_content.get('items', []) or
                        []
                    )
            elif 'videos' in data:
                videos = data['videos']
                logger.debug(f"Found 'videos' key directly: {len(videos)} items")
            elif 'items' in data:
                videos = data['items']
                logger.debug(f"Found 'items' key directly: {len(videos)} items")
            elif 'list' in data:
                videos = data['list']
                logger.debug(f"Found 'list' key directly: {len(videos)} items")
            elif 'aweme_list' in data:
                videos = data['aweme_list']
                logger.debug(f"Found 'aweme_list' key directly: {len(videos)} items")
            elif 'itemList' in data:
                videos = data['itemList']
                logger.debug(f"Found 'itemList' key directly: {len(videos)} items")
            elif 'data' in data and isinstance(data['data'], list):
                # API mới có thể trả về data trực tiếp là list
                videos = data['data']
                logger.debug(f"Found 'data' as list directly: {len(videos)} items")
        elif isinstance(data, list):
            videos = data
            logger.debug(f"Response is directly a list with {len(videos)} items")
        
        logger.info(f"Extracted {len(videos)} videos from API response")
        if len(videos) > 0 and isinstance(videos[0], dict):
            logger.debug(f"First video keys: {list(videos[0].keys())}")
        
        return videos if isinstance(videos, list) else []
    
    def _format_video(self, video_data: Dict) -> Optional[Dict]:
        """
        Format video data from Tikhub API response to frontend format
        Xử lý nested JSON structure theo mapping map của Tikhub:
        - aweme_info.statistics.digg_count (số tim)
        - aweme_info.statistics.play_count (số view)
        - aweme_info.video.play_addr.url_list[0] (download_url)
        - aweme_info.video.cover.url_list[0] (ảnh bìa)
        - aweme_info.create_time (thời gian, cần nhân 1000)
        - aweme_info.author.nickname và author.avatar_thumb.url_list[0]
        """
        try:
            if not isinstance(video_data, dict):
                logger.warning(f"Video data is not a dict: {type(video_data)}")
                return None
            
            # Safe access helper function
            def safe_get(data, *keys, default=None):
                """Safely access nested dictionary keys"""
                try:
                    result = data
                    for key in keys:
                        if isinstance(result, dict):
                            result = result.get(key)
                        elif isinstance(result, list) and isinstance(key, int):
                            result = result[key] if 0 <= key < len(result) else None
                        else:
                            return default
                        if result is None:
                            return default
                    return result
                except (KeyError, IndexError, TypeError, AttributeError):
                    return default
            
            # Extract video ID - aweme_id (chuyển thành string để tránh BigInt issues)
            video_id = safe_get(video_data, 'aweme_id') or safe_get(video_data, 'id') or ''
            video_id = str(video_id) if video_id else ''
            
            if not video_id or video_id == 'None' or video_id == '':
                logger.debug(f"No video ID found. Available keys: {list(video_data.keys())}")
                return None
            
            # Extract statistics - Tikhub format: aweme_info.statistics.digg_count và play_count
            statistics = safe_get(video_data, 'statistics', default={})
            likes = self.normalize_count(safe_get(statistics, 'digg_count', default=0))
            views = self.normalize_count(safe_get(statistics, 'play_count', default=0))
            
            # Extract thumbnail - Tikhub format: aweme_info.video.cover.url_list[0]
            video_obj = safe_get(video_data, 'video', default={})
            cover_obj = safe_get(video_obj, 'cover', default={})
            cover_url_list = safe_get(cover_obj, 'url_list', default=[])
            thumbnail = cover_url_list[0] if isinstance(cover_url_list, list) and len(cover_url_list) > 0 else None
            
            # Fallback: thử origin_cover
            if not thumbnail:
                origin_cover = safe_get(video_obj, 'origin_cover', default={})
                origin_url_list = safe_get(origin_cover, 'url_list', default=[])
                thumbnail = origin_url_list[0] if isinstance(origin_url_list, list) and len(origin_url_list) > 0 else None
            
            # Proxy thumbnail nếu từ domain p3.douyinpic.com (tránh 403)
            if thumbnail and 'p3.douyinpic.com' in thumbnail:
                thumbnail = f"/api/proxy/image?url={urllib.parse.quote(thumbnail)}"
            elif not thumbnail:
                thumbnail = "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop"  # Fallback
            
            # Extract caption - Tikhub format: aweme_info.desc
            caption = safe_get(video_data, 'desc', default='') or safe_get(video_data, 'title', default='') or "No title"
            
            # Extract video URL - Tikhub format: https://www.douyin.com/video/{aweme_id}
            video_url = f"https://www.douyin.com/video/{video_id}"
            
            # Extract download URL - Tikhub format: aweme_info.video.play_addr.url_list[0]
            play_addr = safe_get(video_obj, 'play_addr', default={})
            play_url_list = safe_get(play_addr, 'url_list', default=[])
            download_url = play_url_list[0] if isinstance(play_url_list, list) and len(play_url_list) > 0 else None
            
            # Proxy download_url nếu từ domain p3.douyinpic.com (tránh 403)
            if download_url and 'p3.douyinpic.com' in download_url:
                download_url = f"/api/proxy/video?url={urllib.parse.quote(download_url)}"
            
            # Extract author info - Tikhub format: aweme_info.author.nickname và avatar_thumb.url_list[0]
            author_obj = safe_get(video_data, 'author', default={})
            channel_name = safe_get(author_obj, 'nickname', default='') or safe_get(author_obj, 'unique_id', default='') or "Unknown Channel"
            
            avatar_thumb = safe_get(author_obj, 'avatar_thumb', default={})
            avatar_url_list = safe_get(avatar_thumb, 'url_list', default=[])
            author_avatar = avatar_url_list[0] if isinstance(avatar_url_list, list) and len(avatar_url_list) > 0 else None
            
            # Extract created_at - Tikhub format: aweme_info.create_time (cần nhân 1000)
            published_at = timezone.now().isoformat()
            try:
                create_time = safe_get(video_data, 'create_time')
                if create_time:
                    from datetime import datetime
                    if isinstance(create_time, (int, float)):
                        # Tikhub trả về seconds, cần nhân 1000 để convert sang milliseconds
                        if create_time < 1e10:  # Nếu là seconds (nhỏ hơn 1e10)
                            create_time = create_time * 1000  # Nhân 1000 như yêu cầu
                        dt = datetime.fromtimestamp(create_time / 1000, tz=timezone.utc)
                        published_at = dt.isoformat()
            except Exception as time_error:
                logger.debug(f"Error parsing create_time: {time_error}")
            
            formatted_result = {
                'id': str(video_id),
                'title': str(caption),  # Thêm field title
                'caption': str(caption),  # Giữ caption cho tương thích
                'cover': str(thumbnail),  # Thêm field cover
                'thumbnail': str(thumbnail),  # Giữ thumbnail cho tương thích
                'video_url': str(video_url),  # Thêm field video_url
                'url': str(video_url),  # Giữ url cho tương thích
                'download_url': str(download_url) if download_url else '',  # Thêm field download_url
                'created_at': str(published_at),  # Thêm field created_at
                'publishedAt': str(published_at),  # Giữ publishedAt cho tương thích
                'likes': int(likes),
                'views': int(views),
                'stats': {  # Thêm field stats
                    'digg_count': int(likes),
                    'play_count': int(views)
                },
                'channelName': str(channel_name),
                'author': {  # Thêm field author
                    'nickname': str(channel_name),
                    'avatar': str(author_avatar) if author_avatar else ''
                },
                'status': 'completed'
            }
            
            logger.debug(f"Formatted video: id={formatted_result['id']}, likes={formatted_result['likes']}, views={formatted_result['views']}")
            return formatted_result
            
        except Exception as e:
            logger.error(f"Error formatting video: {e}", exc_info=True)
            logger.debug(f"Video data that failed: {str(video_data)[:300]}")
            return None
