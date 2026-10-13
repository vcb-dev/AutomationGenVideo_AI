"""Chọn backend clone/TTS: MiniMax API hoặc model local trên GPU (RunPod)."""

from __future__ import annotations

from django.conf import settings


def get_voice_provider() -> str:
    """`local` = F5-TTS trên GPU, `minimax` = API trả phí. Mặc định minimax
    để Railway không đổi hành vi; Dockerfile.runpod set VOICE_PROVIDER=local."""
    value = (getattr(settings, 'VOICE_PROVIDER', None) or 'minimax').strip().lower()
    return 'local' if value == 'local' else 'minimax'


def get_clone_service(api_key=None):
    if get_voice_provider() == 'local':
        from video_management.services.local_voice_service import get_local_voice_service
        return get_local_voice_service()
    from video_management.services.minimax_voice_clone_service import get_voice_clone_service
    return get_voice_clone_service(api_key=api_key)


def get_tts_service(api_key=None):
    if get_voice_provider() == 'local':
        from video_management.services.local_voice_service import get_local_voice_service
        return get_local_voice_service()
    from video_management.services.minimax_tts_service import get_minimax_service
    return get_minimax_service(api_key=api_key)
