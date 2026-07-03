"""Debug: Xem Facebook Graph API tra ve gi cho video cua 1 page."""
import json
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from django.core.management.base import BaseCommand
from video_management.models import ManagedFacebookPage
from video_management.services.facebook_graph_service import FacebookGraphService


class Command(BaseCommand):
    help = 'Debug: In raw JSON từ Facebook Graph API cho 1 page'

    def handle(self, *args, **options):
        page = ManagedFacebookPage.objects.filter(is_active=True).first()
        if not page:
            self.stdout.write(self.style.ERROR('Không có page nào'))
            return

        token = page.get_decrypted_token()
        self.stdout.write(f'\n=== Page: {page.name} (ID: {page.page_id}) ===\n')

        graph = FacebookGraphService()
        graph.access_token = token

        # Test 1: Dùng /feed + attachments{media{source}}
        self.stdout.write(self.style.HTTP_INFO('─── TEST 1: /feed + attachments{media{source}} ───'))
        import requests
        url = f"{graph.BASE_URL}/{page.page_id}/feed"
        params = {
            'fields': 'id,message,attachments{type,media_type,media{source,image},subattachments{type,media_type,media{source,image}}}',
            'limit': 5,
            'access_token': token,
        }
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        for post in data.get('data', [])[:3]:
            post_id = post.get('id', '?')
            atts = post.get('attachments', {}).get('data', [])
            self.stdout.write(f'\n  Post {post_id}:')
            if not atts:
                self.stdout.write('    attachments: EMPTY')
                continue
            for att in atts:
                self.stdout.write(f'    type={att.get("type")} media_type={att.get("media_type")}')
                media = att.get('media', {})
                self.stdout.write(f'    media keys: {list(media.keys())}')
                self.stdout.write(f'    media.source: {(media.get("source") or "NONE")[:100]}')
                image = media.get('image', {})
                self.stdout.write(f'    media.image.src: {(image.get("src") or "NONE")[:80]}')

        # Test 2: Dùng /{page_id}/videos endpoint
        self.stdout.write(self.style.HTTP_INFO('\n─── TEST 2: /{page_id}/videos (endpoint chuyên video) ───'))
        url2 = f"{graph.BASE_URL}/{page.page_id}/videos"
        params2 = {
            'fields': 'id,title,description,source,picture,length,created_time,permalink_url',
            'limit': 3,
            'access_token': token,
        }
        r2 = requests.get(url2, params=params2, timeout=30)
        data2 = r2.json()
        if 'error' in data2:
            self.stdout.write(self.style.ERROR(f'  Error: {data2["error"].get("message")}'))
        else:
            for v in data2.get('data', [])[:3]:
                self.stdout.write(f'\n  Video {v.get("id")}:')
                self.stdout.write(f'    title: {(v.get("title") or v.get("description", ""))[:80]}')
                self.stdout.write(f'    source: {(v.get("source") or "NONE")[:120]}')
                self.stdout.write(f'    picture: {(v.get("picture") or "NONE")[:80]}')
                self.stdout.write(f'    length: {v.get("length", "?")}s')

        # Test 3: Lấy 1 video ID từ feed → gọi trực tiếp /{video_id}?fields=source
        self.stdout.write(self.style.HTTP_INFO('\n─── TEST 3: /{video_id}?fields=source (per-video lookup) ───'))
        feed_posts = data.get('data', [])
        video_post_id = None
        for p in feed_posts:
            for att in p.get('attachments', {}).get('data', []):
                if att.get('type', '').startswith('video') or att.get('media_type') == 'video':
                    video_post_id = p.get('id')
                    break
            if video_post_id:
                break

        if video_post_id:
            url3 = f"{graph.BASE_URL}/{video_post_id}"
            params3 = {
                'fields': 'id,source,permalink_url',
                'access_token': token,
            }
            r3 = requests.get(url3, params=params3, timeout=15)
            data3 = r3.json()
            self.stdout.write(f'  Post ID: {video_post_id}')
            self.stdout.write(f'  source: {(data3.get("source") or "NONE")[:120]}')
            self.stdout.write(f'  keys: {list(data3.keys())}')
        else:
            self.stdout.write('  Không tìm thấy video post nào trong feed')

        self.stdout.write(self.style.SUCCESS('\n=== Debug hoàn tất ==='))
