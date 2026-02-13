
import os
import django
from typing import List, Dict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.product_catalog_service import ProductCatalogService

def test_mapping():
    print("🧪 TEST: Kiểm tra logic nhận diện cột Excel mới")
    print("-" * 50)

    # Giả lập header file Excel thực tế (tên cột lộn xộn, tiếng Việt, viết tắt)
    mock_excel_columns = [
        "STT",
        "Tên sản phẩm chi tiết (VN)",   # Mong đợi: Name
        "Mã SP (SKU)",                  # Mong đợi: SKU
        "Danh mục ngành hàng",          # Mong đợi: Category
        "Giá bán lẻ (VND)",             # Mong đợi: Price
        "Mô tả / Description",          # Mong đợi: Description
        "Ghi chú"                       # Không map
    ]

    print(f"📋 Cột trong Excel: {mock_excel_columns}")
    
    # Chuyển về lowercase như code thật
    normalized_cols = [c.strip().lower() for c in mock_excel_columns]
    
    # Gọi hàm map
    mapping = ProductCatalogService._map_columns(normalized_cols)
    
    print("-" * 50)
    print("✅ KẾT QUẢ MAPPING:")
    for field, col_name in mapping.items():
        original_col = mock_excel_columns[normalized_cols.index(col_name)]
        print(f"   🔹 Database Field '{field.upper()}'  <==  Cột Excel '{original_col}'")
        
    # Check kết quả
    required = ['sku', 'category', 'name']
    missing = [f for f in required if f not in mapping]
    
    if not missing:
        print("-" * 50)
        print("🎉 THÀNH CÔNG! Code mới đã nhận diện đủ các trường quan trọng.")
    else:
        print(f"❌ THẤT BẠI. Còn thiếu: {missing}")

if __name__ == "__main__":
    test_mapping()
