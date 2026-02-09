"""
Test script for Minimax TTS service.

Run: python test_minimax_tts.py
"""

import os
import sys
import django

# Fix Windows console encoding
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Setup Django
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.services.minimax_tts_service import get_minimax_service


def test_minimax_tts():
    """Test Minimax TTS generation."""
    
    print("=" * 50)
    print("Testing Minimax TTS Service")
    print("=" * 50)
    
    # Test data
    test_text = "Xin chào! Đây là giọng nói Moss từ Minimax AI. Tôi đang test text-to-speech."
    voice_id = "moss_audio_ce3450f9-c782-11f0-a527-aab150a40f84"
    
    print(f"\nText: {test_text}")
    print(f"Voice ID: {voice_id}")
    print(f"Text length: {len(test_text)} chars\n")
    
    try:
        # Initialize service
        service = get_minimax_service()
        print("[INFO] Minimax service initialized")
        
        # Generate audio
        print("[INFO] Generating audio...")
        result = service.generate_audio(
            text=test_text,
            voice_id=voice_id,
            speed=1.0,
            vol=1.0,
            pitch=0,
            emotion="happy"
        )
        
        print("\n[SUCCESS] Audio generated!")
        print(f"Audio URL: {result['audio_url'][:100]}...")
        print(f"Duration: {result.get('duration', 0)} seconds")
        print(f"Extra info: {result.get('extra_info', {})}")
        
        if result.get('file_path'):
            print(f"Saved to: {result['file_path']}")
        
        return True
        
    except Exception as e:
        print(f"\n[ERROR] {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = test_minimax_tts()
    sys.exit(0 if success else 1)
