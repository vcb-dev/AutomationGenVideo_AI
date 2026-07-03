"""GD3: Cap nhat metrics (views/likes/comments/shares) cho video gan day.

Usage:
    python manage.py refresh_metrics
    python manage.py refresh_metrics --days 14
"""

import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from django.core.management.base import BaseCommand
from video_management.services.facebook_fetcher import FacebookFetcher


class Command(BaseCommand):
    help = 'GD3: Cap nhat metrics cho video dang trong N ngay gan day (mac dinh 7 ngay)'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7)

    def handle(self, *args, **options):
        days = options['days']

        self.stdout.write(f'\n{"="*60}')
        self.stdout.write(f'  REFRESH METRICS: video trong {days} ngay gan day')
        self.stdout.write(f'{"="*60}\n')

        try:
            result = FacebookFetcher().refresh_recent_metrics(days=days)
            updated = result.get('updated', 0)
            total = result.get('total', 0)
            self.stdout.write(f'\n{"="*60}')
            self.stdout.write(f'  Xong: {updated}/{total} video da cap nhat metrics')
            self.stdout.write(f'{"="*60}')
        except Exception as e:
            self.stdout.write(f'LOI: {str(e)[:200]}')
