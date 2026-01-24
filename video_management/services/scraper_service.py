import asyncio
import logging
import re
from datetime import datetime
from django.utils import timezone
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    logger.warning("playwright-stealth not installed, continuing without stealth mode")

class DouyinScraper:
    def __init__(self, headless=True):
        self.headless = headless
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


    def normalize_count(self, count_str):
        """Convert counts like '1.2w', '1.2万', or '1,234' to integers."""
        if not count_str:
            return 0
        count_str = count_str.strip().lower()
        
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

    async def _get_browser_context(self, playwright):
        browser = await playwright.chromium.launch(headless=self.headless)
        context = await browser.new_context(
            user_agent=self.user_agent,
            viewport={'width': 1280, 'height': 800}
        )
        return browser, context

    async def search_videos(self, keyword, min_likes=0, min_views=0, target_count=20):
        """Scrapes and filters Douyin search results."""
        results = []
        async with async_playwright() as p:
            browser, context = await self._get_browser_context(p)
            page = await context.new_page()
            try:
                if HAS_STEALTH:
                    await stealth_async(page)
            except Exception as e:
                logger.warning(f"Stealth mode failed: {e}, continuing without it")
            
            try:
                # Dynamic URL Generation - URL encode keyword for Chinese characters
                import urllib.parse
                encoded_keyword = urllib.parse.quote(keyword)
                url = f"https://www.douyin.com/search/{encoded_keyword}"
                logger.info(f"Navigating to: {url}")
                # Increased timeout to 120 seconds for slow connections
                try:
                    await page.goto(url, wait_until="networkidle", timeout=120000)
                except Exception as goto_error:
                    logger.warning(f"Page goto with networkidle failed: {goto_error}, trying with domcontentloaded")
                    # Fallback to domcontentloaded if networkidle times out
                    await page.goto(url, wait_until="domcontentloaded", timeout=120000)
                
                # Wait a bit for content to load (Douyin may need more time)
                await asyncio.sleep(5)
                logger.info("Waiting for page content to load...")
                
                # Try multiple selectors for video items (Douyin may change structure)
                video_elements = []
                possible_selectors = [
                    'li[data-e2e="scroll-list-item"]',
                    'div[data-e2e="search-result-item"]',
                    '.video-item',
                    '[class*="video-card"]',
                    '[class*="search-item"]'
                ]
                
                for selector in possible_selectors:
                    try:
                        video_elements = await page.query_selector_all(selector)
                        if video_elements:
                            logger.info(f"Found {len(video_elements)} elements with selector: {selector}")
                            break
                    except Exception as e:
                        logger.debug(f"Selector {selector} failed: {e}")
                        continue
                
                if not video_elements:
                    logger.warning("No video elements found with any selector. Page might have different structure.")
                    # Try to get page content for debugging
                    page_title = await page.title()
                    page_url = page.url
                    logger.warning(f"Page title: {page_title}")
                    logger.warning(f"Page URL: {page_url}")
                    
                    # Try to take a screenshot for debugging
                    try:
                        await page.screenshot(path=f"/tmp/douyin_search_{keyword[:10]}.png", full_page=True)
                        logger.info(f"Screenshot saved to /tmp/douyin_search_{keyword[:10]}.png")
                    except Exception as e:
                        logger.debug(f"Could not take screenshot: {e}")
                    
                    # Return empty results instead of continuing
                    logger.warning("Returning empty results due to no elements found")
                    return []
                
                # Auto-scroll logic
                last_height = await page.evaluate("document.body.scrollHeight")
                scroll_attempts = 0
                max_scroll_attempts = 10
                
                logger.info(f"Starting to scrape. Found {len(video_elements)} initial elements. Target: {target_count} videos")
                
                while len(results) < target_count and scroll_attempts < max_scroll_attempts:
                    # Re-query elements after scroll
                    for selector in possible_selectors:
                        try:
                            video_elements = await page.query_selector_all(selector)
                            if video_elements:
                                break
                        except:
                            continue
                    
                    for el in video_elements:
                        try:
                            # 1. Video URL & ID - Try multiple ways to find link
                            link = ""
                            link_selectors = ['a', 'a[href*="video"]', '[href*="video"]']
                            for link_selector in link_selectors:
                                try:
                                    link_el = await el.query_selector(link_selector)
                                    if link_el:
                                        link = await link_el.get_attribute('href') or ""
                                        if link:
                                            break
                                except:
                                    continue
                            
                            if not link:
                                logger.debug("No link found for element, skipping")
                                continue
                                
                            if not link.startswith('http'):
                                link = f"https://www.douyin.com{link}"
                            
                            # Try multiple patterns for video ID
                            video_id = None
                            video_id_patterns = [
                                r'video/(\d+)',
                                r'/video/(\d+)',
                                r'aweme_id=(\d+)',
                                r'item_id=(\d+)'
                            ]
                            for pattern in video_id_patterns:
                                match = re.search(pattern, link)
                                if match:
                                    video_id = match.group(1)
                                    break
                            
                            if not video_id:
                                logger.debug(f"Could not extract video ID from link: {link}")
                                continue
                            
                            if any(r.get('id') == str(video_id) for r in results):
                                continue

                            # 2. Likes (Heart Count)
                            likes_el = await el.query_selector('span[data-e2e="video-like-count"]')
                            likes_str = await likes_el.inner_text() if likes_el else "0"
                            likes = self.normalize_count(likes_str)
                            
                            # Filter by likes early
                            if likes < min_likes:
                                continue

                            # 3. View Count - Try multiple selectors
                            views = 0
                            # Try common selectors for view count
                            view_selectors = [
                                'span[data-e2e="video-play-count"]',
                                '.video-attr-item:nth-child(2)',
                                'span:has-text("播放")',
                                '[class*="play"]',
                                '[class*="view"]'
                            ]
                            for selector in view_selectors:
                                try:
                                    views_el = await el.query_selector(selector)
                                    if views_el:
                                        views_str = await views_el.inner_text()
                                        views = self.normalize_count(views_str)
                                        if views > 0:
                                            break
                                except:
                                    continue
                            
                            if views < min_views:
                                continue

                            # 4. Thumbnail - Try multiple sources
                            thumbnail = ""
                            img_selectors = ['img', 'img[data-e2e="video-cover"]', '.video-cover img']
                            for selector in img_selectors:
                                try:
                                    img_el = await el.query_selector(selector)
                                    if img_el:
                                        thumbnail = await img_el.get_attribute('src') or await img_el.get_attribute('data-src') or ""
                                        if thumbnail and thumbnail.startswith('http'):
                                            break
                                except:
                                    continue
                            
                            # Fallback thumbnail if not found
                            if not thumbnail or not thumbnail.startswith('http'):
                                thumbnail = "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=120&h=80&fit=crop"

                            # 5. Caption
                            title_el = await el.query_selector('p[data-e2e="video-desc"]')
                            caption = await title_el.inner_text() if title_el else ""

                            # 6. Channel & Followers
                            author = "Unknown"
                            author_selectors = [
                                'p[data-e2e="video-author"]',
                                '.author-name',
                                '[class*="author"]',
                                '[class*="user"]'
                            ]
                            for selector in author_selectors:
                                try:
                                    author_el = await el.query_selector(selector)
                                    if author_el:
                                        author = await author_el.inner_text()
                                        if author and author.strip():
                                            break
                                except:
                                    continue
                            
                            # 7. Published Date - Try to extract from page or use current time
                            published_at = timezone.now().isoformat()
                            try:
                                # Try to find time element
                                time_selectors = [
                                    '[data-e2e="video-time"]',
                                    '.publish-time',
                                    '[class*="time"]'
                                ]
                                for selector in time_selectors:
                                    try:
                                        time_el = await el.query_selector(selector)
                                        if time_el:
                                            time_str = await time_el.inner_text()
                                            # Try to parse relative time like "2小时前", "3天前"
                                            # For now, use current time as fallback
                                            break
                                    except:
                                        continue
                            except:
                                pass

                            results.append({
                                'id': str(video_id),
                                'caption': caption or "No title",
                                'thumbnail': thumbnail,
                                'likes': int(likes),
                                'views': int(views),
                                'channelName': author,
                                'url': link,
                                'status': 'pending',
                                'publishedAt': published_at
                            })
                            
                            if len(results) >= target_count:
                                break
                                
                        except Exception as e:
                            logger.error(f"Error parsing item: {e}")
                            continue

                    if len(results) >= target_count:
                        break
                        
                    # Scroll down
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)  # Increased sleep time for content to load
                    
                    new_height = await page.evaluate("document.body.scrollHeight")
                    if new_height == last_height:
                        logger.info(f"Reached end of page. Found {len(results)} videos so far.")
                        break
                    last_height = new_height
                    scroll_attempts += 1

                logger.info(f"Scraping completed. Total videos found: {len(results)}")
                return results
            except Exception as e:
                logger.error(f"Scraper error: {e}", exc_info=True)
                # Return whatever results we have so far
                return results
            finally:
                try:
                    await browser.close()
                except:
                    pass


    async def scrape_channel_videos(self, channel_id, target_count=10):
        """Scrapes latest videos from a specific channel profile."""
        results = []
        async with async_playwright() as p:
            browser, context = await self._get_browser_context(p)
            page = await context.new_page()
            try:
                if HAS_STEALTH:
                    await stealth_async(page)
            except Exception as e:
                logger.warning(f"Stealth mode failed: {e}, continuing without it")
            
            try:
                # Douyin profile URLs vary, usually https://www.douyin.com/user/[id]
                url = f"https://www.douyin.com/user/{channel_id}"
                await page.goto(url, wait_until="networkidle")
                
                # Extract videos
                video_elements = await page.query_selector_all('li[data-e2e="user-post-list"]')
                
                for el in video_elements:
                    try:
                        link_el = await el.query_selector('a')
                        link = await link_el.get_attribute('href') if link_el else ""
                        if link and not link.startswith('http'):
                            link = f"https://www.douyin.com{link}"
                            
                        likes_el = await el.query_selector('span[data-e2e="video-like-count"]')
                        likes_str = await likes_el.inner_text() if likes_el else "0"
                        likes = self.normalize_count(likes_str)
                        
                        video_id = re.search(r'video/(\d+)', link)
                        video_id = video_id.group(1) if video_id else None
                        
                        if video_id:
                            results.append({
                                'video_id': video_id,
                                'share_url': link,
                                'like_count': likes
                            })
                    except Exception as e:
                        logger.error(f"Error parsing channel item: {e}")
                        continue
                        
                return results[:target_count]
            except Exception as e:
                logger.error(f"Channel scraper error: {e}")
                return []
            finally:
                await browser.close()
