"""
Django management command to index videos for smart mix.

Usage:
    python manage.py index_mix_videos --limit 50

This command scans network folders and indexes video metadata into database
for fast mixing operations.
"""

from django.core.management.base import BaseCommand
from video_management.services.smart_preprocessing_service import get_preprocessing_service


class Command(BaseCommand):
    help = 'Index videos from network folders for smart mix preprocessing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=50,
            help='Max videos to index per folder (0 = unlimited)'
        )
        parser.add_argument(
            '--folders',
            type=str,
            nargs='+',
            help='Specific folder types to index (default: all 10 types)'
        )

    def handle(self, *args, **options):
        limit = options['limit']
        specific_folders = options.get('folders')
        
        self.stdout.write(self.style.SUCCESS('Starting video indexing...'))
        
        # Define folder paths (UPDATE THESE to match your network storage!)
        # NOTE: Folder names MUST be unique! Use this mapping as reference.
        folder_mapping = {
            "Sản phẩm": "\\\\VCB_MEDIA\\MEDIA VCB folder\\VIDEO Sản Phẩm\\Logo tag Việt Nam\\Nhẫn",
            "HuyK": "\\\\VCB_MEDIA\\MEDIA VCB folder\\SOURCE HUYK\\Source daily HuyK",
            "Chế tác Above 1": "\\\\VCB_MEDIA\\MEDIA VCB folder\\CHẾ TÁC SẢN PHẨM (xưởng)\\Việt Nam\\Nhẫn",
            "Chế tác Below 1": "\\\\VCB_MEDIA\\MEDIA VCB folder\\CHẾ TÁC SẢN PHẨM (xưởng)\\Việt Nam\\Nhẫn",
            "Chế tác Above 2": "\\\\VCB_MEDIA\\MEDIA VCB folder\\CHẾ TÁC SẢN PHẨM (xưởng)\\Việt Nam\\Nhẫn",
            "HuyK Above 1": "\\\\VCB_MEDIA\\MEDIA VCB folder\\SOURCE HUYK\\Source daily HuyK",
            "HuyK Above 2": "\\\\VCB_MEDIA\\MEDIA VCB folder\\SOURCE HUYK\\Source daily HuyK",
            "Chế tác Below 2": "\\\\VCB_MEDIA\\MEDIA VCB folder\\CHẾ TÁC SẢN PHẨM (xưởng)\\Việt Nam\\Nhẫn",
            "Sản phẩm HT": "\\\\VCB_MEDIA\\MEDIA VCB folder\\VIDEO Sản Phẩm\\Logo tag Việt Nam\\Nhẫn",
            "Outtrol": "\\\\VCB_MEDIA\\MEDIA VCB folder\\SOURCE HUYK\\OUTRO HUYK",
        }
        
        # Filter if specific folders requested
        if specific_folders:
            folder_mapping = {
                k: v for k, v in folder_mapping.items()
                if k in specific_folders
            }
        
        self.stdout.write(f'Indexing from {len(folder_mapping)} folders (limit: {limit} videos/folder)...')
        
        # Run indexing
        service = get_preprocessing_service()
        results = service.index_videos_from_folders(folder_mapping, limit)
        
        # Display results
        total = sum(results.values())
        self.stdout.write(self.style.SUCCESS(f'\n✅ Indexing complete! Total: {total} videos'))
        
        for folder_type, count in results.items():
            if count > 0:
                self.stdout.write(f'  • {folder_type}: {count} videos')
            else:
                self.stdout.write(self.style.WARNING(f'  ⚠️ {folder_type}: 0 videos (check path)'))
        
        # Show cache stats
        self.stdout.write('\n📊 Cache Stats:')
        from video_management.models import IndexedVideo, VideoClipCache
        total_indexed = IndexedVideo.objects.filter(is_available=True).count()
        total_clips = VideoClipCache.objects.count()
        
        self.stdout.write(f'  • Indexed Videos: {total_indexed}')
        self.stdout.write(f'  • Cached Clips: {total_clips}')
        self.stdout.write(f'  • GPU Available: {service.has_gpu()}')
