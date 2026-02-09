"""
Smart Pre-processing Service for Mix Video Optimization.

This service implements a hybrid pre-processing + lazy loading approach:
1. Index video metadata (no file copying)
2. Generate clips on-demand with caching
3. Fast mix using cached clips

Performance: 5-13s for mix (vs 2-3 minutes previously)
"""

import os
import random
import shutil
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction
from django.conf import settings

logger = logging.getLogger(__name__)

# Video extensions
ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.m4v'}

# Cache settings
CACHE_DIR = getattr(settings, 'VIDEO_CLIP_CACHE_DIR', os.path.join(settings.BASE_DIR, 'media', 'clip_cache'))
MAX_CACHE_SIZE_GB = getattr(settings, 'MAX_CLIP_CACHE_SIZE_GB', 10)  # 10GB default
CLIP_DURATION = 8  # seconds


class SmartPreprocessingService:
    """Service for smart video preprocessing and caching."""
    
    def __init__(self):
        self.cache_dir = CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ffmpeg_path = os.getenv('FFMPEG_PATH', 'ffmpeg')
        self.ffprobe_path = os.getenv('FFPROBE_PATH', 'ffprobe')
        self._gpu_available = None
    
    def has_gpu(self) -> bool:
        """Check if NVIDIA GPU is available for hardware acceleration."""
        if self._gpu_available is not None:
            return self._gpu_available
        
        try:
            result = subprocess.run(
                [self.ffmpeg_path, '-hide_banner', '-encoders'],
                capture_output=True,
                text=True,
                timeout=5
            )
            self._gpu_available = 'h264_nvenc' in result.stdout
            if self._gpu_available:
                logger.info("✅ NVIDIA GPU detected - hardware acceleration enabled")
            else:
                logger.info("⚠️ No NVIDIA GPU - using CPU encoding")
            return self._gpu_available
        except Exception as e:
            logger.warning(f"GPU detection failed: {e}")
            self._gpu_available = False
            return False
    
    def index_videos_from_folders(self, folder_paths: Dict[str, str], videos_per_folder: int = 1000) -> Dict[str, int]:
        """
        Index videos from multiple folders into database.
        
        Args:
            folder_paths: Dict mapping folder_type to folder_path
                Example: {"Sản phẩm": "\\\\VCB_MEDIA\\...\\Sản phẩm", ...}
            videos_per_folder: Max videos to index per folder (0 = unlimited)
        
        Returns:
            Dict with folder_type -> count of indexed videos
        """
        from video_management.models import IndexedVideo
        
        results = {}
        
        for folder_type, folder_path in folder_paths.items():
            if not os.path.isdir(folder_path):
                logger.warning(f"Folder not found: {folder_path}")
                results[folder_type] = 0
                continue
            
            logger.info(f"Indexing folder: {folder_type} from {folder_path}")
            count = 0
            
            try:
                # Scan for videos (fast - only 1-2 levels deep)
                video_files = self._scan_folder_fast(folder_path, videos_per_folder)
                
                for video_path in video_files:
                    try:
                        # Get metadata
                        # Normalize path for consistent DB lookup (handles \\?\UNC vs \\ formats)
                        db_path = self._normalize_path_for_db(video_path)
                        
                        # OPTIMIZATION: Check if already indexed for this folder type BEFORE running ffprobe
                        # This makes re-indexing (User 2) super fast!
                        if IndexedVideo.objects.filter(file_path=db_path, folder_type=folder_type).exists():
                            # Already indexed, skip expensive processing
                            continue

                        # Get metadata (EXPENSIVE OPERATION - only run if new)
                        metadata = self._get_video_metadata(video_path)
                        if not metadata:
                            continue
                        # Check if video already indexed FOR THIS FOLDER TYPE
                        video_obj, created = IndexedVideo.objects.get_or_create(
                            file_path=db_path,
                            folder_type=folder_type,  # Allow same file in different folder types
                            defaults={
                                'duration': metadata['duration'],
                                'file_size': metadata['size'],
                                'has_audio': metadata.get('has_audio', False),
                                'width': metadata.get('width'),
                                'height': metadata.get('height'),
                                'modified_time': timezone.make_aware(
                                    datetime.fromtimestamp(metadata['mtime']),
                                    timezone.get_default_timezone()
                                ),
                                'is_available': True
                            }
                        )
                        if created:
                            count += 1
                        
                        if count % 10 == 0:
                            logger.info(f"  Indexed {count} videos from {folder_type}")
                    
                    except Exception as e:
                        logger.error(f"Failed to index {video_path}: {e}")
                        continue
                
                results[folder_type] = count
                if count > 0 or len(video_files) > 0:
                    logger.info(f"✅ Found {len(video_files)} candidates, Indexed {count} new videos from {folder_type}")
                else:
                    logger.info(f"✅ Indexed {count} videos from {folder_type}")
            
            except Exception as e:
                logger.error(f"Error indexing folder {folder_type}: {e}")
                results[folder_type] = 0
        
        return results
    
    def _scan_folder_fast(self, folder_path: str, limit: int = 0) -> List[str]:
        """Scan folder recursively (unlimited depth) to find all videos."""
        videos = []
        
        # Prepare for long paths
        scan_path = self._prepare_path_for_windows(folder_path)
        
        try:
            # os.walk automatically handles recursion for all subfolders
            for root, dirs, files in os.walk(scan_path):
                if limit > 0 and len(videos) >= limit:
                    break
                    
                for file in files:
                    if limit > 0 and len(videos) >= limit:
                        break
                        
                    ext = os.path.splitext(file)[1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        full_path = os.path.join(root, file)
                        videos.append(full_path)
                        
        except Exception as e:
            logger.warning(f"Cannot scan {folder_path}: {e}")
            
        return videos
    
    def _prepare_path_for_windows(self, path: str) -> str:
        """Video path normalization for Windows long paths (MAX_PATH > 260)."""
        if os.name != 'nt':
            return path
            
        # Already normalized
        if path.startswith('\\\\?\\'):
            return path
            
        # Network path: \\server\share\... -> \\?\UNC\server\share\...
        if path.startswith(r'\\'):
            return r'\\?\UNC' + path[1:]
            
        # Local path: C:\... -> \\?\C:\...
        return r'\\?\{}'.format(path)
    
    def _normalize_path_for_db(self, path: str) -> str:
        """Normalize path for consistent DB storage: always \\server\share or C:\\path"""
        if os.name != 'nt':
            return os.path.normpath(path)
        p = path
        # IMPORTANT: Check longer prefix first! \\?\UNC\ must be before \\?\
        if p.startswith(r'\\?\UNC\\'):
            # \\?\UNC\server\share\path -> \\server\share\path
            p = '\\\\' + p[8:]
        elif p.startswith('\\\\?\\'):
            p = p[4:]  # \\?\C:\path -> C:\path
        return os.path.normpath(p)
    
    def _resolve_path_for_access(self, path: str) -> str:
        """Resolve path from DB for file access. Returns \\server\share (ffmpeg works with this)."""
        if os.name != 'nt':
            return path
        p = path.strip()
        # Fix malformed UNC from old bug: UNC\server\share -> \\server\share
        if p.startswith('UNC\\'):
            p = '\\\\' + p[4:]
        # Use standard \\server\share - ffmpeg handles this; avoid \\?\UNC which can cause issues
        return os.path.normpath(p)
    
    def _get_video_metadata(self, video_path: str) -> Optional[Dict]:
        """Get video metadata using ffprobe."""
        import json
        try:
            # Handle long paths on Windows
            safe_path = self._prepare_path_for_windows(video_path)
            
            stat = os.stat(safe_path)
            
            # Use JSON format for robust parsing (handles various ffprobe versions)
            cmd = [
                self.ffprobe_path,
                '-v', 'error',
                '-show_entries', 'format=duration:stream=width,height,codec_type',
                '-of', 'json',
                '-i', safe_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                logger.warning(f"ffprobe failed for {video_path}: {result.stderr[:200]}")
                return None
            
            data = json.loads(result.stdout)
            format_info = data.get('format', {})
            streams = data.get('streams', [])
            
            duration = 0.0
            try:
                duration = float(format_info.get('duration', 0))
            except (TypeError, ValueError):
                pass
            
            width = None
            height = None
            has_audio = False
            for s in streams:
                if s.get('codec_type') == 'video':
                    width = width or s.get('width')
                    height = height or s.get('height')
                elif s.get('codec_type') == 'audio':
                    has_audio = True
            
            if duration == 0:
                logger.warning(f"Metadata check failed (duration=0) for {video_path}")
                return None
            
            return {
                'duration': duration,
                'size': stat.st_size,
                'mtime': stat.st_mtime,
                'width': width,
                'height': height,
                'has_audio': has_audio
            }
        
        except Exception as e:
            logger.error(f"Failed to get metadata for {video_path}: {e}")
            return None
    
    def get_or_generate_clip(self, video_id: int, use_gpu: bool = None, duration: float = None) -> Optional[str]:
        """
        Get cached clip or generate new one (Lazy Loading).
        
        Args:
            video_id: IndexedVideo ID
            use_gpu: Force GPU on/off, None = auto
            duration: Clip duration in seconds (default: CLIP_DURATION = 8s)
            use_gpu: Force GPU on/off (None = auto-detect)
        
        Returns:
            Path to clip file or None if failed
        """
        from video_management.models import IndexedVideo, VideoClipCache
        
        # Get video
        try:
            video = IndexedVideo.objects.get(id=video_id)
        except IndexedVideo.DoesNotExist:
            logger.error(f"Video ID {video_id} not found")
            return None
        
        # Check cache
        cached_clips = VideoClipCache.objects.filter(
            source_video=video
        ).order_by('-access_count', '-last_accessed_at')
        
        if cached_clips.exists():
            clip = cached_clips.first()
            if os.path.isfile(clip.clip_path):
                # Cache hit!
                clip.access_count += 1
                clip.save(update_fields=['access_count', 'last_accessed_at'])
                logger.info(f"✅ Cache HIT: {clip.clip_path}")
                return clip.clip_path
            else:
                # Clip file missing, delete cache entry
                clip.delete()
        
        # Cache miss - generate clip
        logger.info(f"⚠️ Cache MISS: Generating clip from {video.file_path}")
        return self._generate_clip(video, use_gpu, duration)
    
    def _generate_clip(self, video, use_gpu: bool = None, clip_duration: float = None) -> Optional[str]:
        """Generate a short clip from video with GPU acceleration if available."""
        from video_management.models import VideoClipCache
        
        # Resolve path (fix malformed UNC from DB, add long-path prefix)
        resolved_path = self._resolve_path_for_access(video.file_path)
        if not os.path.isfile(resolved_path):
            logger.error(f"Source video not found: {video.file_path} (resolved: {resolved_path})")
            return None
        
        # Auto-detect GPU if not specified (check USE_GPU env: true/false/auto)
        if use_gpu is None:
            env_gpu = os.getenv('USE_GPU', 'true').lower()
            if env_gpu in ('false', '0', 'no'):
                use_gpu = False
            elif env_gpu in ('true', '1', 'yes'):
                # Force GPU - will fallback to CPU if encoding fails
                use_gpu = True
                if not self.has_gpu():
                    logger.info("USE_GPU=true: will try GPU encoding (fallback to CPU if fails)")
            else:
                use_gpu = self.has_gpu()
        
        # Use default or custom duration
        if clip_duration is None:
            clip_duration = CLIP_DURATION
        
        # Random start time
        max_start = max(0, video.duration - clip_duration)
        start_time = random.uniform(0, max_start) if max_start > 0 else 0
        
        # Generate clip filename
        clip_filename = f"{video.id}_{int(start_time)}_{random.randint(1000, 9999)}.mp4"
        clip_path = os.path.join(self.cache_dir, clip_filename)
        
        # FFmpeg command with STRICT NORMALIZATION (critical for concat!)
        encoder = 'h264_nvenc' if use_gpu else 'libx264'
        
        cmd = [
            self.ffmpeg_path,
            '-ss', str(start_time),
            '-i', resolved_path,
            '-t', str(clip_duration),
            '-vf', 'fps=30,scale=540:960',  # ✅ FORCE 30fps + scale (prevents speed up/slow down!)
            '-c:v', encoder,
        ]
        
        if use_gpu:
            # GPU: Optimized for GT 1030 (Pascal architecture with old NVENC generation)
            # Note: GT 1030 doesn't support new presets (p1-p7), use legacy presets
            cmd.extend([
                '-preset', 'fast',         # Legacy preset: fast (NOT p2!)
                '-profile:v', 'main',      # Main profile (most compatible)
                '-level', '4.1',           # Level 4.1 (supports 1080p@30fps)
                '-b:v', '2M',              # 2 Mbps bitrate
                '-maxrate', '2.5M',        # Max bitrate
                '-bufsize', '4M',          # Buffer (2x maxrate)
                '-rc', 'vbr',              # Variable bitrate
                '-pix_fmt', 'yuv420p',     # ✅ Consistent pixel format
            ])
        else:
            # CPU: Fast encoding
            cmd.extend([
                '-preset', 'ultrafast',
                '-crf', '28',
                '-pix_fmt', 'yuv420p',     # ✅ Consistent pixel format
            ])
        
        cmd.extend([
            '-c:a', 'aac',
            '-ar', '44100',                # ✅ Consistent sample rate
            '-ac', '2',                    # ✅ Stereo
            '-y',
            clip_path
        ])
        
        try:
            import time
            start = time.time()
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',  # Ignore Unicode decode errors from FFmpeg output
                timeout=60
            )
            
            generation_time = time.time() - start
            
            # If GPU failed, retry with CPU
            if result.returncode != 0 and use_gpu:
                logger.warning(f"⚠️ GPU encoding failed for video {video.id}")
                logger.warning(f"GPU Error: {result.stderr[:500]}")  # First 500 chars
                logger.info(f"🔄 Retrying with CPU fallback...")
                # Rebuild command with CPU encoder
                encoder = 'libx264'
                preset = 'ultrafast'
                
                cmd_cpu = [
                    self.ffmpeg_path,
                    '-ss', str(start_time),
                    '-i', resolved_path,
                    '-t', str(clip_duration),  # ✅ Use clip_duration instead of CLIP_DURATION
                    '-vf', 'fps=30,scale=540:960',  # ✅ NORMALIZE: Force 30fps + scale
                    '-c:v', encoder,
                    '-preset', preset,
                    '-crf', '28',
                    '-pix_fmt', 'yuv420p',     # ✅ Consistent pixel format
                    '-c:a', 'aac',
                    '-ar', '44100',
                    '-ac', '2',
                    '-y',
                    clip_path
                ]
                
                start = time.time()
                result = subprocess.run(
                    cmd_cpu,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    timeout=60
                )
                generation_time = time.time() - start
                use_gpu = False  # Mark as CPU-generated
            
            if result.returncode != 0:
                logger.error(f"FFmpeg failed: {result.stderr}")
                return None
            
            if not os.path.isfile(clip_path):
                logger.error(f"Clip not generated: {clip_path}")
                return None
            
            # Cache the clip
            clip_size = os.path.getsize(clip_path)
            VideoClipCache.objects.create(
                source_video=video,
                clip_path=clip_path,
                start_time=start_time,
                duration=clip_duration,
                file_size=clip_size,
                generated_with_gpu=use_gpu,
                generation_time=generation_time,
                access_count=1
            )
            
            # Update video usage
            video.last_used_at = timezone.now()
            video.use_count += 1
            video.save(update_fields=['last_used_at', 'use_count'])
            
            logger.info(f"✅ Clip generated in {generation_time:.1f}s (GPU: {use_gpu}): {clip_path}")
            return clip_path
        
        except subprocess.TimeoutExpired:
            logger.error(f"Clip generation timeout for {video.file_path}")
            if os.path.exists(clip_path):
                os.remove(clip_path)
            return None
        except Exception as e:
            logger.error(f"Clip generation failed: {e}")
            if os.path.exists(clip_path):
                os.remove(clip_path)
            return None
    
    def cleanup_cache(self, max_size_gb: float = None):
        """Clean up cache using LRU strategy."""
        from video_management.models import VideoClipCache
        from django.db import models as db_models
        
        if max_size_gb is None:
            max_size_gb = MAX_CACHE_SIZE_GB
        
        max_size_bytes = max_size_gb * 1024 * 1024 * 1024
        
        # Calculate current cache size
        total_size = VideoClipCache.objects.aggregate(
            total=db_models.Sum('file_size')
        )['total'] or 0
        
        if total_size <= max_size_bytes:
            logger.info(f"Cache size OK: {total_size / 1024 / 1024:.1f} MB")
            return
        
        logger.info(f"Cache cleanup needed: {total_size / 1024 / 1024:.1f} MB > {max_size_gb} GB")
        
        # Delete least recently used clips
        clips_to_delete = VideoClipCache.objects.order_by('last_accessed_at')
        deleted_size = 0
        deleted_count = 0
        
        for clip in clips_to_delete:
            if total_size - deleted_size <= max_size_bytes:
                break
            
            try:
                if os.path.exists(clip.clip_path):
                    os.remove(clip.clip_path)
                deleted_size += clip.file_size
                deleted_count += 1
                clip.delete()
            except Exception as e:
                logger.error(f"Failed to delete clip {clip.clip_path}: {e}")
        
        logger.info(f"✅ Deleted {deleted_count} clips, freed {deleted_size / 1024 / 1024:.1f} MB")
    
    def get_random_videos(self, folder_types: List[str]) -> Dict[str, Optional[int]]:
        """
        Get random video IDs from each folder type for mixing.
        
        Args:
            folder_types: List of folder types to select from
        
        Returns:
            Dict mapping folder_type -> video_id (or None if no video found)
        """
        from video_management.models import IndexedVideo
        
        result = {}
        
        for folder_type in folder_types:
            videos = IndexedVideo.objects.filter(
                folder_type=folder_type,
                is_available=True,
                duration__gte=CLIP_DURATION  # Must be long enough for clip
            ).values_list('id', flat=True)
            
            if videos:
                video_id = random.choice(list(videos))
                result[folder_type] = video_id
            else:
                logger.warning(f"No videos found for folder type: {folder_type}")
                result[folder_type] = None
        
        return result


# Singleton instance
_service = None

def get_preprocessing_service() -> SmartPreprocessingService:
    """Get singleton instance of preprocessing service."""
    global _service
    if _service is None:
        _service = SmartPreprocessingService()
    return _service
