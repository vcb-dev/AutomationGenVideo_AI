
import os
import requests
import uuid
from typing import Optional, Dict, Any, List
from django.conf import settings
from elevenlabs import ElevenLabs, save
from elevenlabs.client import ElevenLabs

# Note: ElevenLabs SDK usage has changed in v1.0+
# We will use the client class pattern

class ElevenLabsService:
    """
    Service to interact with ElevenLabs API for Voice Cloning and TTS.
    """
    
    def __init__(self):
        self.api_key = settings.ELEVENLABS_API_KEY
        if not self.api_key:
            # Fallback to os.getenv just in case
            self.api_key = os.getenv("ELEVENLABS_API_KEY")
            
        if not self.api_key:
            print("WARNING: ELEVENLABS_API_KEY is missing")
            
        self.client = ElevenLabs(
            api_key=self.api_key,
        )

    def clone_voice(self, name: str, audio_paths: List[str], description: str = "") -> Dict[str, Any]:
        """
        Instant Voice Cloning using one or more audio files.
        """
        try:
            # Add validation for file existence
            valid_paths = [p for p in audio_paths if os.path.exists(p)]
            if not valid_paths:
                raise ValueError("No valid audio files provided for cloning")
                
            # Perform cloning
            # In new SDK, we operate with the `ivc.create` method for Instant Voice Cloning
            voice = self.client.voices.ivc.create(
                name=name,
                description=description,
                files=valid_paths
            )
            
            return {
                "voice_id": voice.voice_id,
                "name": voice.name,
                "category": voice.category,
                "provider": "elevenlabs"
            }
            
        except Exception as e:
            print(f"ElevenLabs Cloning Error: {str(e)}")
            raise

    def generate_audio(self, text: str, voice_id: str, output_path: Optional[str] = None) -> str:
        """
        Generate audio from text using a specific voice_id.
        Returns the file path of the generated audio.
        """
        try:
            audio_generator = self.client.generate(
                text=text,
                voice=voice_id,
                model="eleven_multilingual_v2"
            )
            
            # Save logic
            if not output_path:
                filename = f"tts_{uuid.uuid4()}.mp3"
                output_dir = os.path.join(settings.MEDIA_ROOT, "generated_audio")
                os.makedirs(output_dir, exist_ok=True)
                output_path = os.path.join(output_dir, filename)
                
            # The SDK returns a generator or bytes depending on configuration
            # 'save' utility from elevenlabs might be needed or manual write
            save(audio_generator, output_path)
            
            return output_path
            
        except Exception as e:
            print(f"ElevenLabs TTS Error: {str(e)}")
            raise

    def get_voices(self) -> List[Dict[str, Any]]:
        """
        Get list of all available voices (system + cloned).
        """
        try:
            response = self.client.voices.get_all()
            voices_data = []
            
            for voice in response.voices:
                voices_data.append({
                    "voice_id": voice.voice_id,
                    "name": voice.name,
                    "category": voice.category,
                    "preview_url": voice.preview_url,
                    "is_cloned": voice.category == 'cloned'
                })
                
            return voices_data
            
        except Exception as e:
            print(f"ElevenLabs Get Voices Error: {str(e)}")
            return []

    def delete_voice(self, voice_id: str) -> bool:
        """Delete a voice by ID"""
        try:
            self.client.voices.delete(voice_id)
            return True
        except Exception:
            return False
