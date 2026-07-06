"""
Django management command to generate encryption key for token storage.

Usage:
    python manage.py generate_encryption_key
    
Output:
    Prints the encryption key that should be added to .env as ENCRYPTION_KEY
"""

from django.core.management.base import BaseCommand
from video_management.utils.encryption import generate_encryption_key


class Command(BaseCommand):
    help = 'Generate a new encryption key for token storage'

    def handle(self, *args, **options):
        key = generate_encryption_key()
        
        self.stdout.write(self.style.SUCCESS('\n✅ New Encryption Key Generated:\n'))
        self.stdout.write(self.style.WARNING(f'{key}\n'))
        
        self.stdout.write(self.style.SUCCESS('📝 Add this to your .env file:\n'))
        self.stdout.write(self.style.WARNING(f'ENCRYPTION_KEY={key}\n'))
        
        self.stdout.write(self.style.SUCCESS('⚠️  Important:\n'))
        self.stdout.write('- Keep this key SECRET and secure\n')
        self.stdout.write('- Do NOT commit to version control\n')
        self.stdout.write('- Use the same key across all servers\n')
        self.stdout.write('- Do NOT change this key after encrypting tokens\n')
