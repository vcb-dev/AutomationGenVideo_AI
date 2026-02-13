
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import Product, ProductList, IndexedVideo, VideoClipCache

def clear_all_data():
    print("🗑️ STARTING CLEANUP...")
    
    # 1. Clear Products
    p_count = Product.objects.count()
    Product.objects.all().delete()
    print(f"✅ Deleted {p_count} Products.")

    # 2. Clear Product Lists
    pl_count = ProductList.objects.count()
    ProductList.objects.all().delete()
    print(f"✅ Deleted {pl_count} Product Lists.")
    
    # 3. Clear Video Indexes (Optional but good for fresh start)
    iv_count = IndexedVideo.objects.count()
    IndexedVideo.objects.all().delete()
    print(f"✅ Deleted {iv_count} Indexed Videos.")
    
    # 4. Clear Cache
    vc_count = VideoClipCache.objects.count()
    VideoClipCache.objects.all().delete()
    print(f"✅ Deleted {vc_count} Cached Clips.")

    print("-" * 30)
    print("✨ DATABASE CLEARED SUCCESSFULLY! READY FOR NEW UPLOAD.")

if __name__ == "__main__":
    clear_all_data()
