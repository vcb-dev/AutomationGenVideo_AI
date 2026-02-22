"""
Database Cleanup Script for A4 V3
==================================

This script provides various cleanup operations for the video database.

Usage:
    # Remove all old folder types completely
    python manage.py cleanup_database --remove-old-folders
    
    # Clear all cache
    python manage.py cleanup_database --clear-cache
    
    # Full cleanup (remove old + clear cache)
    python manage.py cleanup_database --full

Author: VietChiBao Team
Date: 2026-02-12
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from video_management.models import IndexedVideo, VideoClipCache
import os


class Command(BaseCommand):
    help = 'Cleanup database for A4 V3 (remove old folder types, clear cache, etc.)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--remove-old-folders',
            action='store_true',
            help='Remove all videos with old folder types (Chế tác Above/Below)'
        )
        parser.add_argument(
            '--clear-cache',
            action='store_true',
            help='Clear all cached video clips'
        )
        parser.add_argument(
            '--full',
            action='store_true',
            help='Full cleanup (remove old folders + clear cache)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without doing it'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        remove_old = options['remove_old_folders'] or options['full']
        clear_cache = options['clear_cache'] or options['full']
        
        if not (remove_old or clear_cache):
            self.stdout.write(self.style.ERROR('❌ No action specified!'))
            self.stdout.write('\nUsage:')
            self.stdout.write('  --remove-old-folders  Remove old folder types')
            self.stdout.write('  --clear-cache         Clear video cache')
            self.stdout.write('  --full                Do both')
            self.stdout.write('  --dry-run             Preview changes')
            return
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('DATABASE CLEANUP FOR A4 V3'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\n🔍 DRY RUN MODE\n'))
        
        # Old folder types that should not exist in A4 V3
        old_folder_types = [
            "Chế tác Above 1",
            "Chế tác Below 1",
            "Chế tác Above 2",
            "Chế tác Below 2",
            "HuyK Above 1",
            "HuyK Above 2",
        ]
        
        # Remove old folder types
        if remove_old:
            self.stdout.write('\n🗑️  Removing old folder types...\n')
            
            total_to_remove = 0
            for old_type in old_folder_types:
                count = IndexedVideo.objects.filter(folder_type=old_type).count()
                if count > 0:
                    total_to_remove += count
                    self.stdout.write(f'  • {old_type}: {count} videos')
            
            if total_to_remove == 0:
                self.stdout.write(self.style.SUCCESS('  ✅ No old folder types found'))
            else:
                if not dry_run:
                    with transaction.atomic():
                        deleted = 0
                        for old_type in old_folder_types:
                            result = IndexedVideo.objects.filter(folder_type=old_type).delete()
                            deleted += result[0]
                        
                        self.stdout.write(self.style.SUCCESS(f'  ✅ Removed {deleted} videos'))
                else:
                    self.stdout.write(f'  [DRY RUN] Would remove {total_to_remove} videos')
        
        # Clear cache
        if clear_cache:
            self.stdout.write('\n🧹 Clearing video cache...\n')
            
            cache_count = VideoClipCache.objects.count()
            self.stdout.write(f'  Total cached clips: {cache_count}')
            
            if cache_count > 0:
                if not dry_run:
                    # Get cache directory
                    from django.conf import settings
                    cache_dir = os.path.join(settings.MEDIA_ROOT, 'video_clips_cache')
                    
                    # Delete database records
                    deleted = VideoClipCache.objects.all().delete()[0]
                    self.stdout.write(f'  ✅ Deleted {deleted} cache records from database')
                    
                    # Delete physical files
                    if os.path.exists(cache_dir):
                        import shutil
                        try:
                            shutil.rmtree(cache_dir)
                            os.makedirs(cache_dir, exist_ok=True)
                            self.stdout.write(f'  ✅ Cleared cache directory: {cache_dir}')
                        except Exception as e:
                            self.stdout.write(self.style.WARNING(f'  ⚠️  Could not clear cache directory: {e}'))
                else:
                    self.stdout.write(f'  [DRY RUN] Would delete {cache_count} cached clips')
            else:
                self.stdout.write(self.style.SUCCESS('  ✅ Cache is already empty'))
        
        # Show current stats
        self.stdout.write('\n📊 Current Database Stats:\n')
        
        # Count by folder type
        from django.db.models import Count
        folder_stats = IndexedVideo.objects.filter(
            is_available=True
        ).values('folder_type').annotate(
            count=Count('id')
        ).order_by('-count')
        
        if folder_stats:
            for stat in folder_stats:
                self.stdout.write(f'  • {stat["folder_type"]}: {stat["count"]} videos')
        else:
            self.stdout.write('  (No videos indexed)')
        
        total_videos = IndexedVideo.objects.filter(is_available=True).count()
        total_cache = VideoClipCache.objects.count()
        
        self.stdout.write(f'\n  Total indexed videos: {total_videos}')
        self.stdout.write(f'  Total cached clips: {total_cache}')
        
        # Summary
        self.stdout.write('\n' + '=' * 70)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN COMPLETE'))
            self.stdout.write('\nTo apply changes, remove --dry-run flag')
        else:
            self.stdout.write(self.style.SUCCESS('✅ CLEANUP COMPLETE!'))
        
        self.stdout.write('=' * 70 + '\n')
