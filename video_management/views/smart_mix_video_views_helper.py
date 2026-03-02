
def _find_sku_folder_smart(service, root_path, sku):
    """Helper to find a folder matching SKU within a root path with reasonable depth."""
    import os
    if not root_path or not os.path.exists(root_path):
        return None
    
    # Try finding folder with SKU name or partial match
    return service.find_folder_by_name(
        root_path=root_path,
        target_name=sku.strip(),
        exact_match=False,
        max_depth=3 # Root -> [Category -> SubCategory] -> SKU
    )

def _auto_index_by_sku_global(service, sku: str, category_name: str = None):
    """
    Powerful global SKU scanner.
    Indexes videos into 'Sản phẩm' and 'Chế tác' slots by finding folders matching SKU.
    
    Folder structure (actual):
      \\VCB_MEDIA\MEDIA VCB folder\Generate Video\
          Video Sản Phẩm\   → slot 'Sản phẩm'
          Source HUYK\      → slot 'HuyK'
          Chế tác sản phẩm\ → slot 'Chế tác'
    """
    import logging
    import os
    from django.conf import settings
    
    logger = logging.getLogger(__name__)
    if not sku:
        return {}

    logger.info(f"🚀 GLOBAL SKU SCAN: Searching for SKU '{sku}'...")
    
    # Base root: Generate Video folder (primary source of all slot videos)
    base_video_paths = getattr(settings, 'VIDEO_BASE_PATHS', [
        r"\\VCB_MEDIA\MEDIA VCB folder\Generate Video",
    ])
    
    results = {}
    sku_clean = sku.strip()

    # 1. SCAN FOR 'Sản phẩm' → Generate Video\Video Sản Phẩm\<category>\<sku>
    prod_root_rel = r"Video Sản Phẩm"
    found_prod_path = None
    
    for base in base_video_paths:
        root = os.path.join(base, prod_root_rel)
        found_prod_path = _find_sku_folder_smart(service, root, sku_clean)
        if found_prod_path: break
        
    if found_prod_path:
        logger.info(f"✅ Found Product folder: '{found_prod_path}'")
        service.index_videos_from_folders({"Sản phẩm": found_prod_path})
        results["Sản phẩm"] = found_prod_path

    # 2. SCAN FOR 'Chế tác' → Generate Video\Chế tác sản phẩm\<category>\<sku>
    mfg_root_rel = getattr(settings, 'MANUFACTURING_FOLDER_PATH', r'Chế tác sản phẩm')
    found_mfg_path = None
    
    for base in base_video_paths:
        root = os.path.join(base, mfg_root_rel)
        found_mfg_path = _find_sku_folder_smart(service, root, sku_clean)
        if found_mfg_path: break

    if found_mfg_path:
        logger.info(f"✅ Found Manufacturing folder: '{found_mfg_path}'")
        service.index_videos_from_folders({"Chế tác": found_mfg_path})
        results["Chế tác"] = found_mfg_path
        
    # FALLBACK: If category provided but no SKU folder found, index category level for Chế tác
    if not found_mfg_path and category_name:
        _auto_index_manufacturing_folders(service, category_name)
        results["Chế tác (Category)"] = category_name

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
        root = os.path.join(base, mfg_path)
        if not os.path.exists(root): continue
        
        cat_path = service.find_folder_by_name(root, category_name.strip(), exact_match=False, max_depth=1)
        if cat_path:
            logger.info(f"📂 Indexing category path: {cat_path}")
            service.index_videos_from_folders({"Chế tác": cat_path})
            break
