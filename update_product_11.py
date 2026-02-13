
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import Product

def update_product_11():
    try:
        p = Product.objects.get(id=11)
        
        # Found folder: MD64_Dây chuyền cỏ 4 lá đá ghép đeo hai dáng S925_MDA02
        new_sku = "MD64"
        new_category = "Dây chuyền"
        
        print(f"🔄 Updating Product ID 11:")
        print(f"   Process: Update SKU to '{new_sku}' and Category to '{new_category}'")
        
        p.sku = new_sku
        p.category = new_category
        p.save()
        
        print("✅ Product UPDATED successfully!")
        
    except Product.DoesNotExist:
        print("❌ Product ID 11 not found!")

if __name__ == "__main__":
    update_product_11()
