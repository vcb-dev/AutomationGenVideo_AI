"""Backend clone/TTS: chỉ GPU local. MiniMax đã tắt — lỗi thì sửa GPU/env, không fallback."""

from __future__ import annotations

from django.conf import settings


def get_voice_provider() -> str:
    value = (getattr(settings, 'VOICE_PROVIDER', None) or 'local').strip().lower()
    if value != 'local':
        raise RuntimeError(
            f'VOICE_PROVIDER={value!r} đã tắt MiniMax. Đặt VOICE_PROVIDER=local. '
            'Lỗi GPU thì sửa pod/env, không fallback sang MiniMax.'
        )
    return 'local'


def get_clone_service(api_key=None):
    get_voice_provider()
    from video_management.services.local_voice_service import get_local_voice_service
    return get_local_voice_service()


def get_tts_service(api_key=None):
    get_voice_provider()
    from video_management.services.local_voice_service import get_local_voice_service
    return get_local_voice_service()
