
import sys
filepath = r'video_management\views\virtual_mix_views.py'

with open(filepath, encoding='utf-8') as f:
    content = f.read()

# Fix 1: Xóa need_gen logic thừa
old1 = '''        # ── Select videos for ALL outputs ─────────────────────────
        # Luôn hiển thị TẤT CẢ slots, kể cả khi không có cache
        all_selections = []
        need_gen = set()'''
new1 = '''        # ── Select videos cho preview – CHỈ dùng cached clips ────
        all_selections = []'''

# Fix 2: Xóa khối if need_gen thừa
old2 = '''        if need_gen:
            logger.info(
                f"⚡ Virtual Mix: {len(cache_map)} cached + "
                f"{len(need_gen)} slots without cache (will stream on-the-fly)"
            )

        # ── Build manifests ────────────────────────────────────────'''
new2 = '''        logger.info(f"⚡ Virtual Mix: {len(all_selections)} slots with cached clips selected")

        # ── Build manifests ────────────────────────────────────────'''

# Fix 3: Đơn giản hóa build manifest - luôn dùng stream-clip vì guaranteed cached
old3 = '''                if cache_info:
                    stream_url = f'/api/videos/stream-clip/{cache_info["cache_id"]}/'
                else:
                    # Stream on-the-fly khi frontend request (auto-generate nếu cần)
                    stream_url = f'/api/videos/stream/{vid}/'

                clips.append({
                    'video_id': vid,
                    'slot': si + 1,
                    'slot_name': config['name'],
                    'duration': round(dur, 2),
                    'stream_url': stream_url,
                    'folder_type': config['folder_type'],
                    'cached': cache_info is not None,'''
new3 = '''                # All selected videos guaranteed to have cache (selected above)
                stream_url = f'/api/videos/stream-clip/{cache_info["cache_id"]}/'

                clips.append({
                    'video_id': vid,
                    'slot': si + 1,
                    'slot_name': config['name'],
                    'duration': round(dur, 2),
                    'stream_url': stream_url,
                    'folder_type': config['folder_type'],
                    'cached': True,'''

ok = True
for old, new in [(old1, new1), (old2, new2), (old3, new3)]:
    if old in content:
        content = content.replace(old, new)
        print(f'OK: replaced {old[:40]!r}...')
    else:
        print(f'NOT FOUND: {old[:60]!r}')
        ok = False

if ok:
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    import ast; ast.parse(content)
    print('SUCCESS & Syntax OK')
