
import os
import django
import sys

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import Product

def check_product(product_id):
    print(f"🔍 Checking Product ID: {product_id}...")
    try:
        p = Product.objects.get(id=product_id)
        print(f"   Name: {p.name}")
        print(f"   SKU:  {p.sku}")
        print(f"   Cat:  '{p.category}'") # Quoted to see if empty
        
        if not p.category:
            print("⚠️  WARNING: Category is EMPTY! Auto-indexing will FAIL.")
            
            # Suggest fix based on name?
            guess = "Dây chuyền" if "Dây chuyền" in p.name else "?"
            print(f"   💡 Suggested Category: {guess}")
            
    except Product.DoesNotExist:
        print("❌ Product not found!")

if __name__ == "__main__":
    check_product(11)
