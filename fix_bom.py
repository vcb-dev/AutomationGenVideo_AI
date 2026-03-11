
import os

filepath = r'video_management\views\smart_mix_video_views.py'

# Đọc bytes gốc
with open(filepath, 'rb') as f:
    raw = f.read()

# Remove BOM nếu có
if raw.startswith(b'\xef\xbb\xbf'):
    raw = raw[3:]
    print('BOM removed')

# Decode
content = raw.decode('utf-8')

# Kiểm tra middle_pattern hiện tại
idx = content.find('middle_pattern')
print('Current:', repr(content[idx:idx+80]))

# Ghi lại không có BOM
with open(filepath, 'w', encoding='utf-8', newline='') as f:
    f.write(content)

print('Done - file rewritten without BOM')

# Verify syntax
import ast
ast.parse(content)
print('Syntax OK')
