
# Script sửa middle_pattern trong smart_mix_video_views.py
import os

filepath = r'video_management\views\smart_mix_video_views.py'

with open(filepath, encoding='utf-8') as f:
    content = f.read()

# Tìm và thay thế
old_line = 'middle_pattern = ["Chế tác", "HuyK"]  # Xen kẽ bắt đầu bằng Chế tác'
new_line = 'middle_pattern = ["HuyK", "Chế tác"]  # Bắt đầu bằng HuyK → HuyK xuất hiện >= Chế tác'

if old_line in content:
    content = content.replace(old_line, new_line)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print('SUCCESS: Đã đổi pattern thành HuyK first')
else:
    # thử tìm gần đúng
    idx = content.find('middle_pattern')
    print(f'NOT FOUND. Raw around middle_pattern: {repr(content[idx:idx+80])}')
