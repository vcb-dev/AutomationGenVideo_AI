
def _normalize_path(p):
    """Chuẩn hóa path cho Windows (env có thể dùng /)."""
    import os
    if not p:
        return p
    return os.path.normpath(str(p).replace('/', os.path.sep))


def _find_sku_folder_smart(service, root_path, sku):
    """Helper to find a folder matching SKU within a root path with reasonable depth."""
    import os
    root_path = _normalize_path(root_path)
    if not root_path or not os.path.exists(root_path):
        return None

    # Try finding folder with SKU name or partial match
    return service.find_folder_by_name(
        root_path=root_path,
        target_name=sku.strip(),
        exact_match=False,
        max_depth=3 # Root -> [Category -> SubCategory] -> SKU
    )

def _clear_product_slots_before_sku_index():
    """
    Xóa indexed (và cached clips) của Sản phẩm, Sản phẩm HT, Chế tác trước khi index theo SKU.
    Đảm bảo chỉ còn video của sản phẩm đang chọn.
    """
    from video_management.models import IndexedVideo

    product_types = ["Sản phẩm", "Sản phẩm HT", "Chế tác"]
    deleted, _ = IndexedVideo.objects.filter(folder_type__in=product_types).delete()
    return deleted


def _auto_index_by_sku_global(service, sku: str, category_name: str = None):
    """
    Powerful global SKU scanner.
    Xóa Sản phẩm/HT/Chế tác cũ rồi index mới theo SKU.
    Chỉ giữ video của sản phẩm đang chọn.
    """
    import logging
    import os
    from django.conf import settings
    
    logger = logging.getLogger(__name__)
    if not sku:
        return {}

    # Xóa indexed + cache cũ để chỉ còn video của SKU này
    cleared = _clear_product_slots_before_sku_index()
    if cleared:
        logger.info(f"🧹 Cleared {cleared} old product/Chế tác videos before indexing SKU '{sku}'")

    logger.info(f"🚀 GLOBAL SKU SCAN: Searching for SKU '{sku}'...")
    
    # Use configured paths from settings (.env)
    base_video_paths = getattr(settings, 'VIDEO_BASE_PATHS', [
        r"\\VCB_MEDIA\MEDIA VCB folder",
    ])
    
    results = {}
    sku_clean = sku.strip()

    # 1. SCAN FOR 'Sản phẩm' → VIDEO_BASE_PATHS/PRODUCT_VIDEO_SUBFOLDER/<category>/<sku>
    prod_root_rel = getattr(settings, 'PRODUCT_VIDEO_SUBFOLDER', r"Video Sản Phẩm")
    found_prod_path = None
    
    for base in base_video_paths:
        root = _normalize_path(os.path.join(base, prod_root_rel))
        found_prod_path = _find_sku_folder_smart(service, root, sku_clean)
        if found_prod_path: break
        
    if found_prod_path:
        logger.info(f"✅ Found Product folder: '{found_prod_path}'")
        # Index into BOTH Slot 1 (Sản phẩm) AND Slot 6 (Sản phẩm HT)
        service.index_videos_from_folders({
            "Sản phẩm": found_prod_path,
            "Sản phẩm HT": found_prod_path,
        })
        results["Sản phẩm"] = found_prod_path
        results["Sản phẩm HT"] = found_prod_path

    # 2. SCAN FOR 'Chế tác' → VIDEO_BASE_PATHS/MANUFACTURING_FOLDER_PATH/<Nhẫn>/<NM101_...>
    # Path: Generate Video\Chế tác sản phẩm\Việt Nam\Nhẫn\NM101_Nhẫn tàng hình
    mfg_root_rel = getattr(settings, 'MANUFACTURING_FOLDER_PATH', r'Chế tác sản phẩm\Việt Nam')
    found_mfg_path = None
    
    for base in base_video_paths:
        root = _normalize_path(os.path.join(base, mfg_root_rel))
        found_mfg_path = _find_sku_folder_smart(service, root, sku_clean)
        if found_mfg_path: break

    if found_mfg_path:
        logger.info(f"✅ Found Manufacturing folder: '{found_mfg_path}'")
        service.index_videos_from_folders({"Chế tác": found_mfg_path})
        results["Chế tác"] = found_mfg_path
    # Không fallback sang folder tổng category — Chế tác phải lấy video theo đúng mã SKU.

    return results

def _auto_index_manufacturing_folders(service, category_name: str, product_sku: str = None):
    """
    Auto-index manufacturing (Chế tác) folders based on product category & SKU.
    Looks inside Generate Video\Chế tác sản phẩm\<category>
    """
    import logging
    import os
    from django.conf import settings
    
    logger = logging.getLogger(__name__)
    if not category_name: return

    logger.info(f"🔍 Indexing Category folder: '{category_name}'")
    
    base_video_paths = getattr(settings, 'VIDEO_BASE_PATHS', [
        r"\\VCB_MEDIA\MEDIA VCB folder\Generate Video"
    ])
    mfg_path = getattr(settings, 'MANUFACTURING_FOLDER_PATH', r'Chế tác sản phẩm')
    
    for base in base_video_paths:
        root = _normalize_path(os.path.join(base, mfg_path))
        if not os.path.exists(root):
            continue

        cat_path = service.find_folder_by_name(root, category_name.strip(), exact_match=False, max_depth=1)
        if cat_path:
            logger.info(f"📂 Indexing category path: {cat_path}")
            service.index_videos_from_folders({"Chế tác": cat_path})
            break
