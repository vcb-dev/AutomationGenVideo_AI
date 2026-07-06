"""Fix published_at cho video cu bi luu sai (luu thanh thoi gian insert DB).

Doc raw_data.created_time va cap nhat lai published_at dung.

Usage:
    python manage.py fix_published_at
"""

import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from django.core.management.base import BaseCommand
from django.utils import timezone as tz
from video_management.models import OwnedVideoContent


class Command(BaseCommand):
    help = 'Fix published_at cho video cu dang bi luu sai'

    def handle(self, *args, **options):
        videos = OwnedVideoContent.objects.all()
        total = videos.count()
        fixed = 0
        skipped = 0

        self.stdout.write(f'\nChecking {total} videos...\n')

        for v in videos.iterator():
            raw = v.raw_data or {}
            created_time = raw.get('created_time', '')

            if not created_time:
                skipped += 1
                continue

            try:
                correct_dt = datetime.fromisoformat(created_time.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                skipped += 1
                continue

            # Chi fix neu published_at khac voi created_time (sai lenh > 60s)
            if v.published_at and abs((v.published_at - correct_dt).total_seconds()) < 60:
                skipped += 1
                continue

            v.published_at = correct_dt
            v.save(update_fields=['published_at', 'updated_at'])
            fixed += 1

        self.stdout.write(f'\nXong: {fixed} video da fix | {skipped} bo qua (dung hoac khong co data)')
        self.stdout.write(f'Tong: {total} video\n')
