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

# ffprobe timeout: file mạng (UNC) chậm hơn → cần timeout dài
FFPROBE_TIMEOUT_LOCAL = getattr(settings, 'FFPROBE_TIMEOUT', 15)
FFPROBE_TIMEOUT_NETWORK = getattr(settings, 'FFPROBE_TIMEOUT_NETWORK', 60)  # UNC path

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
        if not os.path.exists(self.ffmpeg_path):
            self.ffmpeg_path = shutil.which('ffmpeg') or 'ffmpeg'
        if not os.path.exists(self.ffprobe_path):
            self.ffprobe_path = shutil.which('ffprobe') or 'ffprobe'
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
        OPTIMIZED: Parallel folder indexing + parallel ffprobe metadata fetching.
        
        Args:
            folder_paths: Dict mapping folder_type to folder_path
                Example: {"Sản phẩm": "\\\\VCB_MEDIA\\...\\Sản phẩm", ...}
            videos_per_folder: Max videos to index per folder (0 = unlimited)
        
        Returns:
            Dict with folder_type -> count of indexed videos
        """
        from video_management.models import IndexedVideo
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        results = {}
        results_lock = threading.Lock()

        def _index_one_folder(folder_type: str, folder_path: str) -> int:
            """Index a single folder, returning count of newly indexed videos."""
            if not os.path.isdir(folder_path):
                logger.warning(f"Folder not found: {folder_path}")
                return 0

            logger.info(f"📂 [PARALLEL] Indexing folder: {folder_type} → {folder_path}")

            try:
                video_files = self._scan_folder_fast(folder_path, videos_per_folder)
                logger.info(f"  🔍 {folder_type}: found {len(video_files)} video candidates")

                # ── Fast pre-filter: skip already-indexed paths ────────────────
                new_files = []
                for video_path in video_files:
                    db_path = self._normalize_path_for_db(video_path)
                    if not IndexedVideo.objects.filter(
                        file_path=db_path, folder_type=folder_type
                    ).exists():
                        new_files.append((video_path, db_path))

                if not new_files:
                    logger.info(f"  ✅ {folder_type}: all {len(video_files)} already indexed (skip)")
                    return 0

                logger.info(f"  ⚡ {folder_type}: {len(new_files)} new files to index (parallel ffprobe)")

                # ── Parallel ffprobe metadata ──────────────────────────────────
                count = 0
                count_lock = threading.Lock()

                def _probe_and_insert(video_path: str, db_path: str):
                    nonlocal count
                    try:
                        metadata = self._get_video_metadata(video_path)
                        if not metadata:
                            return
                        video_obj, created = IndexedVideo.objects.get_or_create(
                            file_path=db_path,
                            folder_type=folder_type,
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
                            with count_lock:
                                count += 1
                                if count % 10 == 0:
                                    logger.info(f"    📊 {folder_type}: indexed {count} new videos so far")
                    except Exception as e:
                        logger.error(f"Failed to index {video_path}: {e}")

                # 8 workers for ffprobe (network I/O bound, not CPU bound)
                ffprobe_workers = min(8, len(new_files))
                with ThreadPoolExecutor(
                    max_workers=ffprobe_workers,
                    thread_name_prefix=f"ffprobe_{folder_type[:6]}"
                ) as ff_executor:
                    futs = [
                        ff_executor.submit(_probe_and_insert, vp, dp)
                        for vp, dp in new_files
                    ]
                    for f in as_completed(futs):
                        pass  # results handled inside worker

                logger.info(f"  ✅ {folder_type}: indexed {count} new videos (total candidates: {len(video_files)})")
                return count

            except Exception as e:
                logger.error(f"Error indexing folder {folder_type}: {e}")
                return 0

        # ── Run all folders in parallel (max 4 concurrent folders) ────────────
        logger.info(f"🚀 Indexing {len(folder_paths)} folders in parallel (max 4 workers)...")
        folder_workers = min(4, len(folder_paths))
        with ThreadPoolExecutor(
            max_workers=folder_workers,
            thread_name_prefix="index_folder"
        ) as folder_executor:
            future_map = {
                folder_executor.submit(_index_one_folder, ft, fp): ft
                for ft, fp in folder_paths.items()
            }
            for fut in as_completed(future_map):
                ft = future_map[fut]
                try:
                    results[ft] = fut.result()
                except Exception as e:
                    logger.error(f"Folder {ft} indexing raised: {e}")
                    results[ft] = 0

        total = sum(results.values())
        logger.info(f"✅ Parallel indexing complete: {total} new videos across {len(folder_paths)} folders")
        return results
    
    def scan_and_index_specific_sku(self, sku: str, folder_path: str) -> Optional[int]:
        """
        Real-time scan for a specific SKU in the products folder.
        Finds and indexes ALL matching videos (not just the first one).
        
        Args:
            sku: Product SKU to search for
            folder_path: Physical path to 'Sản phẩm' folder
            
        Returns:
            First Video ID if found and indexed, else None
        """
        from video_management.models import IndexedVideo
        
        if not os.path.exists(folder_path):
             logger.warning(f"Product folder not found: {folder_path}")
             return None
             
        logger.info(f"🕵️ Real-time scanning for SKU '{sku}' in {folder_path}...")
        
        sku_clean = sku.strip().lower()
        found_videos = []
        
        # Recursive scan: Find ALL videos where SKU appears in path or filename
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in ALLOWED_EXTENSIONS:
                    full_path = os.path.join(root, file)
                    path_lower = full_path.lower()
                    # Match: SKU in filename or in folder path
                    if sku_clean in path_lower:
                        found_videos.append(full_path)
        
        if not found_videos:
            logger.warning(f"❌ No video found for SKU '{sku}' in {folder_path}")
            return None
            
        logger.info(f"✅ Found {len(found_videos)} videos for SKU '{sku}'")
        
        first_id = None
        indexed_count = 0
        
        for found_path in found_videos:
            try:
                db_path = self._normalize_path_for_db(found_path)
                
                # Index new video - use get_or_create to avoid duplicate key errors
                # (both _auto_index_by_sku and scan_and_index_specific_sku may run in parallel)
                metadata = self._get_video_metadata(found_path)
                if metadata:
                    video_obj, created = IndexedVideo.objects.get_or_create(
                        file_path=db_path,
                        folder_type="Sản phẩm",
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
                        indexed_count += 1
                    if first_id is None:
                        first_id = video_obj.id
            except Exception as e:
                logger.error(f"Failed to index found video {found_path}: {e}")
        
        logger.info(f"📊 SKU '{sku}': indexed {indexed_count} new videos, first_id={first_id}")
        return first_id
        
    def find_folder_by_name(self, root_path: str, target_name: str, exact_match: bool = False, max_depth: int = 3) -> Optional[str]:
        """
        Smart scan to find a folder by name (case-insensitive).
        
        Args:
            root_path: Starting directory
            target_name: Name to search for (e.g. "Dây chuyền" or "N300874")
            exact_match: If True, requires exact name match. If False, allows substring match.
            max_depth: Maximum recursion depth to prevent infinite loops.
        
        Returns:
            Absolute path to found folder, or None.
        """
        if not os.path.exists(root_path):
            return None
            
        target_clean = target_name.strip().lower()
        
        # BFS Search for better performance (find shallowest match first)
        queue = [(root_path, 0)]
        visited = set()
        
        while queue:
            current_path, depth = queue.pop(0)
            
            if current_path in visited or depth > max_depth:
                continue
            visited.add(current_path)
            
            try:
                # Scan current directory
                entries = os.scandir(current_path)
                subdirs = []
                
                for entry in entries:
                    if entry.is_dir():
                        name = entry.name.lower()
                        # Check match
                        is_match = (name == target_clean) if exact_match else (target_clean in name)
                        
                        if is_match:
                            logger.info(f"✅ Found smart folder match: '{entry.name}' in '{current_path}'")
                            return entry.path
                            
                        subdirs.append(entry.path)
                        
                # Add subdirs to queue
                for subdir in subdirs:
                    queue.append((subdir, depth + 1))
                    
            except PermissionError:
                continue
            except Exception as e:
                logger.warning(f"Error scanning {current_path}: {e}")
                continue
                
        return None
    
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
        """Video path normalization for Windows long paths (MAX_PATH > 260).
        Also normalizes forward-slash UNC paths (//server/share) to backslash (\\server\share).
        """
        if os.name != 'nt':
            return path
        
        # Normalize forward slashes to backslashes first
        # //VCB_MEDIA/MEDIA VCB folder/... → \\VCB_MEDIA\MEDIA VCB folder\...
        p = path.replace('/', '\\')
        
        # Already normalized with long-path prefix
        if p.startswith('\\\\?\\'):
            return p
            
        # Network path: \\server\share\... → \\?\UNC\server\share\...
        if p.startswith('\\\\'):
            return '\\\\?\\UNC' + p[1:]
            
        # Local path: C:\... → \\?\C:\...
        return '\\\\?\\{}'.format(p)
    
    def _normalize_path_for_db(self, path: str) -> str:
        """Normalize path for consistent DB storage: always \\server\share or C:\\path"""
        if os.name != 'nt':
            return os.path.normpath(path)
        p = path
        # Normalize forward-slash UNC: //server/share → \\server\share
        p = p.replace('/', '\\')
        # IMPORTANT: Check longer prefix first! \\?\UNC\ must be before \\?\
        if p.startswith('\\\\?\\UNC\\'):
            # \\?\UNC\server\share\path → \\server\share\path
            p = '\\\\' + p[8:]
        elif p.startswith('\\\\?\\'):
            p = p[4:]  # \\?\C:\path → C:\path
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
        """Get video metadata using ffprobe. Fallback metadata when ffprobe timeout (file mạng chậm)."""
        import json
        safe_path = None
        stat = None
        try:
            safe_path = self._prepare_path_for_windows(video_path)
            stat = os.stat(safe_path)
        except Exception as e:
            logger.warning(f"Cannot stat file {video_path}: {e}")
            return None

        # Timeout dài hơn cho file mạng (UNC path) - thường chậm
        is_network = video_path.replace('/', '\\').strip().startswith('\\\\')
        timeout_sec = FFPROBE_TIMEOUT_NETWORK if is_network else FFPROBE_TIMEOUT_LOCAL

        try:
            cmd = [
                self.ffprobe_path,
                '-v', 'error',
                '-show_entries', 'format=duration:stream=width,height,codec_type',
                '-of', 'json',
                '-i', safe_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if result.returncode != 0:
                logger.warning(f"ffprobe failed for {video_path}: {result.stderr[:200]}")
                return self._fallback_metadata(video_path, stat, "ffprobe returned error")

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
                return self._fallback_metadata(video_path, stat, "duration=0")

            return {
                'duration': duration,
                'size': stat.st_size,
                'mtime': stat.st_mtime,
                'width': width,
                'height': height,
                'has_audio': has_audio
            }

        except subprocess.TimeoutExpired:
            logger.warning(f"ffprobe timeout ({timeout_sec}s) for network file {video_path} - using fallback metadata")
            return self._fallback_metadata(video_path, stat, "timeout")
        except Exception as e:
            logger.error(f"Failed to get metadata for {video_path}: {e}")
            return self._fallback_metadata(video_path, stat, str(e))

    def _fallback_metadata(self, video_path: str, stat, reason: str) -> Optional[Dict]:
        """Fallback khi ffprobe timeout/lỗi - vẫn index video với duration mặc định để dùng được."""
        if stat is None:
            try:
                safe_path = self._prepare_path_for_windows(video_path)
                stat = os.stat(safe_path)
            except Exception:
                return None
        duration_default = 30.0  # Giả định 30s - mix vẫn chạy được, clip sẽ trim đúng khi generate
        logger.info(f"Using fallback metadata (duration={duration_default}s) for {Path(video_path).name} - reason: {reason}")
        return {
            'duration': duration_default,
            'size': stat.st_size,
            'mtime': stat.st_mtime,
            'width': None,
            'height': None,
            'has_audio': True
        }
    
    def get_or_generate_clip(
        self, video_id: int, use_gpu: bool = None, duration: float = None,
        keep_original_audio: bool = False, priority: str = 'normal'
    ) -> Optional[str]:
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
        # Outro (keep_original_audio=True): skip cache - pre-gen clips have re-encoded audio
        if not keep_original_audio:
            cached_clips = VideoClipCache.objects.filter(
                source_video=video
            ).order_by('-access_count', '-last_accessed_at')
            if cached_clips.exists():
                clip = cached_clips.first()
                if os.path.isfile(clip.clip_path):
                    clip.access_count += 1
                    clip.save(update_fields=['access_count', 'last_accessed_at'])
                    logger.info(f"✅ Cache HIT: {clip.clip_path}")
                    return clip.clip_path
                clip.delete()
        
        # Cache miss - generate clip
        logger.info(f"⚠️ Cache MISS: Generating clip from {video.file_path} (priority: {priority})")
        return self._generate_clip(video, use_gpu, duration, keep_original_audio=keep_original_audio, priority=priority)
    
    def _generate_clip(
        self, video, use_gpu: bool = None, clip_duration: float = None,
        keep_original_audio: bool = False, priority: str = 'normal'
    ) -> Optional[str]:
        """
        Generate a short clip from video with GPU acceleration if available.
        
        TWO-PASS OPTIMIZATION for NAS/network files:
          Pass 1: Fast sequential copy from NAS → local temp (no decode/encode)
          Pass 2: Encode from local temp → final clip (no network I/O)
        This avoids SMB random-access bottleneck: ~5-7s instead of ~20-30s.
        """
        from video_management.models import VideoClipCache
        import time
        
        # Resolve path (fix malformed UNC from DB, add long-path prefix)
        resolved_path = self._resolve_path_for_access(video.file_path)
        if not os.path.isfile(resolved_path):
            logger.error(f"Source video not found: {video.file_path} (resolved: {resolved_path})")
            return None
        
        # Detect network file (UNC path: \\server\share\...)
        is_network_file = resolved_path.replace('/', '\\').strip().startswith('\\\\')
        
        # Auto-detect GPU if not specified (check USE_GPU env: true/false/auto)
        if use_gpu is None:
            env_gpu = os.getenv('USE_GPU', 'true').lower()
            if env_gpu in ('false', '0', 'no'):
                use_gpu = False
            elif env_gpu in ('true', '1', 'yes'):
                use_gpu = True
                if not self.has_gpu():
                    logger.info("USE_GPU=true: will try GPU encoding (fallback to CPU if fails)")
            else:
                use_gpu = self.has_gpu()
        
        # ── FORCE CPU FOR NETWORK FILES ─────────────────────────────────────
        if is_network_file and use_gpu:
            logger.info("🌐 Network file → forcing CPU (libx264 ultrafast)")
            use_gpu = False
        
        # Use default or custom duration
        if clip_duration is None:
            clip_duration = CLIP_DURATION
        
        # Random start time
        max_start = max(0, video.duration - clip_duration)
        start_time = random.uniform(0, max_start) if max_start > 0 else 0
        
        # Generate clip filename
        clip_filename = f"{video.id}_{int(start_time)}_{random.randint(1000, 9999)}.mp4"
        clip_path = os.path.join(self.cache_dir, clip_filename)

        # ═══════════════════════════════════════════════════════════════════
        # TWO-PASS STRATEGY for NETWORK files (NAS/UNC)
        # ═══════════════════════════════════════════════════════════════════
        if is_network_file:
            return self._generate_clip_two_pass(
                video, resolved_path, start_time, clip_duration,
                clip_path, use_gpu, keep_original_audio=keep_original_audio,
                priority=priority
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # SINGLE-PASS for LOCAL files (fast disk I/O, no SMB overhead)
        # ═══════════════════════════════════════════════════════════════════
        return self._generate_clip_single_pass(
            video, resolved_path, start_time, clip_duration,
            clip_path, use_gpu, keep_original_audio=keep_original_audio,
            priority=priority
        )

    def _generate_clip_two_pass(
        self, video, resolved_path: str, start_time: float,
        clip_duration: float, clip_path: str, use_gpu: bool,
        keep_original_audio: bool = False, priority: str = 'normal'
    ) -> Optional[str]:
        """
        TWO-PASS clip generation for NETWORK files.
        
        Pass 1: ffmpeg -c copy → extract raw segment from NAS (sequential read, ~1-2s)
        Pass 2: ffmpeg encode  → encode from local temp file (no network, ~3-5s)
        
        Total: ~5-7s instead of ~20-30s (single-pass from NAS).
        """
        from video_management.models import VideoClipCache
        import time
        
        temp_raw = os.path.join(self.cache_dir, f"raw_{video.id}_{random.randint(1000,9999)}.mp4")
        
        try:
            overall_start = time.time()
            
            # ── PASS 1: Fast raw extract from NAS (sequential copy, no decode) ──
            logger.info(f"⚡ [PASS 1] Copying raw segment from NAS (sequential, no encode)...")
            # Add small buffer to clip_duration to ensure we have enough frames after trim
            extract_duration = clip_duration + 0.5
            
            cmd_extract = [
                self.ffmpeg_path,
                '-ss', str(start_time),
                '-i', resolved_path,
                '-t', str(extract_duration),
                '-c', 'copy',        # ⚡ NO encoding! Just byte-copy → blazing fast on NAS
                '-avoid_negative_ts', 'make_zero',
                '-y',
                temp_raw
            ]
            
            kwargs = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'ignore', 'timeout': 180}
            if priority == 'low' and os.name == 'nt':
                kwargs['creationflags'] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
                
            result1 = subprocess.run(cmd_extract, **kwargs)
            pass1_time = time.time() - t1
            
            if result1.returncode != 0 or not os.path.isfile(temp_raw):
                logger.warning(f"⚠️ Pass 1 copy failed: {result1.stderr[:200]}")
                # Fallback to single-pass
                logger.info("🔄 Fallback to single-pass (direct encode from NAS)...")
                return self._generate_clip_single_pass(
                    video, resolved_path, start_time, clip_duration,
                    clip_path, use_gpu, keep_original_audio=keep_original_audio
                )
            
            logger.info(f"✅ [PASS 1] Raw extracted in {pass1_time:.1f}s → {os.path.getsize(temp_raw)/1024/1024:.1f}MB")
            
            # ── PASS 2: Encode from LOCAL temp file (no network I/O!) ──────────
            logger.info(f"⚡ [PASS 2] Encoding from local temp (no network)...")
            
            encoder = 'h264_nvenc' if use_gpu else 'libx264'
            
            cmd_encode = [
                self.ffmpeg_path,
                '-i', temp_raw,          # ⚡ Reading from LOCAL disk!
                '-t', str(clip_duration), # Trim to exact duration
                '-vf', 'fps=30,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
                '-c:v', encoder,
            ]
            
            if use_gpu:
                cmd_encode.extend([
                    '-preset', 'fast',
                    '-profile:v', 'main',
                    '-level', '4.1',
                    '-b:v', '2M',
                    '-maxrate', '2.5M',
                    '-bufsize', '4M',
                    '-rc', 'vbr',
                    '-pix_fmt', 'yuv420p',
                ])
            else:
                cmd_encode.extend([
                    '-preset', 'ultrafast',
                    '-crf', '23',
                    '-pix_fmt', 'yuv420p',
                ])
            
            if keep_original_audio:
                cmd_encode.extend(['-c:a', 'copy', '-y', clip_path])
            else:
                cmd_encode.extend([
                    '-c:a', 'aac', '-ar', '44100', '-ac', '2',
                    '-y', clip_path
                ])
            
            t2 = time.time()
            kwargs_encode = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'ignore', 'timeout': 90}
            if priority == 'low' and os.name == 'nt':
                kwargs_encode['creationflags'] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
                
            result2 = subprocess.run(cmd_encode, **kwargs_encode)
            pass2_time = time.time() - t2
            
            # GPU fallback to CPU
            if result2.returncode != 0 and use_gpu:
                logger.warning(f"⚠️ GPU encode failed, retrying with CPU...")
                cmd_encode_cpu = [
                    self.ffmpeg_path, '-i', temp_raw, '-t', str(clip_duration),
                    '-vf', 'fps=30,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                    '-pix_fmt', 'yuv420p',
                ]
                cmd_encode_cpu.extend(
                    ['-c:a', 'copy'] if keep_original_audio
                    else ['-c:a', 'aac', '-ar', '44100', '-ac', '2']
                )
                cmd_encode_cpu.extend(['-y', clip_path])
                t2 = time.time()
                result2 = subprocess.run(cmd_encode_cpu, **kwargs_encode)
                pass2_time = time.time() - t2
                use_gpu = False
            
            # Cleanup temp raw
            if os.path.exists(temp_raw):
                os.remove(temp_raw)
            
            if result2.returncode != 0 or not os.path.isfile(clip_path):
                logger.error(f"❌ Pass 2 encode failed: {result2.stderr[:300]}")
                return None
            
            total_time = time.time() - overall_start
            
            # Cache the clip
            clip_size = os.path.getsize(clip_path)
            VideoClipCache.objects.create(
                source_video=video,
                clip_path=clip_path,
                start_time=start_time,
                duration=clip_duration,
                file_size=clip_size,
                generated_with_gpu=use_gpu,
                generation_time=total_time,
                access_count=1
            )
            
            video.last_used_at = timezone.now()
            video.use_count += 1
            video.save(update_fields=['last_used_at', 'use_count'])
            
            logger.info(
                f"✅ [TWO-PASS] Clip done in {total_time:.1f}s "
                f"(P1 copy: {pass1_time:.1f}s + P2 encode: {pass2_time:.1f}s) "
                f"→ {clip_path}"
            )
            return clip_path
        
        except subprocess.TimeoutExpired:
            logger.warning(f"⏱️ Two-pass timeout for {video.file_path} → fallback to single-pass")
            for f in [temp_raw, clip_path]:
                if os.path.exists(f):
                    os.remove(f)
            # Fallback: encode directly from NAS (slower but works)
            return self._generate_clip_single_pass(
                video, resolved_path, start_time, clip_duration,
                clip_path, use_gpu, keep_original_audio=keep_original_audio
            )
        except Exception as e:
            logger.error(f"❌ Two-pass generation failed: {e} → fallback to single-pass")
            for f in [temp_raw, clip_path]:
                if os.path.exists(f):
                    os.remove(f)
            # Fallback: encode directly from NAS
            return self._generate_clip_single_pass(
                video, resolved_path, start_time, clip_duration,
                clip_path, use_gpu
            )

    def _generate_clip_single_pass(
        self, video, resolved_path: str, start_time: float,
        clip_duration: float, clip_path: str, use_gpu: bool,
        keep_original_audio: bool = False, priority: str = 'normal'
    ) -> Optional[str]:
        """Single-pass clip generation for LOCAL files (or fallback)."""
        from video_management.models import VideoClipCache
        import time
        
        clip_timeout = 120
        encoder = 'h264_nvenc' if use_gpu else 'libx264'
        
        cmd = [
            self.ffmpeg_path,
            '-ss', str(start_time),
            '-i', resolved_path,
            '-t', str(clip_duration),
            '-vf', 'fps=30,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
            '-c:v', encoder,
        ]
        
        if use_gpu:
            cmd.extend([
                '-preset', 'fast',
                '-profile:v', 'main',
                '-level', '4.1',
                '-b:v', '2M',
                '-maxrate', '2.5M',
                '-bufsize', '4M',
                '-rc', 'vbr',
                '-pix_fmt', 'yuv420p',
            ])
        else:
            cmd.extend([
                '-preset', 'ultrafast',
                '-crf', '23',
                '-pix_fmt', 'yuv420p',
            ])
        
        cmd.extend(
            ['-c:a', 'copy'] if keep_original_audio
            else ['-c:a', 'aac', '-ar', '44100', '-ac', '2']
        )
        cmd.extend(['-y', clip_path])
        
        try:
            start = time.time()
            
            kwargs = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'errors': 'ignore', 'timeout': clip_timeout}
            if priority == 'low' and os.name == 'nt':
                kwargs['creationflags'] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
                
            result = subprocess.run(cmd, **kwargs)
            
            generation_time = time.time() - start
            
            # GPU failed → retry CPU
            if result.returncode != 0 and use_gpu:
                logger.warning(f"⚠️ GPU encoding failed for video {video.id}")
                logger.info(f"🔄 Retrying with CPU fallback...")
                cmd_cpu = [
                    self.ffmpeg_path,
                    '-ss', str(start_time),
                    '-i', resolved_path,
                    '-t', str(clip_duration),
                    '-vf', 'fps=30,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                    '-pix_fmt', 'yuv420p',
                ]
                cmd_cpu.extend(
                    ['-c:a', 'copy'] if keep_original_audio
                    else ['-c:a', 'aac', '-ar', '44100', '-ac', '2']
                )
                cmd_cpu.extend(['-y', clip_path])
                start = time.time()
                result = subprocess.run(cmd_cpu, **kwargs)
                generation_time = time.time() - start
                use_gpu = False
            
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
            
            video.last_used_at = timezone.now()
            video.use_count += 1
            video.save(update_fields=['last_used_at', 'use_count'])
            
            logger.info(f"✅ Clip generated in {generation_time:.1f}s (GPU: {use_gpu}): {clip_path}")
            return clip_path
        
        except subprocess.TimeoutExpired:
            logger.error(f"Clip generation timeout ({clip_timeout}s) for {video.file_path}")
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
    
    def get_random_videos(self, folder_types: List[str], product_category: Optional[str] = None) -> Dict[str, Optional[int]]:
        """
        Get random video IDs from each folder type for mixing.
        
        Args:
            folder_types: List of folder types to select from
            product_category: Optional category to filter 'Sản phẩm' videos
        
        Returns:
            Dict mapping folder_type -> video_id (or None if no video found)
        """
        from video_management.models import IndexedVideo
        from django.db.models import Q
        
        result = {}
        
        for folder_type in folder_types:
            # Base query
            query = Q(folder_type=folder_type, is_available=True, duration__gte=CLIP_DURATION)
            
            # Category filtering for 'Sản phẩm'
            if product_category and folder_type in ["Sản phẩm", "Sản phẩm HT"]:
                filtered_qs = IndexedVideo.objects.filter(
                    query & Q(file_path__icontains=product_category)
                ).values_list('id', flat=True)
                
                if filtered_qs.exists():
                    logger.info(f"  🔍 Filtered '{folder_type}' by '{product_category}' -> found {len(filtered_qs)} videos")
                    video_id = random.choice(list(filtered_qs))
                    result[folder_type] = video_id
                    continue
                else:
                    logger.warning(f"  ⚠️ No videos found for '{product_category}' in '{folder_type}'")
            
            # Fallback (All videos in folder)
            videos = IndexedVideo.objects.filter(query).values_list('id', flat=True)
            
            if videos:
                video_id = random.choice(list(videos))
                result[folder_type] = video_id
            else:
                logger.warning(f"No videos found for folder type: {folder_type}")
                result[folder_type] = None
        
        return result


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND PRE-GENERATION SYSTEM
# ═══════════════════════════════════════════════════════════════════════════
# After indexing, auto-generate clips for ALL indexed videos in background.
# When user hits "Generate Mix", all clips are cached → near-instant!
# ═══════════════════════════════════════════════════════════════════════════

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Pre-generation progress tracking
_pregen_progress = {
    "status": "idle",       # idle | running | completed | error
    "total": 0,
    "done": 0,
    "cached": 0,            # Already had cache (skipped)
    "generated": 0,         # Newly generated
    "failed": 0,
    "percent": 0,
    "message": "",
    "started_at": None,
    "completed_at": None,
}
_pregen_lock = threading.Lock()
_pregen_cancel = threading.Event()


def get_pregen_progress() -> dict:
    """Get current pre-generation progress."""
    with _pregen_lock:
        return dict(_pregen_progress)


def cancel_pregen():
    """Cancel running pre-generation."""
    _pregen_cancel.set()
    with _pregen_lock:
        _pregen_progress["message"] = "Cancelling..."


def start_background_pregen(clip_duration: float = 12.0):
    """
    Start background pre-generation of clips for ALL indexed videos.
    
    This runs in a daemon thread and generates 1 clip per indexed video.    
    Clips are generated at `clip_duration` seconds (default 12s) which
    covers most A4 formula slot durations (audio/6 ≈ 7-15s).
    
    The mix system's cache lookup doesn't check duration, so a 12s clip
    will be returned for 9.79s requests. The final output is trimmed by
    _replace_audio() to exact audio length.
    
    Args:
        clip_duration: Duration of pre-generated clips (default 12s)
    """
    import time
    
    with _pregen_lock:
        if _pregen_progress["status"] == "running":
            logger.info("⚠️ Pre-generation already running, skipping")
            return
    
    def _run_pregen():
        from video_management.models import IndexedVideo, VideoClipCache
        
        service = get_preprocessing_service()
        _pregen_cancel.clear()
        
        MAX_PASSES = 3  # Tối đa 3 pass để đảm bảo indexed == cached
        
        try:
            started_at = time.time()
            
            # Khởi tạo progress
            total_all = IndexedVideo.objects.filter(is_available=True).count()
            already_cached_init = VideoClipCache.objects.values('source_video_id').distinct().count()
            
            with _pregen_lock:
                _pregen_progress.update({
                    "status": "running",
                    "total": total_all,
                    "done": already_cached_init,
                    "cached": already_cached_init,
                    "generated": 0,
                    "failed": 0,
                    "percent": int(already_cached_init / total_all * 100) if total_all > 0 else 0,
                    "message": f"Chuan bi cache {total_all - already_cached_init} clips...",
                    "started_at": started_at,
                    "completed_at": None,
                })
            
            total_generated = 0
            
            for pass_num in range(1, MAX_PASSES + 1):
                if _pregen_cancel.is_set():
                    break
                
                # Re-query moi pass: bat ca video moi index + retry failed
                all_videos = list(IndexedVideo.objects.filter(is_available=True))
                videos_with_cache = set(
                    VideoClipCache.objects.values_list('source_video_id', flat=True)
                )
                
                videos_to_generate = [v for v in all_videos if v.id not in videos_with_cache]
                total_all = len(all_videos)
                already_cached = len(videos_with_cache)
                total_to_gen = len(videos_to_generate)
                
                if total_to_gen == 0:
                    logger.info(f"All {already_cached}/{total_all} videos da co cache!")
                    break
                
                logger.info(
                    f"[Pass {pass_num}/{MAX_PASSES}] Can cache {total_to_gen} videos "
                    f"({already_cached} da co, {total_to_gen} con thieu)"
                )
                
                with _pregen_lock:
                    _pregen_progress.update({
                        "total": total_all,
                        "done": already_cached,
                        "cached": already_cached,
                        "message": f"[Pass {pass_num}/{MAX_PASSES}] Dang cache {total_to_gen} videos...",
                        "percent": int(already_cached / total_all * 100) if total_all > 0 else 0,
                    })
                
                pass_gen = 0
                pass_fail = 0
                gen_lock = threading.Lock()
                
                def _gen_one(video, _already_cached=already_cached, _total_all=total_all, _total_to_gen=total_to_gen, _pass_num=pass_num):
                    nonlocal pass_gen, pass_fail, total_generated
                    
                    if _pregen_cancel.is_set():
                        return
                    
                    try:
                        clip_path = service.get_or_generate_clip(
                            video.id, use_gpu=None, duration=clip_duration, priority='low'
                        )
                        time.sleep(0.5)  # 0.5s (giam tu 1.0s)
                        
                        with gen_lock:
                            if clip_path:
                                pass_gen += 1
                                total_generated += 1
                            else:
                                pass_fail += 1
                            
                            done_real = _already_cached + pass_gen
                            pct = int(done_real / _total_all * 100) if _total_all > 0 else 0
                            
                            with _pregen_lock:
                                _pregen_progress.update({
                                    "done": done_real,
                                    "generated": total_generated,
                                    "failed": pass_fail,
                                    "percent": min(pct, 99),
                                    "message": (
                                        f"[Pass {_pass_num}] Cache {pass_gen}/{_total_to_gen}"
                                        + (f" ({pass_fail} loi)" if pass_fail > 0 else "")
                                    ),
                                })
                            
                            if pass_gen % 10 == 0 and pass_gen > 0:
                                logger.info(
                                    f"[Pass {_pass_num}] {pass_gen}/{_total_to_gen} "
                                    f"({pct}%) - {pass_fail} loi"
                                )
                    
                    except Exception as e:
                        with gen_lock:
                            pass_fail += 1
                        logger.error(f"Pre-gen failed for video {video.id}: {e}")
                
                # 3 workers de can bang toc do va tai he thong
                max_workers = min(3, total_to_gen)
                with ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix=f"pregen_p{pass_num}"
                ) as executor:
                    futures = [
                        executor.submit(_gen_one, v) for v in videos_to_generate
                    ]
                    for f in as_completed(futures):
                        if _pregen_cancel.is_set():
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                
                logger.info(f"[Pass {pass_num}] Xong: {pass_gen} cache, {pass_fail} loi")
                
                # Neu pass nay khong co loi nao → done som
                if pass_fail == 0 and not _pregen_cancel.is_set():
                    break
                
                # Con loi → retry pass tiep theo
                if pass_num < MAX_PASSES and pass_fail > 0:
                    logger.info(f"{pass_fail} videos loi → Retry pass {pass_num + 1}/{MAX_PASSES}...")
                    time.sleep(2.0)
            
            # ── Final verification: indexed == cached ────────────────────────
            elapsed = time.time() - started_at
            
            if _pregen_cancel.is_set():
                msg = f"Da dung. Da cache {total_generated} clips trong {elapsed:.0f}s"
                final_status = "idle"
                final_pct = _pregen_progress.get("percent", 0)
            else:
                final_indexed = IndexedVideo.objects.filter(is_available=True).count()
                final_cached_count = VideoClipCache.objects.values('source_video_id').distinct().count()
                missing = final_indexed - final_cached_count
                
                if missing <= 0:
                    msg = (
                        f"Hoan tat! {final_cached_count}/{final_indexed} videos da cache "
                        f"(100%) - {elapsed:.0f}s"
                    )
                    final_pct = 100
                else:
                    msg = (
                        f"Cache gan xong: {final_cached_count}/{final_indexed} "
                        f"({missing} video khong the cache) - {elapsed:.0f}s"
                    )
                    final_pct = int(final_cached_count / final_indexed * 100) if final_indexed > 0 else 100
                    logger.warning(f"{missing} videos khong the cache sau {MAX_PASSES} passes")
                
                final_status = "completed"
            
            logger.info(msg)
            
            with _pregen_lock:
                _pregen_progress.update({
                    "status": final_status,
                    "percent": final_pct,
                    "message": msg,
                    "completed_at": time.time(),
                })
        
        except Exception as e:
            logger.error(f"Pre-generation error: {e}", exc_info=True)
            with _pregen_lock:
                _pregen_progress.update({
                    "status": "error",
                    "message": f"Error: {str(e)}",
                })
    
    # Start as daemon thread
    thread = threading.Thread(
        target=_run_pregen,
        name="clip_pregen_bg",
        daemon=True
    )
    thread.start()
    logger.info("Background pre-generation thread started")





# Singleton instance
_service = None

def get_preprocessing_service() -> SmartPreprocessingService:
    """Get singleton instance of preprocessing service."""
    global _service
    if _service is None:
        _service = SmartPreprocessingService()
    return _service

