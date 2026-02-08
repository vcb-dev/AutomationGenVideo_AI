import os
import logging
import yt_dlp
from moviepy.editor import VideoFileClip
from django.conf import settings

logger = logging.getLogger(__name__)

class VoiceExtractionService:
    """
    Service to extract audio/voice from KOC videos.
    """
    
    def __init__(self, output_dir="media/temp_voices"):
        self.output_dir = os.path.join(settings.BASE_DIR, output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

    def download_video_audio(self, url: str) -> str:
        """
        Download video first, then extract audio using MoviePy.
        This avoids yt-dlp's strict dependency on system FFmpeg for audio conversion.
        Includes Headers to avoid Timeouts/Blocks from TikTok.
        """
        # Download best single file (usually contains both audio and video for TikTok/Shorts)
        ydl_opts = {
            'format': 'best', 
            'outtmpl': os.path.join(self.output_dir, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.tiktok.com/',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            'socket_timeout': 30, # Increase timeout
            'retries': 3,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                
                # Determine the downloaded video path
                # prepare_filename is reliable
                video_path = ydl.prepare_filename(info)
                
                if not os.path.exists(video_path):
                     # Fallback manual construction if prepare_filename is off (rare)
                     ext = info.get('ext', 'mp4')
                     video_path = os.path.join(self.output_dir, f"{info['id']}.{ext}")

                logger.info(f"Downloaded temp video to: {video_path}")

                # Now extract audio using MoviePy (re-using existing method)
                try:
                    audio_path = self.extract_from_local_file(video_path)
                    
                    # Clean up the large video file to save space
                    if os.path.exists(video_path):
                        os.remove(video_path)
                        
                    return audio_path
                except Exception as extract_error:
                    logger.error(f"MoviePy extraction failed: {extract_error}")
                    raise extract_error

        except Exception as e:
            logger.error(f"Failed to download video for audio extraction: {str(e)}")
            if "Requested format is not available" in str(e):
                 logger.error("yt-dlp could not find a single file format. This usually requires ffmpeg to merge streams.")
            raise

    def extract_from_local_file(self, video_path: str) -> str:
        """
        Extract audio from a local video file using MoviePy.
        """
        try:
            filename = os.path.basename(video_path)
            name, ext = os.path.splitext(filename)
            output_path = os.path.join(self.output_dir, f"{name}.mp3")
            
            video = VideoFileClip(video_path)
            video.audio.write_audiofile(output_path, logger=None)
            video.close()
            
            return output_path
        except Exception as e:
            logger.error(f"Failed to extract audio: {str(e)}")
            raise

    def separate_vocals(self, audio_path: str) -> str:
        """
        Separate vocals from background music.
        Note: This is a placeholder. For real separation, we need 'demucs' or 'spleeter'.
        For now, this just returns the original audio and logs a warning.
        """
        # TODO: Implement Demucs/Spleeter integration here
        # Example command: demucs -n htdemucs --two-stems=vocals <audio_path>
        
        logger.warning("Vocal separation requested but not yet implemented (requires Demucs/Spleeter). Using original audio.")
        return audio_path


    def create_voice_clone(self, audio_path: str, voice_name: str) -> dict:
        """
        Create a cloned voice from the extracted audio using HeyGen (Creator Plan).
        """
        from heygen_service.heygen_client import HeyGenClient
        import asyncio
        
        # Initialize service
        client = HeyGenClient()
        
        # Clone the voice using HeyGen
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(client.clone_voice(audio_path, voice_name))
            loop.close()
            
            # Map result to standard format
            return {
                "voice_id": result.get("voice_id"),
                "name": voice_name,
                "provider": "heygen",
                "is_cloned": True,
                "preview_url": result.get("preview_url") # HeyGen might return this
            }
        except Exception as e:
            logger.error(f"HeyGen Cloning Failed: {str(e)}")
            raise

    def process_koc_voice_and_clone(self, input_source: str, voice_name: str, is_url: bool = True) -> dict:
        """
        Full workflow: 
        1. Download/Get Video
        2. Extract Audio
        3. Clone via HeyGen
        4. Return Voice ID info
        """
        # Step 1 & 2: Get Audio
        if is_url:
            audio_path = self.download_video_audio(input_source)
        else:
            audio_path = self.extract_from_local_file(input_source)
            
        # Step 3: Clean Audio (Placeholder for now)
        clean_voice_path = self.separate_vocals(audio_path)
        
        try:
            # Step 4: Clone
            result = self.create_voice_clone(clean_voice_path, voice_name)
            
             # Clean up temp file
            if os.path.exists(audio_path):
                os.remove(audio_path)
                
            return {
                "success": True,
                **result
            }
            
        except Exception as e:
            logger.error(f"Process Failed: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }


