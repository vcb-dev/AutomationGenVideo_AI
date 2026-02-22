import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import IndexedVideo

print("\n" + "="*70)
print("CHECKING 'Chế tác' VIDEOS FOR 'Dây chuyền'")
print("="*70 + "\n")

# Check all Chế tác videos
all_che_tac = IndexedVideo.objects.filter(folder_type="Chế tác", is_available=True).count()
print(f"Total 'Chế tác' videos: {all_che_tac}")

# Check Chế tác videos with "Dây chuyền" in path
day_chuyen_che_tac = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    is_available=True,
    file_path__icontains="Dây chuyền"
).count()
print(f"'Chế tác' videos with 'Dây chuyền' in path: {day_chuyen_che_tac}")

# Check Chế tác videos with "Nhẫn" in path
nhan_che_tac = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    is_available=True,
    file_path__icontains="Nhẫn"
).count()
print(f"'Chế tác' videos with 'Nhẫn' in path: {nhan_che_tac}")

# Show some sample paths
print("\n" + "="*70)
print("SAMPLE 'Chế tác' VIDEO PATHS:")
print("="*70 + "\n")

samples = IndexedVideo.objects.filter(
    folder_type="Chế tác",
    is_available=True
).values_list('file_path', flat=True)[:5]

for i, path in enumerate(samples, 1):
    # Extract just the relevant part
    if "CHẾ TÁC" in path:
        parts = path.split("CHẾ TÁC SẢN PHẨM (xưởng)")
        if len(parts) > 1:
            print(f"{i}. ...CHẾ TÁC SẢN PHẨM (xưởng){parts[1][:100]}")
        else:
            print(f"{i}. {path[:100]}")
    else:
        print(f"{i}. {path[:100]}")

print("\n" + "="*70)
