"""Clone giọng + TTS chạy local trên GPU (F5-TTS), không gọi MiniMax.

Clone = lưu file mẫu + transcript (Whisper nếu thiếu prompt_text).
TTS = F5-TTS zero-shot từ file mẫu đó. Giọng MiniMax cũ không dùng được —
user phải clone lại trên server GPU.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import uuid
from typing import Any, Dict, Optional

from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

_INSTANCE = None
_INFER_LOCK = threading.Lock()


def voice_store_dir(voice_id: str) -> str:
    root = getattr(settings, 'LOCAL_VOICE_DIR', '') or os.path.join(
        default_storage.location, 'local_voices'
    )
    path = os.path.join(root, voice_id)
    os.makedirs(path, exist_ok=True)
    return path


def _run_ffmpeg(args: list[str]) -> None:
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or '').strip() or f'exit {result.returncode}'
        raise RuntimeError(f'ffmpeg thất bại: {err}')


def _to_wav_24k_mono(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _run_ffmpeg(['-i', src, '-ac', '1', '-ar', '24000', dest])


def _wav_to_mp3(src: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _run_ffmpeg(['-i', src, '-codec:a', 'libmp3lame', '-b:a', '192k', dest])


def _wav_duration(path: str) -> float:
    import wave
    with wave.open(path, 'rb') as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)


class LocalVoiceService:
    """Cùng contract clone_voice_from_file / generate_audio / delete_voice với MiniMax."""

    def __init__(self):
        self._whisper = None
        self._f5 = None
        self.model = getattr(settings, 'LOCAL_F5_HF_REPO', '') or 'hynt/F5-TTS-Vietnamese-ViVoice'

    def clone_voice_from_file(
        self,
        audio_path: str,
        voice_name: str,
        prompt_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not audio_path or not os.path.isfile(audio_path):
            raise FileNotFoundError(f'Không thấy file mẫu: {audio_path}')
        size = os.path.getsize(audio_path)
        if size > 20 * 1024 * 1024:
            raise ValueError('File mẫu phải nhỏ hơn 20MB')

        voice_id = f"local_{uuid.uuid4().hex[:16]}"
        store = voice_store_dir(voice_id)
        ref_wav = os.path.join(store, 'reference.wav')
        _to_wav_24k_mono(audio_path, ref_wav)

        transcript = (prompt_text or '').strip() or self._transcribe(ref_wav)
        if not transcript:
            raise RuntimeError(
                'Không đọc được lời trong file mẫu. Hãy upload đoạn nói rõ 10–30 giây, hoặc gửi kèm prompt_text.'
            )
        with open(os.path.join(store, 'reference.txt'), 'w', encoding='utf-8') as handle:
            handle.write(transcript)

        logger.info(f"[Local Voice] cloned {voice_name} -> {voice_id} ({size} bytes, transcript={len(transcript)} chars)")
        return {'voice_id': voice_id, 'transcript': transcript}

    def delete_voice(self, voice_id: str) -> Dict[str, Any]:
        root = getattr(settings, 'LOCAL_VOICE_DIR', '') or os.path.join(
            default_storage.location, 'local_voices'
        )
        store = os.path.join(root, voice_id)
        if os.path.isdir(store):
            shutil.rmtree(store, ignore_errors=True)
            return {'deleted': True}
        return {'deleted': False, 'reason': 'voice files not found'}

    def generate_audio(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        vol: float = 1.0,
        pitch: int = 0,
        emotion: Optional[str] = None,
        language_boost: Optional[str] = None,
        output_path: Optional[str] = None,
        model: Optional[str] = None,
        **_kwargs,
    ) -> Dict[str, Any]:
        del vol, pitch, emotion, language_boost, model
        if not text or not str(text).strip():
            raise ValueError('text is required')
        if not voice_id:
            raise ValueError('voice_id is required')

        store = os.path.join(
            getattr(settings, 'LOCAL_VOICE_DIR', '') or os.path.join(default_storage.location, 'local_voices'),
            voice_id,
        )
        ref_wav = os.path.join(store, 'reference.wav')
        ref_txt_path = os.path.join(store, 'reference.txt')
        if not os.path.isfile(ref_wav):
            raise FileNotFoundError(
                f'Giọng "{voice_id}" chưa có file mẫu trên server GPU. '
                'Giọng clone bằng MiniMax không dùng được — hãy clone lại trong Tiện ích.'
            )
        ref_text = ''
        if os.path.isfile(ref_txt_path):
            with open(ref_txt_path, encoding='utf-8') as handle:
                ref_text = handle.read().strip()
        if not ref_text:
            ref_text = self._transcribe(ref_wav)

        if not output_path:
            raise ValueError('output_path is required')

        tmp_wav = output_path.rsplit('.', 1)[0] + '.wav'
        duration = self._synthesize(
            text=str(text).strip(),
            ref_wav=ref_wav,
            ref_text=ref_text,
            speed=max(0.5, min(2.0, float(speed or 1.0))),
            output_wav=tmp_wav,
        )
        _wav_to_mp3(tmp_wav, output_path)
        if os.path.isfile(tmp_wav) and tmp_wav != output_path:
            os.remove(tmp_wav)

        return {
            'success': True,
            'audio_url': output_path,
            'file_path': output_path,
            'duration': duration,
            'extra_info': {'usage_characters': len(str(text)), 'provider': 'local'},
        }

    def _transcribe(self, wav_path: str) -> str:
        model = self._load_whisper()
        segments, _info = model.transcribe(wav_path, language='vi')
        return ' '.join(segment.text.strip() for segment in segments).strip()

    def _load_whisper(self):
        if self._whisper is not None:
            return self._whisper
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                'Chưa cài faster-whisper. Trên RunPod: pip install -r requirements_gpu.txt'
            ) from exc
        name = getattr(settings, 'LOCAL_WHISPER_MODEL', None) or 'small'
        device = 'cuda' if _cuda_available() else 'cpu'
        compute = 'float16' if device == 'cuda' else 'int8'
        logger.info(f"[Local Voice] loading Whisper {name} on {device}")
        self._whisper = WhisperModel(name, device=device, compute_type=compute)
        return self._whisper

    def _synthesize(self, text: str, ref_wav: str, ref_text: str, speed: float, output_wav: str) -> float:
        engine = self._load_f5()
        with _INFER_LOCK:
            wav, sr, _spec = engine.infer(
                ref_file=ref_wav,
                ref_text=ref_text,
                gen_text=text,
                speed=speed,
            )
        try:
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError('Chưa cài soundfile (requirements_gpu.txt)') from exc
        os.makedirs(os.path.dirname(output_wav), exist_ok=True)
        sf.write(output_wav, wav, sr)
        try:
            return float(len(wav) / float(sr or 1))
        except TypeError:
            return _wav_duration(output_wav)

    def _load_f5(self):
        if self._f5 is not None:
            return self._f5
        try:
            from f5_tts.api import F5TTS
        except ImportError as exc:
            raise RuntimeError(
                'Chưa cài f5-tts. Trên RunPod: pip install -r requirements_gpu.txt rồi restart pod.'
            ) from exc

        model_dir = getattr(settings, 'LOCAL_MODEL_DIR', '') or os.path.join(
            default_storage.location, 'local_models'
        )
        os.makedirs(model_dir, exist_ok=True)
        ckpt = getattr(settings, 'LOCAL_F5_CKPT', '') or ''
        vocab = getattr(settings, 'LOCAL_F5_VOCAB', '') or ''
        repo = getattr(settings, 'LOCAL_F5_HF_REPO', '') or 'hynt/F5-TTS-Vietnamese-ViVoice'

        if not ckpt:
            ckpt, vocab = _ensure_hf_checkpoint(repo, model_dir, vocab)

        device = 'cuda' if _cuda_available() else 'cpu'
        logger.info(f"[Local Voice] loading F5-TTS repo={repo} device={device} ckpt={ckpt}")
        kwargs = {'device': device}
        if ckpt:
            kwargs['ckpt_file'] = ckpt
        if vocab:
            kwargs['vocab_file'] = vocab
        try:
            self._f5 = F5TTS(**kwargs)
        except TypeError:
            self._f5 = F5TTS(model='F5TTS_v1_Base', **{k: v for k, v in kwargs.items() if k != 'device'})
        return self._f5


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def _ensure_hf_checkpoint(repo: str, model_dir: str, vocab: str) -> tuple[str, str]:
    local_dir = os.path.join(model_dir, repo.replace('/', '__'))
    os.makedirs(local_dir, exist_ok=True)
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo, local_dir=local_dir)
    except Exception as exc:
        logger.warning(f"[Local Voice] không tải được {repo}: {exc}. Thử checkpoint có sẵn trong {local_dir}")

    ckpt = None
    for name in ('model_last.pt', 'model.pt', 'model_1200000.pt'):
        candidate = _find_file(local_dir, (name,))
        if candidate:
            ckpt = candidate
            break
    if not ckpt:
        ckpt = _find_file(local_dir, ('.pt', '.safetensors'))
    if not vocab:
        vocab = _find_file(local_dir, ('vocab.txt',)) or ''
        if vocab and not vocab.endswith('vocab.txt'):
            maybe = os.path.join(os.path.dirname(vocab), 'vocab.txt')
            vocab = maybe if os.path.isfile(maybe) else vocab
    if not ckpt:
        raise RuntimeError(
            f'Không thấy checkpoint F5-TTS trong {local_dir}. '
            f'Set LOCAL_F5_CKPT hoặc tải model {repo} vào volume /workspace/models.'
        )
    return ckpt, vocab


def _find_file(root: str, suffixes: tuple[str, ...]) -> Optional[str]:
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            lower = name.lower()
            if any(lower.endswith(suffix) if suffix.startswith('.') else lower == suffix for suffix in suffixes):
                return os.path.join(dirpath, name)
    return None


def get_local_voice_service() -> LocalVoiceService:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LocalVoiceService()
    return _INSTANCE
