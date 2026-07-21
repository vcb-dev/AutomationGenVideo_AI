"""
Management command để dọn dẹp video cache.

Usage:
    python manage.py clean_video_cache --invalid  # Xóa cache không valid
    python manage.py clean_video_cache --old 30   # Xóa cache không access trong 30 ngày
    python manage.py clean_video_cache --all      # Xóa tất cả cache
    python manage.py clean_video_cache --stats    # Xem thống kê cache
"""

from django.core.management.base import BaseCommand
from video_management.services.video_cache_service import get_cache_service


class Command(BaseCommand):
    help = 'Quản lý video cache (cleanup, stats)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--invalid',
            action='store_true',
            help='Xóa các cache entries không còn valid (file bị xóa hoặc thay đổi)'
        )
        parser.add_argument(
            '--old',
            type=int,
            metavar='DAYS',
            help='Xóa cache entries không được access trong X ngày'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Xóa TẤT CẢ cache entries'
        )
        parser.add_argument(
            '--stats',
            action='store_true',
            help='Hiển thị thống kê cache'
        )

    def handle(self, *args, **options):
        cache_service = get_cache_service()
        
        if options['stats']:
            self._show_stats(cache_service)
            return
        
        if options['all']:
            self._clear_all()
            return
        
        if options['invalid']:
            self._cleanup_invalid(cache_service)
        
        if options['old']:
            self._cleanup_old(cache_service, options['old'])
        
        if not options['invalid'] and not options['old']:
            self.stdout.write(
                self.style.WARNING('Không có option nào được chọn. Dùng --help để xem các options.')
            )
            self._show_stats(cache_service)
    
    def _show_stats(self, cache_service):
        """Hiển thị thống kê cache."""
        self.stdout.write(self.style.SUCCESS('\n=== Video Cache Statistics ==='))
        
        stats = cache_service.get_cache_stats()
        
        self.stdout.write(f"Total entries: {stats['total_entries']}")
        self.stdout.write(f"Total duration: {stats['total_duration_hours']} hours")
        self.stdout.write(f"Cache hits: {stats['cache_hits']}")
        self.stdout.write(f"Cache misses: {stats['cache_misses']}")
        self.stdout.write(f"Hit rate: {stats['hit_rate']}%")
        self.stdout.write('')
    
    def _cleanup_invalid(self, cache_service):
        """Xóa cache không valid."""
        self.stdout.write('Cleaning up invalid cache entries...')
        deleted = cache_service.cleanup_invalid_cache()
        self.stdout.write(
            self.style.SUCCESS(f'✓ Deleted {deleted} invalid cache entries')
        )
    
    def _cleanup_old(self, cache_service, days):
        """Xóa cache cũ."""
        self.stdout.write(f'Cleaning up cache entries older than {days} days...')
        deleted = cache_service.cleanup_old_cache(days)
        self.stdout.write(
            self.style.SUCCESS(f'✓ Deleted {deleted} old cache entries')
        )
    
    def _clear_all(self):
        """Xóa tất cả cache."""
        from video_management.models import LocalVideoFile
        
        confirm = input('Are you sure you want to delete ALL cache entries? (yes/no): ')
        if confirm.lower() == 'yes':
            count = LocalVideoFile.objects.count()
            LocalVideoFile.objects.all().delete()
            self.stdout.write(
                self.style.SUCCESS(f'✓ Deleted all {count} cache entries')
            )
        else:
            self.stdout.write(self.style.WARNING('Cancelled'))
