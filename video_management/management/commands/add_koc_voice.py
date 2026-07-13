"""
Management command to clone KOC voice and add to database.

Usage:
    python manage.py add_koc_voice --audio "path/to/audio.mp3" --name "KOC Name" --gender male
    
Example:
    python manage.py add_koc_voice --audio "C:/voices/koc_voice.mp3" --name "Chi Bao KOC" --gender male --language vi
"""

from django.core.management.base import BaseCommand
from video_management.models import Voice
from video_management.services.minimax_voice_clone_service import get_voice_clone_service
import os


class Command(BaseCommand):
    help = 'Clone KOC voice using Minimax and add to database'

    def add_arguments(self, parser):
        parser.add_argument(
            '--audio',
            type=str,
            required=True,
            help='Path to audio file (mp3, m4a, wav, 10s-5min, max 20MB)'
        )
        parser.add_argument(
            '--name',
            type=str,
            required=True,
            help='Name for the voice (e.g., "Chi Bao KOC")'
        )
        parser.add_argument(
            '--gender',
            type=str,
            choices=['male', 'female'],
            required=True,
            help='Gender of the voice'
        )
        parser.add_argument(
            '--language',
            type=str,
            default='vi',
            help='Language code (default: vi)'
        )
        parser.add_argument(
            '--prompt',
            type=str,
            required=False,
            help='Optional: Path to prompt audio (<8s) for better quality'
        )
        parser.add_argument(
            '--voice-id',
            type=str,
            required=False,
            help='Optional: Custom voice ID (will be generated if not provided)'
        )

    def handle(self, *args, **options):
        """Clone voice and add to database."""
        
        audio_path = options['audio']
        voice_name = options['name']
        gender = options['gender']
        language = options['language']
        prompt_path = options.get('prompt')
        custom_voice_id = options.get('voice_id')
        
        self.stdout.write('=' * 60)
        self.stdout.write(self.style.SUCCESS('[MINIMAX VOICE CLONE]'))
        self.stdout.write('=' * 60)
        
        # Check audio file exists
        if not os.path.exists(audio_path):
            self.stdout.write(
                self.style.ERROR(f'[ERROR] Audio file not found: {audio_path}')
            )
            return
        
        # Check prompt file if provided
        if prompt_path and not os.path.exists(prompt_path):
            self.stdout.write(
                self.style.ERROR(f'[ERROR] Prompt audio file not found: {prompt_path}')
            )
            return
        
        # Display info
        self.stdout.write(f'\n[INFO] Voice Name: {voice_name}')
        self.stdout.write(f'[INFO] Audio File: {audio_path}')
        self.stdout.write(f'[INFO] Gender: {gender}')
        self.stdout.write(f'[INFO] Language: {language}')
        if prompt_path:
            self.stdout.write(f'[INFO] Prompt Audio: {prompt_path}')
        if custom_voice_id:
            self.stdout.write(f'[INFO] Custom Voice ID: {custom_voice_id}')
        
        try:
            # Clone voice using Minimax
            self.stdout.write(f'\n[STEP 1] Uploading audio to Minimax...')
            
            service = get_voice_clone_service()
            result = service.clone_voice_from_file(
                audio_path=audio_path,
                voice_name=voice_name,
                voice_id=custom_voice_id,
                prompt_audio_path=prompt_path
            )
            
            voice_id = result['voice_id']
            expires_at = result.get('expires_at')
            
            self.stdout.write(
                self.style.SUCCESS(f'[SUCCESS] Voice cloned! voice_id: {voice_id}')
            )
            
            # Add to database
            self.stdout.write(f'\n[STEP 2] Adding to database...')
            
            voice, created = Voice.objects.update_or_create(
                voice_id=voice_id,
                defaults={
                    'name': voice_name,
                    'provider': 'minimax',
                    'language': language,
                    'gender': gender,
                    'is_cloned': True,
                    'is_system': False
                }
            )
            
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f'[SUCCESS] Voice added to database!')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'[WARNING] Voice already exists, updated!')
                )
            
            # Summary
            self.stdout.write('\n' + '=' * 60)
            self.stdout.write(self.style.SUCCESS('[COMPLETE]'))
            self.stdout.write('=' * 60)
            self.stdout.write(f'Voice ID: {voice_id}')
            self.stdout.write(f'Voice Name: {voice_name}')
            self.stdout.write(f'Provider: minimax')
            self.stdout.write(f'Status: {"Created" if created else "Updated"}')
            self.stdout.write(f'\nIMPORTANT: Voice is temporary for 168 hours (7 days).')
            self.stdout.write(f'Use it at least once in TTS to make it permanent!')
            self.stdout.write('=' * 60)
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'\n[ERROR] {str(e)}')
            )
            import traceback
            self.stdout.write(traceback.format_exc())
