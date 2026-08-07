"""
Views cho PAAST Content Analyzer.
"""
import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from video_management.services.paast_analysis_service import PaastAnalysisService

logger = logging.getLogger(__name__)

MIN_CONTENT_LENGTH = 100


@api_view(['POST'])
def analyze_content(request):
    """
    Phân tích content theo khung PAAST (5 lớp x 6 tiêu chí) và tính điểm 0-100.

    POST /api/ai/paast/analyze/
    Body:
    {
        "content": "..."
    }
    """
    try:
        content = (request.data.get('content') or '').strip()

        if len(content) < MIN_CONTENT_LENGTH:
            return Response(
                {'error': f'Content quá ngắn — cần ít nhất {MIN_CONTENT_LENGTH} ký tự'},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = PaastAnalysisService()
        result = service.analyze(content)

        return Response({
            'success': True,
            **result,
        })

    except RuntimeError as e:
        logger.error(f"PAAST analyze failed: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception as e:
        logger.error(f"Error in analyze_content view: {str(e)}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def upgrade_content(request):
    """
    Nâng cấp content dựa trên danh sách tiêu chí đang thiếu, rồi phân tích lại bản mới.

    POST /api/ai/paast/upgrade/
    Body:
    {
        "original_content": "...",
        "missing_elements": [
            {"layer": "acknowledge", "criterion": "STORY", "suggestion": "..."}
        ]
    }
    """
    try:
        original_content = (request.data.get('original_content') or '').strip()
        missing_elements = request.data.get('missing_elements') or []

        if len(original_content) < MIN_CONTENT_LENGTH:
            return Response(
                {'error': f'original_content quá ngắn — cần ít nhất {MIN_CONTENT_LENGTH} ký tự'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not isinstance(missing_elements, list):
            return Response(
                {'error': 'missing_elements phải là một mảng'},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = PaastAnalysisService()
        result = service.upgrade(original_content, missing_elements)

        return Response({
            'success': True,
            **result,
        })

    except RuntimeError as e:
        logger.error(f"PAAST upgrade failed: {str(e)}")
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    except Exception as e:
        logger.error(f"Error in upgrade_content view: {str(e)}", exc_info=True)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
