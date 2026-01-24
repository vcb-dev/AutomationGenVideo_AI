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
            
            from ..services.rapidapi_service import TikhubService
            api_service = TikhubService()
            
            # Call Tikhub download endpoint
            download_url = f"{api_service.base_url}/api/download/video"
            params = {'url': url}
            
            logger.info(f"Calling Tikhub API download: {download_url}")
            
            response = requests.get(
                download_url,
                headers=api_service.headers,
                params=params,
                timeout=30.0
            )
            
            if response.status_code == 200:
                data = response.json()
                download_link = None
                
                # Logic parse response linh hoat
                if isinstance(data, dict):
                    download_link = (
                        data.get('download_url') or
                        data.get('video_url') or
                        data.get('url') or
                        data.get('data', {}).get('download_url') or
                        data.get('data', {}).get('video_url') or
                        data.get('data', {}).get('url')
                    )
                elif isinstance(data, str):
                    download_link = data
                
                if download_link:
                    return Response({
                        "download_url": download_link,
                        "original_url": url,
                        "message": "Download link generated successfully"
                    }, status=status.HTTP_200_OK)
                else:
                    return Response({
                        "error": "API response doesn't contain download URL",
                        "response": data if settings.DEBUG else None
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                return Response(
                    {"error": f"Download API error: {response.status_code}"}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"Download error: {e}", exc_info=True)
            return Response(
                {"error": f"Download resolution error: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
