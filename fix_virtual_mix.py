
import sys
filepath = r'video_management\views\virtual_mix_views.py'

with open(filepath, encoding='utf-8') as f:
    content = f.read()

old = '''                # Ưu tiên cached, nhưng LUÔN chọn video (để hiển thị đủ slot)
                cached_candidates = [v for v in candidates if v in cache_map]

                if cached_candidates:
                    vid = random.choice(cached_candidates)
                else:
                    # Chưa có cache – vẫn chọn video để hiển thị trong preview
                    # Frontend sẽ stream trực tiếp từ /api/videos/stream/<id>/
                    vid = random.choice(candidates)
                    need_gen.add(vid)'''

new = '''                # CHỈ chọn video đã có cached clips – preview phải chạy INSTANT
                # Nếu không có cache cho slot này → bỏ qua slot đó
                cached_candidates = [v for v in candidates if v in cache_map]

                if not cached_candidates:
                    logger.warning(f"⚠️ Slot {slot_idx+1} ({ft}): no cached clips yet, skipping slot in preview")
                    continue

                vid = random.choice(cached_candidates)'''

if old in content:
    content = content.replace(old, new)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print('SUCCESS: Virtual mix now only uses cached clips for preview')
else:
    print('NOT FOUND - checking...')
    idx = content.find('Ưu tiên cached')
    if idx >= 0:
        print(repr(content[idx:idx+300]))
    else:
        print('Cannot find target string')
