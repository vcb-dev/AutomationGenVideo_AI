"""TTS/clone local trên GPU: không gọi MiniMax khi VOICE_PROVIDER=local.

Chạy: python manage.py test tests.test_local_voice
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from video_management.services.voice_backend import get_clone_service, get_tts_service, get_voice_provider
from video_management.views import voice_views


def _voice(name='KOC Lan', voice_id='local_abc', provider='local', is_cloned=True, is_system=False):
    v = MagicMock()
    v.name = name
    v.voice_id = voice_id
    v.provider = provider
    v.is_cloned = is_cloned
    v.is_system = is_system
    v.created_at = None
    return v


class VoiceProviderSwitch(SimpleTestCase):
    @override_settings(VOICE_PROVIDER='local')
    def test_local_flag(self):
        self.assertEqual(get_voice_provider(), 'local')

    @override_settings(VOICE_PROVIDER='minimax')
    def test_minimax_flag(self):
        self.assertEqual(get_voice_provider(), 'minimax')

    @override_settings(VOICE_PROVIDER='local')
    def test_clone_and_tts_use_same_local_service(self):
        with patch('video_management.services.local_voice_service.get_local_voice_service') as local:
            svc = MagicMock()
            local.return_value = svc
            self.assertIs(get_clone_service(api_key='sk-should-ignore'), svc)
            self.assertIs(get_tts_service(api_key='sk-should-ignore'), svc)
            local.assert_called()

    @override_settings(VOICE_PROVIDER='minimax')
    def test_minimax_still_uses_api_key(self):
        with patch(
            'video_management.services.minimax_voice_clone_service.get_voice_clone_service'
        ) as clone, patch(
            'video_management.services.minimax_tts_service.get_minimax_service'
        ) as tts:
            clone.return_value = 'clone'
            tts.return_value = 'tts'
            self.assertEqual(get_clone_service(api_key='sk-api-abc'), 'clone')
            clone.assert_called_once_with(api_key='sk-api-abc')
            self.assertEqual(get_tts_service(api_key='sk-api-abc'), 'tts')
            tts.assert_called_once_with(api_key='sk-api-abc')


class LocalDeleteVoice(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(VOICE_PROVIDER='local')
    def test_delete_local_skips_minimax_and_removes_files(self):
        local_svc = MagicMock()
        local_svc.delete_voice.return_value = {'deleted': True}

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_voice_clone_service') as minimax, \
                patch('video_management.services.local_voice_service.get_local_voice_service', return_value=local_svc):
            Voice.objects.filter.return_value.first.return_value = _voice()
            Voice.objects.filter.return_value.delete.return_value = (1, {})
            request = self.factory.delete('/api/voice/delete/local_abc/')
            res = voice_views.delete_voice_api(request, 'local_abc')

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data['minimax_deleted'])
        minimax.assert_not_called()
        local_svc.delete_voice.assert_called_once_with('local_abc')


class LocalTtsRejectsMinimaxVoice(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(VOICE_PROVIDER='local')
    def test_old_minimax_voice_must_be_recloned(self):
        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'get_tts_service') as tts:
            Voice.objects.filter.return_value.first.return_value = _voice(
                provider='minimax', voice_id='moss_old'
            )
            request = self.factory.post('/api/voice/tts/', {
                'text': 'Xin chào',
                'voice_id': 'moss_old',
            }, format='json')
            res = voice_views.voice_tts_api(request)

        self.assertEqual(res.status_code, 400)
        self.assertIn('clone lại', res.data['error'])
        tts.assert_not_called()


class LocalVoiceServiceFiles(SimpleTestCase):
    def test_clone_writes_reference_transcript(self):
        import os
        import tempfile

        from video_management.services.local_voice_service import LocalVoiceService

        svc = LocalVoiceService()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'in.mp3')
            with open(src, 'wb') as handle:
                handle.write(b'ID3fake')
            with override_settings(LOCAL_VOICE_DIR=tmp), \
                    patch.object(svc, '_transcribe', return_value='xin chao'), \
                    patch('video_management.services.local_voice_service._to_wav_24k_mono') as conv:
                def _fake_convert(_src, dest):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, 'wb') as out:
                        out.write(b'RIFF')
                conv.side_effect = _fake_convert
                result = svc.clone_voice_from_file(src, 'KOC Lan')

            self.assertTrue(result['voice_id'].startswith('local_'))
            store = os.path.join(tmp, result['voice_id'])
            with open(os.path.join(store, 'reference.txt'), encoding='utf-8') as handle:
                self.assertEqual(handle.read(), 'xin chao')

    def test_tts_without_sample_explains_reclone(self):
        import tempfile

        from video_management.services.local_voice_service import LocalVoiceService

        svc = LocalVoiceService()
        with tempfile.TemporaryDirectory() as tmp, override_settings(LOCAL_VOICE_DIR=tmp):
            with self.assertRaises(FileNotFoundError) as raised:
                svc.generate_audio('Xin chao', 'local_missing', output_path='/tmp/out.mp3')
            self.assertIn('clone lại', str(raised.exception))


class LocalCloneStart(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    @override_settings(VOICE_PROVIDER='local')
    def test_start_clone_does_not_require_minimax_key(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        with patch.object(voice_views, 'Voice') as Voice, \
                patch.object(voice_views, 'threading') as threading:
            Voice.objects.filter.return_value.first.return_value = None
            request = self.factory.post('/api/voice/clone/start/', {
                'file': SimpleUploadedFile('mau.mp3', b'ID3fake', content_type='audio/mpeg'),
                'voice_name': 'KOC Lan',
                'gender': 'female',
            }, format='multipart')
            res = voice_views.clone_voice_start_api(request)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['success'])
        threading.Thread.assert_called_once()
