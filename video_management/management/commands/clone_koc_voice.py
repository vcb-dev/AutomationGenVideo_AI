"""
Management command to clone KOC voice from audio file.

Usage:
    python manage.py clone_koc_voice <audio_file_path> <voice_name> --gender <male|female> [--voice-id <custom_id>] [--prompt <prompt_audio_path>]

Example:
    python manage.py clone_koc_voice "D:\Voices\koc_nguyen_van_a.mp3" "KOC Nguyen Van A" --gender male
    python manage.py clone_koc_voice "D:\Voices\koc.wav" "KOC Voice" --gender female --voice-id "koc_custom_001" --prompt "D:\Voices\prompt.wav"
"""

from django.core.management.base import BaseCommand
from video_management.services.minimax_voice_clone_service import get_voice_clone_service
from video_management.models import Voice
import os


class Command(BaseCommand):
    help = 'Clone KOC voice from audio file using Minimax API'

    def add_arguments(self, parser):
        parser.add_argument(
            'audio_file',
            type=str,
            help='Path to audio file (mp3/wav/m4a, 10s-5min, max 20MB)'
        )
        parser.add_argument(
            'voice_name',
            type=str,
            help='Name for the cloned voice (e.g., "KOC Nguyen Van A")'
        )
        parser.add_argument(
            '--voice-id',
            type=str,
            default=None,
            help='Custom voice ID (optional, will be auto-generated if not provided)'
        )
        parser.add_argument(
            '--prompt',
            type=str,
            default=None,
            help='Path to prompt audio file (<8s) for better quality (optional)'
        )
        parser.add_argument(
            '--gender',
            type=str,
            required=True,
            choices=['male', 'female'],
            help='Gender of the voice'
        )
        parser.add_argument(
            '--language',
            type=str,
            default='vi',
            help='Language code (default: vi)'
        )

    def handle(self, *args, **options):
        """Clone voice from audio file."""
        
        audio_file = options['audio_file']
        voice_name = options['voice_name']
        voice_id = options['voice_id']
        prompt_file = options['prompt']
        gender = options['gender']
        language = options['language']
        
        self.stdout.write("=" * 60)
        self.stdout.write(self.style.SUCCESS('[Minimax Voice Cloning]'))
        self.stdout.write("=" * 60)
        
        # Validate audio file
        if not os.path.exists(audio_file):
            self.stdout.write(
                self.style.ERROR(f'[ERROR] Audio file not found: {audio_file}')
            )
            return
        
        # Check file size
        file_size = os.path.getsize(audio_file)
        file_size_mb = file_size / (1024 * 1024)
        
        self.stdout.write(f'\n[INFO] Audio file: {audio_file}')
        self.stdout.write(f'[INFO] File size: {file_size_mb:.2f} MB')
        self.stdout.write(f'[INFO] Voice name: {voice_name}')
        
        if voice_id:
            self.stdout.write(f'[INFO] Custom voice ID: {voice_id}')
        
        if file_size > 20 * 1024 * 1024:
            self.stdout.write(
                self.style.ERROR('[ERROR] File size must be less than 20MB')
            )
            return
        
        # Validate prompt file (optional)
        if prompt_file:
            if not os.path.exists(prompt_file):
                self.stdout.write(
                    self.style.ERROR(f'[ERROR] Prompt file not found: {prompt_file}')
                )
                return
            
            prompt_size_mb = os.path.getsize(prompt_file) / (1024 * 1024)
            self.stdout.write(f'[INFO] Prompt file: {prompt_file}')
            self.stdout.write(f'[INFO] Prompt size: {prompt_size_mb:.2f} MB')
        
        try:
            # Initialize service
            self.stdout.write('\n[STEP 1] Initializing Minimax service...')
            service = get_voice_clone_service()
            self.stdout.write(self.style.SUCCESS('[OK] Service initialized'))
            
            # Clone voice
            self.stdout.write('\n[STEP 2] Cloning voice (this may take 10-30 seconds)...')
            result = service.clone_voice_from_file(
                audio_path=audio_file,
                voice_name=voice_name,
                voice_id=voice_id,
                prompt_audio_path=prompt_file
            )
            
            self.stdout.write(self.style.SUCCESS('[OK] Voice cloned successfully!'))
            self.stdout.write(f'[INFO] Voice ID: {result["voice_id"]}')
            
            # Save to database
            self.stdout.write('\n[STEP 3] Saving to database...')
            voice, created = Voice.objects.update_or_create(
                voice_id=result['voice_id'],
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
                    self.style.SUCCESS(f'[OK] Created new voice in database (ID: {voice.id})')
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f'[OK] Updated existing voice in database (ID: {voice.id})')
                )
            
            # Summary
            self.stdout.write("\n" + "=" * 60)
            self.stdout.write(self.style.SUCCESS('[SUCCESS] Voice cloning complete!'))
            self.stdout.write("=" * 60)
            self.stdout.write(f'\nVoice ID: {result["voice_id"]}')
            self.stdout.write(f'Voice Name: {voice_name}')
            self.stdout.write(f'Database ID: {voice.id}')
            self.stdout.write(f'Provider: minimax')
            self.stdout.write(f'Expires at: {result["expires_at"]} (Unix timestamp)')
            self.stdout.write('\n[NOTE] Cloned voice expires in 7 days (168 hours)')
            self.stdout.write('[NOTE] Use the voice at least once within 7 days to make it permanent')
            self.stdout.write('\n[NEXT] You can now use this voice in the web interface!')
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'\n[ERROR] Voice cloning failed: {str(e)}')
            )
            import traceback
            self.stdout.write(traceback.format_exc())
