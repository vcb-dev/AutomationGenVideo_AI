"""GD2: Delta sync bai moi cho tat ca pages da backfill.

Usage:
    python manage.py sync_all_pages
    python manage.py sync_all_pages --max-posts 20
    python manage.py sync_all_pages --page-id 1136265599576987
"""

import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from django.core.management.base import BaseCommand
from django.utils import timezone
from video_management.models import ManagedFacebookPage
from video_management.services.facebook_fetcher import FacebookFetcher


class Command(BaseCommand):
    help = 'GD2 Delta Sync: cao 10 bai moi nhat cho cac page da backfill'

    def add_arguments(self, parser):
        parser.add_argument('--max-posts', type=int, default=10)
        parser.add_argument('--delay', type=int, default=3)
        parser.add_argument('--page-id', type=str, default=None)

    def handle(self, *args, **options):
        max_posts = options['max_posts']
        delay = options['delay']
        target = options['page_id']

        if target:
            pages = ManagedFacebookPage.objects.filter(page_id=target)
        else:
            pages = ManagedFacebookPage.objects.filter(
                is_active=True, is_backfilled=True
            ).order_by('last_scraped_at')

        total = pages.count()
        if total == 0:
            self.stdout.write('Khong co page nao. (Chay backfill_pages truoc neu chua backfill)')
            return

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  DELTA SYNC: {total} page(s) | max_posts={max_posts}')
        self.stdout.write(f'{"="*60}')

        fetcher = FacebookFetcher()
        done = 0
        failed = 0
        total_created = 0

        for i, page in enumerate(pages, 1):
            self.stdout.write(f'\n[{i}/{total}] {page.name} (ID: {page.page_id})')
            try:
                result = fetcher.sync_page(page_id=page.page_id, max_posts=max_posts)
                created = result.get('created', 0)
                updated = result.get('updated', 0)
                total_created += created
                done += 1

                page.last_scraped_at = timezone.now()
                page.scrape_error = None
                page.save(update_fields=['last_scraped_at', 'scrape_error', 'updated_at'])

                self.stdout.write(f'  -> +{created} moi, ~{updated} cap nhat')
            except Exception as e:
                failed += 1
                page.scrape_error = str(e)[:500]
                page.save(update_fields=['scrape_error', 'updated_at'])
                self.stdout.write(f'  -> LOI: {str(e)[:120]}')

            if i < total:
                time.sleep(delay)

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  Xong: {done}/{total} OK | {failed} loi | +{total_created} video moi')
        self.stdout.write(f'{"="*60}')
