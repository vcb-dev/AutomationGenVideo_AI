from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.conf import settings
import requests
import logging

from ..serializers import VideoDownloadSerializer

logger = logging.getLogger(__name__)

@method_decorator(csrf_exempt, name='dispatch')
class DownloadView(APIView):
    def post(self, request):
        serializer = VideoDownloadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            url = serializer.validated_data['url']
            logger.info(f"Download request for URL: {url}")
            
            # from ..services.rapidapi_service import TikhubService
            # api_service = TikhubService()
            
            # Temporarily disabled
            return Response({
                "error": "Download service is currently unavailable"
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            
            # ... (Rest of logic disabled)
                
        except Exception as e:
            logger.error(f"Download error: {e}", exc_info=True)
            return Response(
                {"error": f"Download resolution error: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
