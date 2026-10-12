"""Gửi chi phí API bên thứ ba (TikHub, Gemini) của mỗi request về BE qua header X-Api-Usage.

BE có một chốt chung đọc header này ở MỌI lượt gọi sang AI rồi ghi vào api_usage_logs kèm ngữ
cảnh bên BE (người bấm, màn hình). Nhờ vậy không phải sửa từng module BE đang gọi AI.
"""
import json
import logging

from video_management.services import usage_meter

logger = logging.getLogger(__name__)

HEADER = 'X-Api-Usage'


class ApiUsageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with usage_meter.collect() as events:
            response = self.get_response(request)
        if events:
            try:
                # ensure_ascii mặc định → giá trị header thuần ASCII
                response[HEADER] = json.dumps(usage_meter.summarize(events), separators=(',', ':'))
            except Exception as e:  # noqa: BLE001 — lỗi thống kê không được làm hỏng phản hồi
                logger.warning('[API-USAGE] Không gắn được header chi phí: %s', e)
        return response
