# Generated manually - fix malformed UNC paths in IndexedVideo

from django.db import migrations


def fix_unc_paths(apps, schema_editor):
    """Fix file_path: UNC\server\share -> \\server\share. Remove duplicates."""
    IndexedVideo = apps.get_model('video_management', 'IndexedVideo')
    updated = 0
    deleted = 0
    for obj in IndexedVideo.objects.filter(file_path__startswith='UNC\\'):
        correct_path = '\\\\' + obj.file_path[4:]
        # If correct path already exists (same folder_type), delete this duplicate
        if IndexedVideo.objects.filter(file_path=correct_path, folder_type=obj.folder_type).exclude(pk=obj.pk).exists():
            obj.delete()
            deleted += 1
        else:
            obj.file_path = correct_path
            obj.save()
            updated += 1
    if updated or deleted:
        print(f"Fixed {updated} paths, removed {deleted} duplicates (UNC -> \\\\)")


def reverse_fix(apps, schema_editor):
    pass  # No reverse


class Migration(migrations.Migration):

    dependencies = [
        ('video_management', '0013_allow_same_file_different_folder_type'),
    ]

    operations = [
        migrations.RunPython(fix_unc_paths, reverse_fix),
    ]
