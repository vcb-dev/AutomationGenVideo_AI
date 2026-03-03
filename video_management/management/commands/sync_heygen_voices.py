"""
Sync HeyGen voices: remove invalid voices, add missing valid voices from HeyGen API.

Usage:
    python manage.py sync_heygen_voices

- Removes local Voice records (provider='heygen') whose voice_id no longer exists on HeyGen.
- Adds valid HeyGen voices (Vietnamese + multilingual) that are missing in DB.
"""

import os
import requests
from django.core.management.base import BaseCommand
from video_management.models import Voice


class Command(BaseCommand):
    help = 'Sync HeyGen voices: remove invalid, add missing valid voices'

    def handle(self, *args, **options):
        api_key = os.getenv('HEYGEN_API_KEY')
        if not api_key:
            self.stderr.write(self.style.ERROR('HEYGEN_API_KEY not set in environment'))
            return

        try:
            resp = requests.get(
                'https://api.heygen.com/v2/voices',
                headers={'X-Api-Key': api_key, 'Content-Type': 'application/json'},
                timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            api_voices = data.get('data', {}).get('voices', [])
        except Exception as e:
            self.stderr.write(self.style.ERROR(f'HeyGen API error: {e}'))
            return

        if not api_voices:
            self.stdout.write('No voices returned from HeyGen API.')
            return

        valid_ids = {v.get('voice_id') for v in api_voices if v.get('voice_id')}
        api_map = {v['voice_id']: v for v in api_voices if v.get('voice_id')}

        # 1. Remove invalid HeyGen voices from DB
        heygen_voices = list(Voice.objects.filter(provider='heygen'))
        removed = 0
        for v in heygen_voices:
            if v.voice_id not in valid_ids:
                self.stdout.write('Removing invalid: %s' % v.voice_id)
                v.delete()
                removed += 1

        # 2. Add missing valid voices (Vietnamese + Multilingual)
        added = 0
        for v in api_voices:
            vid = v.get('voice_id')
            if not vid:
                continue
            lang = (v.get('language') or '').lower()
            if 'vietnam' not in lang and 'vi' not in lang and 'multilingual' not in lang:
                continue
            if Voice.objects.filter(voice_id=vid).exists():
                continue
            gender = (v.get('gender') or 'unknown').lower()
            if gender not in ('male', 'female'):
                gender = 'male'
            Voice.objects.create(
                name=v.get('name', vid),
                voice_id=vid,
                provider='heygen',
                is_cloned=False,
                is_system=True,
                language='vi' if 'vi' in lang or 'vietnam' in lang else 'mul',
                gender=gender
            )
            self.stdout.write('Added: %s' % vid)
            added += 1

        self.stdout.write(self.style.SUCCESS(f'Done. Removed {removed}, added {added} voice(s).'))
