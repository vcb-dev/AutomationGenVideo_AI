"""
HTTP-based scraper for Douyin - Không sử dụng browser
Lấy dữ liệu trực tiếp từ URL bằng HTTP requests
"""
import asyncio
import logging
import re
import json
from datetime import datetime
from django.utils import timezone
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class DouyinHttpScraper:
    """Scraper sử dụng HTTP requests thay vì browser"""
    
    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        self.headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.douyin.com/",
            "Origin": "https://www.douyin.com",
        }
    
    def normalize_count(self, count_str):
        """Convert counts like '1.2w', '1.2万', or '1,234' to integers."""
        if not count_str:
            return 0
        count_str = str(count_str).strip().lower()
        count_str = count_str.replace(',', '').replace(' ', '')
        
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
    
    async def search_videos(self, keyword, min_likes=0, min_views=0, target_count=20):
        """
        Search videos bằng HTTP request trực tiếp
        Lưu ý: Douyin là SPA, nên có thể cần parse JavaScript hoặc API calls
        """
        results = []
        
        try:
            # URL encode keyword
            import urllib.parse
            encoded_keyword = urllib.parse.quote(keyword)
            url = f"https://www.douyin.com/search/{encoded_keyword}"
            
            logger.info(f"Fetching URL: {url}")
            
            # Option 1: Thử lấy HTML trực tiếp
            async with httpx.AsyncClient(
                headers=self.headers,
                follow_redirects=True,
                timeout=30.0
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                
                html_content = response.text
                logger.info(f"Received HTML content, length: {len(html_content)}")
                
                # Parse HTML
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # Method 1: Tìm script tags chứa JSON data (SPA thường embed data trong script)
                scripts = soup.find_all('script')
                for script in scripts:
                    if script.string:
                        # Tìm JSON data trong script tags
                        # Douyin có thể embed data như: window.__INITIAL_STATE__ = {...}
                        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', script.string, re.DOTALL)
                        if json_match:
                            try:
                                data = json.loads(json_match.group(1))
                                logger.info("Found __INITIAL_STATE__ data")
                                # Parse data structure từ Douyin
                                results = self._parse_initial_state(data, min_likes, min_views, target_count)
                                if results:
                                    return results
                            except json.JSONDecodeError as e:
                                logger.debug(f"Failed to parse JSON: {e}")
                                continue
                        
                        # Tìm pattern khác: window._SSR_HYDRATED_DATA
                        json_match = re.search(r'window\._SSR_HYDRATED_DATA\s*=\s*({.+?});', script.string, re.DOTALL)
                        if json_match:
                            try:
                                data = json.loads(json_match.group(1))
                                logger.info("Found _SSR_HYDRATED_DATA")
                                results = self._parse_hydrated_data(data, min_likes, min_views, target_count)
                                if results:
                                    return results
                            except json.JSONDecodeError as e:
                                logger.debug(f"Failed to parse JSON: {e}")
                                continue
                
                # Method 2: Parse HTML trực tiếp (nếu có server-side rendering)
                video_elements = soup.find_all(['li', 'div'], attrs={'data-e2e': re.compile(r'scroll-list-item|search-result')})
                
                if video_elements:
                    logger.info(f"Found {len(video_elements)} video elements in HTML")
                    for el in video_elements[:target_count]:
                        video_data = self._parse_html_element(el)
                        if video_data:
                            # Apply filters
                            if video_data.get('likes', 0) >= min_likes and video_data.get('views', 0) >= min_views:
                                results.append(video_data)
                                if len(results) >= target_count:
                                    break
                
                # Method 3: Thử gọi API endpoint trực tiếp (nếu biết endpoint)
                # Douyin có thể có internal API endpoint
                api_results = await self._try_api_endpoint(keyword, min_likes, min_views, target_count)
                if api_results:
                    return api_results
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"Error in HTTP scraper: {e}", exc_info=True)
        
        logger.info(f"HTTP scraper returned {len(results)} videos")
        return results
    
    def _parse_initial_state(self, data, min_likes, min_views, target_count):
        """Parse data từ window.__INITIAL_STATE__"""
        results = []
        try:
            # Navigate through data structure (cần điều chỉnh theo cấu trúc thực tế)
            # Ví dụ: data['search']['videoList'] hoặc tương tự
            if isinstance(data, dict):
                # Tìm video list trong nested structure
                video_list = self._find_video_list(data)
                if video_list:
                    for item in video_list[:target_count]:
                        video = self._format_video_from_api(item)
                        if video and video.get('likes', 0) >= min_likes and video.get('views', 0) >= min_views:
                            results.append(video)
        except Exception as e:
            logger.error(f"Error parsing initial state: {e}")
        return results
    
    def _parse_hydrated_data(self, data, min_likes, min_views, target_count):
        """Parse data từ window._SSR_HYDRATED_DATA"""
        return self._parse_initial_state(data, min_likes, min_views, target_count)
    
    def _find_video_list(self, data, path=None):
        """Recursively find video list in nested dict"""
        if path is None:
            path = []
        
        if isinstance(data, dict):
            # Check common keys
            for key in ['videoList', 'videos', 'items', 'list', 'data', 'aweme_list']:
                if key in data:
                    value = data[key]
                    if isinstance(value, list) and len(value) > 0:
                        # Check if first item looks like video data
                        if isinstance(value[0], dict) and any(k in value[0] for k in ['video_id', 'aweme_id', 'id', 'itemId']):
                            return value
            # Recursive search
            for key, value in data.items():
                result = self._find_video_list(value, path + [key])
                if result:
                    return result
        elif isinstance(data, list):
            if len(data) > 0 and isinstance(data[0], dict):
                if any(k in data[0] for k in ['video_id', 'aweme_id', 'id', 'itemId']):
                    return data
        
        return None
    
    def _format_video_from_api(self, item):
        """Format video data từ API response"""
        try:
            video_id = str(item.get('video_id') or item.get('aweme_id') or item.get('id') or item.get('itemId') or '')
            if not video_id:
                return None
            
            # Extract data với fallbacks
            likes = self.normalize_count(item.get('like_count') or item.get('digg_count') or item.get('statistics', {}).get('like_count', 0))
            views = self.normalize_count(item.get('play_count') or item.get('view_count') or item.get('statistics', {}).get('play_count', 0))
            
            # Thumbnail
            thumbnail = (
                item.get('cover') or 
                item.get('cover_url') or
                item.get('video', {}).get('cover', {}).get('url') or
                item.get('images', [{}])[0].get('url') if item.get('images') else None or
                "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop"
            )
            
            # Caption
            caption = (
                item.get('desc') or 
                item.get('title') or 
                item.get('caption') or
                item.get('share_info', {}).get('share_title') or
                "No title"
            )
            
            # Author
            author = (
                item.get('author', {}).get('nickname') or
                item.get('author', {}).get('name') or
                item.get('user', {}).get('nickname') or
                "Unknown Channel"
            )
            
            # URL
            video_url = (
                item.get('share_url') or
                item.get('url') or
                f"https://www.douyin.com/video/{video_id}"
            )
            
            return {
                'id': video_id,
                'caption': caption,
                'thumbnail': thumbnail,
                'likes': int(likes),
                'views': int(views),
                'channelName': author,
                'url': video_url,
                'status': 'pending',
                'publishedAt': timezone.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error formatting video: {e}")
            return None
    
    def _parse_html_element(self, element):
        """Parse video data từ HTML element"""
        try:
            # Tìm link
            link_el = element.find('a', href=True)
            if not link_el:
                return None
            
            link = link_el['href']
            if not link.startswith('http'):
                link = f"https://www.douyin.com{link}"
            
            # Extract video ID
            video_id_match = re.search(r'video/(\d+)', link)
            if not video_id_match:
                return None
            video_id = video_id_match.group(1)
            
            # Extract likes
            likes_el = element.find(attrs={'data-e2e': 'video-like-count'})
            likes_str = likes_el.get_text() if likes_el else "0"
            likes = self.normalize_count(likes_str)
            
            # Extract views
            views_el = element.find(attrs={'data-e2e': 'video-play-count'})
            views_str = views_el.get_text() if views_el else "0"
            views = self.normalize_count(views_str)
            
            # Extract thumbnail
            img_el = element.find('img')
            thumbnail = img_el.get('src') or img_el.get('data-src') or "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop"
            
            # Extract caption
            caption_el = element.find(attrs={'data-e2e': 'video-desc'})
            caption = caption_el.get_text() if caption_el else "No title"
            
            # Extract author
            author_el = element.find(attrs={'data-e2e': 'video-author'})
            author = author_el.get_text() if author_el else "Unknown"
            
            return {
                'id': video_id,
                'caption': caption,
                'thumbnail': thumbnail,
                'likes': int(likes),
                'views': int(views),
                'channelName': author,
                'url': link,
                'status': 'pending',
                'publishedAt': timezone.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error parsing HTML element: {e}")
            return None
    
    async def _try_api_endpoint(self, keyword, min_likes, min_views, target_count):
        """Thử gọi API endpoint trực tiếp của Douyin (nếu biết)"""
        # Douyin có thể có internal API endpoint
        # Cần reverse engineer để tìm endpoint
        # Ví dụ: https://www.douyin.com/aweme/v1/web/general/search/single/
        try:
            import urllib.parse
            encoded_keyword = urllib.parse.quote(keyword)
            
            # Các endpoint có thể thử (cần verify)
            possible_endpoints = [
                f"https://www.douyin.com/aweme/v1/web/general/search/single/?keyword={encoded_keyword}",
                f"https://www.douyin.com/aweme/v1/web/search/item/?keyword={encoded_keyword}",
            ]
            
            for endpoint in possible_endpoints:
                try:
                    async with httpx.AsyncClient(headers=self.headers, timeout=30.0) as client:
                        response = await client.get(endpoint)
                        if response.status_code == 200:
                            data = response.json()
                            if data.get('status_code') == 0:  # Douyin success code
                                results = self._parse_api_response(data, min_likes, min_views, target_count)
                                if results:
                                    logger.info(f"Successfully fetched from API endpoint: {endpoint}")
                                    return results
                except Exception as e:
                    logger.debug(f"Endpoint {endpoint} failed: {e}")
                    continue
        except Exception as e:
            logger.debug(f"API endpoint method failed: {e}")
        
        return []
    
    def _parse_api_response(self, data, min_likes, min_views, target_count):
        """Parse response từ API endpoint"""
        results = []
        try:
            # Navigate through response structure
            video_list = data.get('data', {}).get('item_list') or data.get('item_list') or []
            for item in video_list[:target_count]:
                video = self._format_video_from_api(item)
                if video and video.get('likes', 0) >= min_likes and video.get('views', 0) >= min_views:
                    results.append(video)
        except Exception as e:
            logger.error(f"Error parsing API response: {e}")
        return results
