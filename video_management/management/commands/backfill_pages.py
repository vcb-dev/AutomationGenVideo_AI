"""GD1: Cao sau lich su cho cac page chua backfill.

Usage:
    python manage.py backfill_pages
    python manage.py backfill_pages --max-total 500
    python manage.py backfill_pages --page-id 1136265599576987
"""

import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from django.core.management.base import BaseCommand
from video_management.models import ManagedFacebookPage
from video_management.services.facebook_fetcher import FacebookFetcher


class Command(BaseCommand):
    help = 'GD1 Backfill: cao sau lich su (300-500 bai) cho cac page chua backfill'

    def add_arguments(self, parser):
        parser.add_argument('--max-total', type=int, default=300)
        parser.add_argument('--delay', type=int, default=10)
        parser.add_argument('--page-id', type=str, default=None)

    def handle(self, *args, **options):
        max_total = options['max_total']
        delay = options['delay']
        target = options['page_id']

        if target:
            pages = ManagedFacebookPage.objects.filter(page_id=target, is_active=True)
        else:
            pages = ManagedFacebookPage.objects.filter(
                is_active=True, is_backfilled=False
            )

        total = pages.count()
        if total == 0:
            self.stdout.write('Tat ca pages da duoc backfill (hoac khong co page nao).')
            return

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  BACKFILL: {total} page(s) | max_total={max_total} bai/page')
        self.stdout.write(f'{"="*60}')

        fetcher = FacebookFetcher()
        done = 0
        failed = 0

        for i, page in enumerate(pages, 1):
            self.stdout.write(f'\n[{i}/{total}] {page.name} (ID: {page.page_id})')
            try:
                result = fetcher.backfill_page(page.page_id, max_total=max_total)
                created = result.get('created', 0)
                scanned = result.get('total_scanned', 0)
                done += 1
                self.stdout.write(f'  -> +{created} video moi (quet {scanned} bai)')
            except Exception as e:
                failed += 1
                self.stdout.write(f'  -> LOI: {str(e)[:120]}')

            if i < total:
                self.stdout.write(f'  -> Nghi {delay}s...')
                time.sleep(delay)

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  Xong: {done}/{total} OK | {failed} loi')
        self.stdout.write(f'{"="*60}')
