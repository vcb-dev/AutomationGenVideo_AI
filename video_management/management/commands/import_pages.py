"""Buoc 0: Import danh sach Pages chinh chu tu Facebook vao DB.

Day la buoc dau tien khi deploy. Goi API /me/accounts de lay tat ca
Fanpage ma tai khoan Facebook dang quan ly, luu vao bang ManagedFacebookPage.

Usage:
    python manage.py import_pages
"""

import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from django.core.management.base import BaseCommand
from video_management.services.facebook_fetcher import FacebookFetcher
from video_management.models import ManagedFacebookPage


class Command(BaseCommand):
    help = 'Buoc 0: Import danh sach Pages chinh chu tu Facebook (goi /me/accounts)'

    def handle(self, *args, **options):
        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  IMPORT PAGES tu Facebook')
        self.stdout.write(f'{"="*60}\n')

        fetcher = FacebookFetcher()

        try:
            result = fetcher.import_my_managed_pages()
        except Exception as e:
            self.stdout.write(f'LOI: {e}')
            return

        created = result.get('created', 0)
        updated = result.get('updated', 0)

        self.stdout.write(f'  Them moi : {created} page(s)')
        self.stdout.write(f'  Cap nhat : {updated} page(s)')

        # Hien thi danh sach pages trong DB
        pages = ManagedFacebookPage.objects.filter(is_active=True)
        self.stdout.write(f'\n  Tong pages trong DB: {pages.count()}')
        for p in pages:
            status = 'BACKFILLED' if p.is_backfilled else 'CHUA BACKFILL'
            videos = p.videos.count()
            self.stdout.write(f'    - {p.name} (ID: {p.page_id}) [{status}] [{videos} video]')

        self.stdout.write(f'\n{"="*60}')

        if not ManagedFacebookPage.objects.filter(is_backfilled=False, is_active=True).exists():
            self.stdout.write('Tat ca pages da backfill. Chay: python manage.py sync_all_pages')
        else:
            self.stdout.write('Buoc tiep: python manage.py backfill_pages')

        self.stdout.write(f'{"="*60}')
