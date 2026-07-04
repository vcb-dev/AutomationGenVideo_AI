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

    anthropic_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    
    # ── Phase 1: Try Claude (Smart, localized) ─────────────────────────────
    if anthropic_key and not anthropic_key.startswith('your_'):
        try:
            from anthropic import Anthropic
            client = Anthropic(api_key=anthropic_key)
            
            prompt = f"""Bạn là máy dịch thuật tốt nhất. Dịch cụm từ tìm kiếm sau từ tiếng Việt sang tiếng Trung Quốc Giản thể.
Chỉ trả về bản dịch, không giải thích.
Cụm từ: "{text}"
Dịch:"""

            models = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]
            response = None
            for m in models:
                try:
                    kwargs = {
                        "model": m,
                        "max_tokens": 50,
                        "messages": [{"role": "user", "content": prompt}]
                    }
                    if "opus-4-7" not in m:
                        kwargs["temperature"] = 0.0
                    
                    response = client.messages.create(**kwargs)
                    break
                except Exception as e:
                    if "not_found_error" in str(e).lower() and m != models[-1]:
                        continue
                    raise e
            translated = response.content[0].text.strip().replace('"', '').replace("'", "")
            if translated and translated.lower() != text.lower():
                return Response({'success': True, 'translated': translated, 'source': 'claude'})
        except Exception as e:
            logger.warning(f"Claude translation failed: {e}")

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
