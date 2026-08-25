import base64
import logging

from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from video_management.services.thumbnail_service import (
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    MAX_CONTENT_BYTES,
    MAX_TITLE_LEN,
    ThumbnailInput,
    download_image_bytes,
    generate_thumbnail,
    list_templates,
    load_person_bytes_by_slug,
)

logger = logging.getLogger(__name__)

VALID_ORIENTATIONS = {'landscape', 'portrait'}

# Parse boolean từ string.
def _parse_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

# Validate dữ liệu đầu vào
def _validate_generate_form(data, files):
    title = (data.get('title') or '').strip()
    template = (data.get('template') or 'simple_v1').strip()
    orientation = (data.get('orientation') or 'landscape').strip().lower()
    font_key = (data.get('font') or 'bevietnam_bold').strip()
    person_slug = (data.get('person_slug') or '').strip()
    person_url = (data.get('person_image_url') or '').strip()
    content = files.get('content_image')
    enhance_background = _parse_bool(data.get('enhance_background'), default=True)
    font_size_raw = (data.get('font_size') or '').strip()
    text_color = (data.get('text_color') or '').strip()

    if not title:
        return None, 'Vui lòng nhập tiêu đề'
    if len(title) > MAX_TITLE_LEN:
        return None, f'Tiêu đề tối đa {MAX_TITLE_LEN} ký tự'
    if orientation not in VALID_ORIENTATIONS:
        return None, 'Layout phải là landscape hoặc portrait'
    if not content:
        return None, 'Vui lòng upload ảnh nội dung'
    if content.size > MAX_CONTENT_BYTES:
        return None, f'Ảnh nội dung tối đa {MAX_CONTENT_BYTES // (1024 * 1024)}MB'

    font_size = None
    if font_size_raw:
        try:
            font_size = int(font_size_raw)
        except ValueError:
            return None, f'Cỡ chữ phải là số nguyên từ {FONT_SIZE_MIN} đến {FONT_SIZE_MAX}'
        if not FONT_SIZE_MIN <= font_size <= FONT_SIZE_MAX:
            return None, f'Cỡ chữ phải từ {FONT_SIZE_MIN} đến {FONT_SIZE_MAX}'

    if text_color and not text_color.startswith('#'):
        text_color = f'#{text_color}'

    return {
        'title': title,
        'template': template,
        'orientation': orientation,
        'font_key': font_key,
        'person_slug': person_slug,
        'person_url': person_url,
        'content_bytes': content.read(),
        'enhance_background': enhance_background,
        'font_size': font_size,
        'text_color': text_color or None,
    }, None

# API danh sách các template
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def thumbnail_templates_api(request):
    return Response({'success': True, **list_templates()})

# API tạo thumbnail
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def thumbnail_generate_api(request):
    # Validate dữ liệu đầu vào
    payload, err = _validate_generate_form(request.data, request.FILES)
    if err:
        return Response({'success': False, 'error': err}, status=status.HTTP_400_BAD_REQUEST)

    try:
        # Tải ảnh người từ URL hoặc assets
        if payload['person_url']:
            person_bytes = download_image_bytes(payload['person_url'])
        elif payload['person_slug']:
            person_bytes = load_person_bytes_by_slug(payload['person_slug'])
        else:
            return Response(
                {'success': False, 'error': 'Thiếu ảnh người — gửi person_image_url hoặc person_slug'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Tạo thumbnail
        png_bytes = generate_thumbnail(
            ThumbnailInput(
                title=payload['title'],
                template=payload['template'],
                orientation=payload['orientation'],
                font_key=payload['font_key'],
                content_image_bytes=payload['content_bytes'],
                person_image_bytes=person_bytes,
                enhance_background=payload['enhance_background'],
                font_size=payload['font_size'],
                text_color=payload['text_color'],
            )
        )
    except ValueError as exc:
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except FileNotFoundError as exc:
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except Exception:
        logger.error('[THUMBNAIL] generate failed', exc_info=True)
        return Response(
            {'success': False, 'error': 'Không tạo được thumbnail — vui lòng thử lại'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return Response({
        'success': True,
        'mime_type': 'image/png',
        'image_base64': base64.b64encode(png_bytes).decode('ascii'),
    })
