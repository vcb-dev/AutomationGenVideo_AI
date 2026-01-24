from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.http import HttpResponse
import urllib.parse
import requests
import logging

logger = logging.getLogger(__name__)

class ProxyImageView(APIView):
    """
    Proxy endpoint to handle 403 Forbidden when loading Douyin images
    """
    @method_decorator(csrf_exempt, name='dispatch')
    def get(self, request):
        try:
            url = request.GET.get('url')
            if not url:
                return Response({"error": "Missing url parameter"}, status=status.HTTP_400_BAD_REQUEST)
            
            url = urllib.parse.unquote(url)
            logger.info(f"Proxy image request: {url}")
            
            headers = {
                'Referer': 'https://www.douyin.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=10.0, stream=True)
            
            if response.status_code == 200:
                http_response = HttpResponse(
                    response.content, 
                    content_type=response.headers.get('Content-Type', 'image/jpeg')
                )
                http_response['Cache-Control'] = 'public, max-age=3600'
                return http_response
            else:
                return Response({"error": f"Failed to fetch image: {response.status_code}"}, status=response.status_code)
                
        except Exception as e:
            logger.error(f"Proxy image error: {e}", exc_info=True)
            return Response({"error": f"Proxy error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProxyVideoView(APIView):
    """
    Proxy endpoint to handle 403 Forbidden when playing Douyin videos
    """
    @method_decorator(csrf_exempt, name='dispatch')
    def get(self, request):
        try:
            url = request.GET.get('url')
            if not url:
                return Response({"error": "Missing url parameter"}, status=status.HTTP_400_BAD_REQUEST)
            
            url = urllib.parse.unquote(url)
            logger.info(f"Proxy video request: {url}")
            
            headers = {
                'Referer': 'https://www.douyin.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Range': request.META.get('HTTP_RANGE', '')
            }
            
            response = requests.get(url, headers=headers, timeout=30.0, stream=True)
            
            if response.status_code in [200, 206]:
                http_response = HttpResponse(
                    response.content, 
                    content_type=response.headers.get('Content-Type', 'video/mp4')
                )
                
                # Support range requests
                if 'Content-Range' in response.headers:
                    http_response['Content-Range'] = response.headers['Content-Range']
                if 'Accept-Ranges' in response.headers:
                    http_response['Accept-Ranges'] = response.headers['Accept-Ranges']
                
                http_response['Cache-Control'] = 'public, max-age=3600'
                http_response.status_code = response.status_code
                return http_response
            else:
                return Response({"error": f"Failed to fetch video: {response.status_code}"}, status=response.status_code)
                
        except Exception as e:
            logger.error(f"Proxy video error: {e}", exc_info=True)
            return Response({"error": f"Proxy error: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
