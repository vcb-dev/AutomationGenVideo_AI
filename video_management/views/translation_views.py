"""
Translation API Views
Provides real-time translation for search keywords.
"""

import logging
import requests
import json
from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

logger = logging.getLogger(__name__)

@api_view(['POST'])
@permission_classes([AllowAny])
def translate_to_chinese(request):
    """
    Translate Vietnamese/English text to Chinese (Simplified) using Gemini.
    Optimized for short search keywords.
    """
    text = request.data.get('text', '').strip()
    if not text:
        return Response({'success': True, 'translated': ''})

    # Skip if text is already mostly Chinese characters
    if any('\u4e00' <= char <= '\u9fff' for char in text):
        return Response({'success': True, 'translated': text, 'source': 'already_chinese'})

    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    
    # ── Phase 1: Try Gemini (Smart, localized) ─────────────────────────────
    if api_key:
        try:
            import google.generativeai as genai
            
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-2.5-flash')
            prompt = f"""Bạn là máy dịch thuật tốt nhất. Dịch cụm từ tìm kiếm sau từ tiếng Việt sang tiếng Trung Quốc Giản thể.
Chỉ trả về bản dịch, không giải thích.
Cụm từ: "{text}"
Dịch:"""

            response = model.generate_content(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 50}
            )
            translated = response.text.strip().replace('"', '').replace("'", "")
            if translated and translated.lower() != text.lower():
                return Response({'success': True, 'translated': translated, 'source': 'gemini'})
        except Exception as e:
            logger.warning(f"Gemini translation failed: {e}")

    # ── Phase 2: Fallback to Google Translate (Free, Reliable) ──────────────
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "vi",  # Vietnamese
            "tl": "zh-CN",  # Chinese Simplified
            "dt": "t",
            "q": text
        }
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            # Google Translate returns nested lists: [[["translated", "original", ...]]]
            if data and len(data) > 0 and data[0] and len(data[0]) > 0:
                translated = data[0][0][0]
                if translated:
                    logger.info(f"Google Translated: '{text}' -> '{translated}'")
                    return Response({
                        'success': True,
                        'original': text,
                        'translated': translated,
                        'source': 'google_translate'
                    })
    except Exception as e:
        logger.error(f"Google Translate fallback failed: {e}")

    return Response({'success': False, 'translated': text, 'error': 'All translation methods failed'})
