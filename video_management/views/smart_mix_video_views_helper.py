
def _normalize_path(p):
    """Chuẩn hóa path cho Windows (env có thể dùng /)."""
    import os
    if not p:
        return p
    return os.path.normpath(str(p).replace('/', os.path.sep))


def _translate_to_nas_path(path, nas_base):
    """
    Translate Windows UNC path (\\VCB_MEDIA\... or //VCB_MEDIA/...) 
    to NAS Linux path (/volume1/...) using the configured nas_base.
    If path is already a valid Linux path, return as-is.
    """
    import os
    if not path:
        return path
    # Already a valid Linux path?
    if os.path.exists(path):
        return path
    # Normalize slashes
    norm = path.replace('\\', '/')
    # Detect UNC-style path
    if '//VCB_MEDIA/' in norm or norm.startswith('//VCB_MEDIA'):
        marker = 'MEDIA VCB folder/'
        idx = norm.find(marker)
        if idx != -1:
            sub = norm[idx + len(marker):]
            nas_base_norm = nas_base.replace('\\', '/')
            # nas_base already contains "Generate Video" or similar
            # We need to go up to "MEDIA VCB folder" level
            # Find "MEDIA VCB folder" in nas_base
            base_marker = 'MEDIA VCB folder/'
            base_idx = nas_base_norm.find(base_marker)
            if base_idx != -1:
                nas_media_root = nas_base_norm[:base_idx] + base_marker.rstrip('/')
                translated = os.path.join(nas_media_root, sub)
            else:
                # Fallback: just append sub to nas_base parent
                translated = os.path.join(os.path.dirname(nas_base_norm), sub)
            return translated
    return path


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
    import platform
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
    
    # ── TRANSLATE NAS PATHS nếu chạy trên Linux ────────────────────────────
    # VIDEO_BASE_PATHS có thể vẫn là Windows UNC → cần dịch sang NAS path
    is_linux = platform.system() != 'Windows'
    if is_linux and base_video_paths:
        translated_paths = []
        for bp in base_video_paths:
            # Thử translate: //VCB_MEDIA/... → /volume1/...
            translated = _translate_to_nas_path(bp, bp)
            # Nếu translated path không tồn tại, thử dùng /volume1/ prefix
            if not os.path.exists(translated):
                # Tìm phần cuối path sau "MEDIA VCB folder/"
                norm = bp.replace('\\', '/')
                marker = 'MEDIA VCB folder/'
                idx = norm.find(marker)
                if idx != -1:
                    sub = norm[idx + len(marker):]
                    candidate = f'/volume1/MEDIA VCB folder/{sub}'
                    if os.path.isdir(candidate):
                        translated = candidate
                        logger.info(f"🔄 Translated VIDEO_BASE_PATHS: {bp} → {translated}")
            translated_paths.append(translated)
        base_video_paths = translated_paths
    # ──────────────────────────────────────────────────────────────────────
    
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
    else:
        logger.warning(f"⚠️ Product folder not found for SKU '{sku}' in paths: {base_video_paths}")

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
    else:
        logger.warning(f"⚠️ Manufacturing folder not found for SKU '{sku}' in paths: {base_video_paths}")
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
