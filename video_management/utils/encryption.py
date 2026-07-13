"""
Encryption/decryption utilities for sensitive data (tokens, etc.).

Uses cryptography.fernet for symmetric encryption.
"""

import os
from cryptography.fernet import Fernet
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class TokenEncryption:
    """Handle encryption and decryption of access tokens."""
    
    _cipher = None
    
    @classmethod
    def get_cipher(cls):
        """Get or create cipher instance."""
        if cls._cipher is None:
            key = getattr(settings, 'FERNET_KEY', None)
            if not key:
                raise ValueError("FERNET_KEY not configured in settings")
            cls._cipher = Fernet(key)
        return cls._cipher
    
    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """
        Encrypt plaintext string.
        
        Args:
            plaintext: The string to encrypt
            
        Returns:
            Encrypted string (base64-encoded)
        """
        if not plaintext:
            return ''
        
        try:
            cipher = cls.get_cipher()
            encrypted_bytes = cipher.encrypt(plaintext.encode('utf-8'))
            return encrypted_bytes.decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption failed: {str(e)}")
            raise
    
    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        """
        Decrypt ciphertext string.
        
        Args:
            ciphertext: The encrypted string (base64-encoded)
            
        Returns:
            Decrypted plaintext string
        """
        if not ciphertext:
            return ''
        
        try:
            cipher = cls.get_cipher()
            decrypted_bytes = cipher.decrypt(ciphertext.encode('utf-8'))
            return decrypted_bytes.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {str(e)}")
            raise


def generate_encryption_key():
    """Generate a new encryption key for settings."""
    return Fernet.generate_key().decode('utf-8')
