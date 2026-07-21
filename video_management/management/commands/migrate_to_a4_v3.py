"""
Database Migration Script for A4 V3
====================================

This script migrates the database from A4 V2 (8 slots with split layouts) 
to A4 V3 (7 simple slots).

Changes:
- Removes old folder types: "Chế tác Above 1/2", "Chế tác Below 1/2"
- Consolidates them into single "Chế tác" folder type
- Updates all IndexedVideo records accordingly

Usage:
    python manage.py migrate_to_a4_v3

Author: VietChiBao Team
Date: 2026-02-12
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from video_management.models import IndexedVideo, VideoClipCache


class Command(BaseCommand):
    help = 'Migrate database from A4 V2 to A4 V3 (consolidate Chế tác folders)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without actually changing it'
        )
        parser.add_argument(
            '--delete-cache',
            action='store_true',
            help='Also delete cached clips for old folder types'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        delete_cache = options['delete_cache']
        
        self.stdout.write(self.style.SUCCESS('=' * 70))
        self.stdout.write(self.style.SUCCESS('DATABASE MIGRATION: A4 V2 → A4 V3'))
        self.stdout.write(self.style.SUCCESS('=' * 70))
        
        if dry_run:
            self.stdout.write(self.style.WARNING('\n🔍 DRY RUN MODE - No changes will be made\n'))
        
        # Old folder types to consolidate
        old_folder_types = [
            "Chế tác Above 1",
            "Chế tác Below 1",
            "Chế tác Above 2",
            "Chế tác Below 2",
        ]
        
        new_folder_type = "Chế tác"
        
        # Step 1: Count affected records
        self.stdout.write('\n📊 Step 1: Analyzing database...\n')
        
        total_affected = 0
        for old_type in old_folder_types:
            count = IndexedVideo.objects.filter(folder_type=old_type).count()
            total_affected += count
            if count > 0:
                self.stdout.write(f'  • {old_type}: {count} videos')
        
        if total_affected == 0:
            self.stdout.write(self.style.SUCCESS('\n✅ No migration needed! Database is already clean.'))
            return
        
        self.stdout.write(f'\n  Total videos to migrate: {total_affected}')
        
        # Step 2: Check for existing "Chế tác" records
        existing_che_tac = IndexedVideo.objects.filter(folder_type=new_folder_type).count()
        self.stdout.write(f'\n  Existing "{new_folder_type}" videos: {existing_che_tac}')
        
        # Step 3: Migrate records
        if not dry_run:
            self.stdout.write(f'\n🔄 Step 2: Migrating records to "{new_folder_type}"...\n')
            
            with transaction.atomic():
                migrated_count = 0
                skipped_count = 0
                
                for old_type in old_folder_types:
                    # Get all videos from this old type
                    old_videos = IndexedVideo.objects.filter(folder_type=old_type)
                    
                    for video in old_videos:
                        # Check if this file_path already exists in new folder type
                        existing = IndexedVideo.objects.filter(
                            file_path=video.file_path,
                            folder_type=new_folder_type
                        ).first()
                        
                        if existing:
                            # Duplicate - delete the old one
                            video.delete()
                            skipped_count += 1
                        else:
                            # No duplicate - update folder_type
                            video.folder_type = new_folder_type
                            video.save()
                            migrated_count += 1
                    
                    self.stdout.write(f'  ✅ Processed "{old_type}"')
                
                self.stdout.write(f'\n  Total migrated: {migrated_count} videos')
                self.stdout.write(f'  Duplicates removed: {skipped_count} videos')
        else:
            self.stdout.write(f'\n  [DRY RUN] Would migrate {total_affected} videos to "{new_folder_type}"')
        
        # Step 4: Handle cached clips
        if delete_cache:
            self.stdout.write('\n🗑️  Step 3: Cleaning up cached clips...\n')
            
            # Note: VideoClipCache doesn't have folder_type, it references IndexedVideo
            # So we need to find clips that reference old videos
            cache_count = VideoClipCache.objects.count()
            self.stdout.write(f'  Total cached clips: {cache_count}')
            
            if not dry_run:
                # Delete all cache to be safe (will be regenerated on next use)
                deleted = VideoClipCache.objects.all().delete()[0]
                self.stdout.write(f'  ✅ Deleted {deleted} cached clips (will regenerate on demand)')
            else:
                self.stdout.write(f'  [DRY RUN] Would delete all {cache_count} cached clips')
        
        # Step 5: Verify migration
        if not dry_run:
            self.stdout.write('\n✅ Step 4: Verifying migration...\n')
            
            # Check if any old folder types remain
            remaining = 0
            for old_type in old_folder_types:
                count = IndexedVideo.objects.filter(folder_type=old_type).count()
                remaining += count
                if count > 0:
                    self.stdout.write(self.style.ERROR(f'  ❌ Still found {count} videos in "{old_type}"'))
            
            if remaining == 0:
                self.stdout.write(self.style.SUCCESS('  ✅ All old folder types removed'))
            
            # Show new count
            new_count = IndexedVideo.objects.filter(folder_type=new_folder_type).count()
            self.stdout.write(f'  ✅ Total "{new_folder_type}" videos: {new_count}')
        
        # Summary
        self.stdout.write('\n' + '=' * 70)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN COMPLETE - No changes made'))
            self.stdout.write('\nTo apply changes, run:')
            self.stdout.write('  python manage.py migrate_to_a4_v3')
            if delete_cache:
                self.stdout.write('  python manage.py migrate_to_a4_v3 --delete-cache')
        else:
            self.stdout.write(self.style.SUCCESS('✅ MIGRATION COMPLETE!'))
            self.stdout.write('\nNext steps:')
            self.stdout.write('  1. Restart your Django server')
            self.stdout.write('  2. Test video generation with A4 V3 formula')
            self.stdout.write('  3. Monitor logs for any issues')
        
        self.stdout.write('=' * 70 + '\n')
