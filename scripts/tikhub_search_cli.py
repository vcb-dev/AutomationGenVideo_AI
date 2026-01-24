#!/usr/bin/env python3
"""
Tikhub Search CLI Tool
Script độc lập để search videos từ Tikhub API và output kết quả
Có thể build thành file exe bằng PyInstaller
"""
import sys
import os
import json
import argparse
from datetime import datetime
from typing import Dict, List, Optional

# Thêm path để import TikhubService
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TikhubServiceCLI:
    """Tikhub Service không phụ thuộc Django"""
    
    def __init__(self, api_key: str = None, base_url: str = 'https://api.tikhub.io'):
        """
        Initialize Tikhub Service
        
        Args:
            api_key: Tikhub API key (nếu None, sẽ lấy từ env hoặc default)
            base_url: Tikhub API base URL
        """
        self.api_key = api_key or os.getenv('TIKHUB_API_KEY', 'o8sDUc0vwFT1knKZR6XEkU3pCjSga4Jz7QXEumm+Z+21gQm4OWOgLR2ZbA==')
        self.base_url = base_url
        
        if not self.api_key:
            raise ValueError("TIKHUB_API_KEY is required. Set it as environment variable or pass as argument.")
        
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
    
    def normalize_count(self, count_str):
        """Normalize count string to integer"""
        if not count_str:
            return 0
        if isinstance(count_str, (int, float)):
            return int(count_str)
        if isinstance(count_str, str):
            # Remove commas and other non-numeric chars except decimal point
            count_str = count_str.replace(',', '').replace(' ', '')
            # Handle K, M suffixes
            if 'K' in count_str.upper():
                return int(float(count_str.upper().replace('K', '')) * 1000)
            elif 'M' in count_str.upper():
                return int(float(count_str.upper().replace('M', '')) * 1000000)
            try:
                return int(float(count_str))
            except ValueError:
                return 0
        return 0
    
    def search_videos(self, keyword: str, min_likes: int = 0, min_views: int = 0, 
                     target_count: int = 20, offset: int = 0) -> Dict:
        """
        Search videos using Tikhub API
        
        Args:
            keyword: Search keyword
            min_likes: Minimum likes filter
            min_views: Minimum views filter
            target_count: Target number of videos to return
            offset: Pagination offset (cursor)
            
        Returns:
            Dict với keys: videos, cursor
        """
        results = []
        current_offset = offset
        final_cursor = offset
        search_id = ''
        backtrace = ''
        
        try:
            endpoint_url = f"{self.base_url}/api/v1/douyin/search/fetch_video_search_v1"
            
            # Lần đầu tiên: cursor = 0, search_id = '', backtrace = ''
            if current_offset == 0:
                search_id = ''
                backtrace = ''
            
            # Tạo payload JSON theo format Tikhub API
            payload = {
                'keyword': keyword,
                'cursor': current_offset,
                'sort_type': '0',
                'publish_time': '0',
                'filter_duration': '0',
                'content_type': '0',
                'search_id': search_id,
                'backtrace': backtrace
            }
            
            logger.info(f"Searching for keyword: '{keyword}'...")
            
            # Gửi POST request
            response = requests.post(
                endpoint_url,
                headers=self.headers,
                json=payload,
                timeout=30.0
            )
            
            logger.info(f"Response status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"API error {response.status_code}: {response.text[:200]}")
                return {'videos': [], 'cursor': offset}
            
            # Parse JSON response
            response_data = response.json()
            
            # Parse data field (có thể là JSON string)
            data_field = response_data.get('data')
            if isinstance(data_field, str):
                import json
                data = json.loads(data_field)
            elif isinstance(data_field, dict):
                data = data_field
            else:
                data = response_data
            
            # Extract videos
            videos = self._extract_videos_from_response(data)
            logger.info(f"Extracted {len(videos)} videos from API response")
            
            if not videos:
                logger.warning("No videos found in response")
                return {'videos': [], 'cursor': offset}
            
            # Format videos
            formatted_count = 0
            for video_data in videos:
                formatted_video = self._format_video(video_data)
                if not formatted_video:
                    continue
                
                # Apply filters
                if formatted_video.get('likes', 0) >= min_likes and formatted_video.get('views', 0) >= min_views:
                    results.append(formatted_video)
                    if len(results) >= target_count:
                        break
            
            # Update cursor
            if isinstance(data, dict):
                response_cursor = data.get('cursor')
                if response_cursor is not None:
                    final_cursor = response_cursor
            
            logger.info(f"Found {len(results)} videos after filtering")
            
            return {
                'videos': results[:target_count] if results else [],
                'cursor': final_cursor
            }
            
        except Exception as e:
            logger.error(f"Search error: {e}", exc_info=True)
            return {'videos': [], 'cursor': offset}
    
    def _extract_videos_from_response(self, data: Dict) -> List[Dict]:
        """Extract videos from API response"""
        videos = []
        
        if isinstance(data, dict) and 'data' in data:
            data_content = data['data']
            
            if isinstance(data_content, list):
                raw_items = data_content
            elif isinstance(data_content, dict):
                raw_items = (
                    data_content.get('data', []) or
                    data_content.get('items', []) or
                    []
                )
            else:
                raw_items = []
            
            # Extract aweme_info from items
            for item in raw_items:
                if isinstance(item, dict):
                    if 'aweme_info' in item:
                        aweme_info = item.get('aweme_info')
                        if aweme_info is not None and isinstance(aweme_info, dict):
                            videos.append(aweme_info)
                    else:
                        videos.append(item)
        
        return videos
    
    def _format_video(self, video_data: Dict) -> Optional[Dict]:
        """Format video data"""
        try:
            if not isinstance(video_data, dict):
                return None
            
            def safe_get(data, *keys, default=None):
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
            
            # Extract video ID
            video_id = safe_get(video_data, 'aweme_id') or safe_get(video_data, 'id') or ''
            video_id = str(video_id) if video_id else ''
            
            if not video_id or video_id == 'None':
                return None
            
            # Extract statistics
            statistics = safe_get(video_data, 'statistics', default={})
            likes = self.normalize_count(safe_get(statistics, 'digg_count', default=0))
            views = self.normalize_count(safe_get(statistics, 'play_count', default=0))
            
            # Extract thumbnail
            video_obj = safe_get(video_data, 'video', default={})
            cover_obj = safe_get(video_obj, 'cover', default={})
            cover_url_list = safe_get(cover_obj, 'url_list', default=[])
            thumbnail = cover_url_list[0] if isinstance(cover_url_list, list) and len(cover_url_list) > 0 else None
            
            if not thumbnail:
                origin_cover = safe_get(video_obj, 'origin_cover', default={})
                origin_url_list = safe_get(origin_cover, 'url_list', default=[])
                thumbnail = origin_url_list[0] if isinstance(origin_url_list, list) and len(origin_url_list) > 0 else None
            
            if not thumbnail:
                thumbnail = "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop"
            
            # Extract caption
            caption = safe_get(video_data, 'desc', default='') or safe_get(video_data, 'title', default='') or "No title"
            
            # Extract video URL
            video_url = f"https://www.douyin.com/video/{video_id}"
            
            # Extract download URL
            play_addr = safe_get(video_obj, 'play_addr', default={})
            play_url_list = safe_get(play_addr, 'url_list', default=[])
            download_url = play_url_list[0] if isinstance(play_url_list, list) and len(play_url_list) > 0 else None
            
            # Extract author info
            author_obj = safe_get(video_data, 'author', default={})
            channel_name = safe_get(author_obj, 'nickname', default='') or "Unknown Channel"
            
            avatar_thumb = safe_get(author_obj, 'avatar_thumb', default={})
            avatar_url_list = safe_get(avatar_thumb, 'url_list', default=[])
            author_avatar = avatar_url_list[0] if isinstance(avatar_url_list, list) and len(avatar_url_list) > 0 else None
            
            # Extract created_at
            published_at = datetime.now().isoformat()
            try:
                create_time = safe_get(video_data, 'create_time')
                if create_time:
                    if isinstance(create_time, (int, float)):
                        if create_time < 1e10:
                            create_time = create_time * 1000
                        dt = datetime.fromtimestamp(create_time / 1000)
                        published_at = dt.isoformat()
            except Exception:
                pass
            
            return {
                'id': str(video_id),
                'title': str(caption),
                'caption': str(caption),
                'cover': str(thumbnail),
                'thumbnail': str(thumbnail),
                'video_url': str(video_url),
                'url': str(video_url),
                'download_url': str(download_url) if download_url else '',
                'created_at': str(published_at),
                'publishedAt': str(published_at),
                'likes': int(likes),
                'views': int(views),
                'channelName': str(channel_name),
                'author': {
                    'nickname': str(channel_name),
                    'avatar': str(author_avatar) if author_avatar else ''
                },
                'status': 'completed'
            }
            
        except Exception as e:
            logger.error(f"Error formatting video: {e}")
            return None


def main():
    """Main CLI function"""
    parser = argparse.ArgumentParser(
        description='Search Douyin/TikTok videos using Tikhub API',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tikhub_search_cli.py "花" --output results.json
  python tikhub_search_cli.py "美食" --min-likes 1000 --min-views 10000 --count 20
  python tikhub_search_cli.py "vàng bạc" --output output.json --pretty
        """
    )
    
    parser.add_argument('keyword', help='Search keyword')
    parser.add_argument('--api-key', help='Tikhub API key (or set TIKHUB_API_KEY env var)')
    parser.add_argument('--min-likes', type=int, default=0, help='Minimum likes filter')
    parser.add_argument('--min-views', type=int, default=0, help='Minimum views filter')
    parser.add_argument('--count', type=int, default=20, help='Number of videos to return (default: 20)')
    parser.add_argument('--output', '-o', help='Output JSON file path (default: print to stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty print JSON output')
    
    args = parser.parse_args()
    
    try:
        # Initialize service
        service = TikhubServiceCLI(api_key=args.api_key)
        
        # Search videos
        result = service.search_videos(
            keyword=args.keyword,
            min_likes=args.min_likes,
            min_views=args.min_views,
            target_count=args.count
        )
        
        # Prepare output
        output_data = {
            'keyword': args.keyword,
            'filters': {
                'min_likes': args.min_likes,
                'min_views': args.min_views
            },
            'total_found': len(result.get('videos', [])),
            'cursor': result.get('cursor', 0),
            'videos': result.get('videos', []),
            'timestamp': datetime.now().isoformat()
        }
        
        # Output
        json_str = json.dumps(output_data, indent=2 if args.pretty else None, ensure_ascii=False)
        
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(json_str)
            print(f"✓ Results saved to {args.output}")
            print(f"✓ Found {output_data['total_found']} videos")
        else:
            print(json_str)
        
        return 0
        
    except KeyboardInterrupt:
        print("\n✗ Interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
