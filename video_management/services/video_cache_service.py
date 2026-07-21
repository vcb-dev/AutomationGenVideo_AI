"""
Video Cache Service - Quản lý cache metadata của video files local
Tối ưu tốc độ scan folder bằng PostgreSQL cache
"""

import os
import logging
from pathlib import Path
from typing import List, Dict, Optional, Generator
from datetime import datetime, timedelta
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.utils import timezone
from django.db import models as django_models

logger = logging.getLogger(__name__)

# Video extensions
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.mpeg', '.mpg'}


@dataclass
class VideoScanResult:
    """Kết quả scan 1 video file"""
    path: str
    duration: float
    has_audio: bool = True
    width: Optional[int] = None
    height: Optional[int] = None


def get_cache_service():
    """Factory function để tạo VideoCacheService instance"""
    return VideoCacheService()


class VideoCacheService:
    """
    Service quản lý cache metadata của local video files.
    Tự động scan folder, check cache, update khi cần.
    """
    
    def __init__(self):
        self.cache_hits = 0
        self.cache_misses = 0
    
    def get_cached_video(self, file_path: str) -> Optional[VideoScanResult]:
        """
        Lấy thông tin video từ cache (PostgreSQL).
        Kiểm tra xem file có thay đổi không (so sánh mtime và size).
        
        Returns:
            VideoScanResult nếu cache valid, None nếu miss hoặc invalid
        """
        from video_management.models import LocalVideoFile
        
        try:
            cached = LocalVideoFile.objects.filter(file_path=file_path).first()
            
            if not cached:
                logger.debug(f"Cache MISS: {file_path}")
                self.cache_misses += 1
                return None
            
            # Check if file still exists and unchanged
            if not os.path.isfile(file_path):
                logger.debug(f"Cache INVALID (file deleted): {file_path}")
                cached.delete()
                self.cache_misses += 1
                return None
            
            stat = os.stat(file_path)
            file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
            
            if stat.st_size != cached.file_size or abs((file_mtime - cached.modified_time).total_seconds()) > 1:
                logger.debug(f"Cache INVALID (file changed): {file_path}")
                cached.delete()
                self.cache_misses += 1
                return None
            
            # Cache HIT - update last_accessed
            cached.last_accessed_at = timezone.now()
            cached.save(update_fields=['last_accessed_at'])
            
            logger.debug(f"Cache HIT: {file_path}")
            self.cache_hits += 1
            
            return VideoScanResult(
                path=file_path,
                duration=cached.duration,
                has_audio=cached.has_audio,
                width=cached.width,
                height=cached.height
            )
            
        except Exception as e:
            logger.warning(f"Error checking cache for {file_path}: {e}")
            self.cache_misses += 1
            return None
    
    def cache_video(self, file_path: str, duration: float, has_audio: bool = False,
                   width: Optional[int] = None, height: Optional[int] = None) -> None:
        """
        Lưu metadata của video vào cache (PostgreSQL).
        Tự động update nếu đã tồn tại.
        """
        from video_management.models import LocalVideoFile
        
        try:
            stat = os.stat(file_path)
            file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
            folder_name = os.path.basename(os.path.dirname(file_path))
            
            LocalVideoFile.objects.update_or_create(
                file_path=file_path,
                defaults={
                    'file_size': stat.st_size,
                    'modified_time': file_mtime,
                    'duration': duration,
                    'has_audio': has_audio,
                    'width': width,
                    'height': height,
                    'folder_name': folder_name,
                }
            )
            logger.debug(f"Cached: {file_path} (duration={duration}s)")
            
        except Exception as e:
            logger.warning(f"Failed to cache {file_path}: {e}")
    
    def scan_video_with_cache(self, file_path: str, skip_duration: bool = False) -> Optional[VideoScanResult]:
        """
        Scan một video file với cache.
        Nếu có trong cache và valid → return ngay.
        Nếu không → gọi ffprobe và cache lại.
        
        Args:
            file_path: Đường dẫn tuyệt đối tới file
            skip_duration: Nếu True, không check duration (dùng khi scan nhanh)
            
        Returns:
            VideoScanResult hoặc None nếu lỗi
        """
        # Try cache first
        cached = self.get_cached_video(file_path)
        if cached:
            return cached
        
        # Cache miss → scan with ffprobe (hoặc skip nếu không có ffprobe)
        try:
            from video_management.views.mix_video_views import _get_duration, _has_audio
            
            duration = None
            has_audio = True
            
            if not skip_duration:
                duration = _get_duration(file_path)
            
            # Nếu skip_duration hoặc không có ffprobe → dùng duration mặc định
            if skip_duration or not duration or duration <= 0:
                if not skip_duration:
                    logger.debug(f"Cannot get duration for {file_path}, using default")
                # Dùng duration mặc định = 10 giây (để scan nhanh)
                duration = 10.0
                has_audio = True
            else:
                has_audio = _has_audio(file_path)
            
            # Save to cache (chỉ khi có duration thực)
            if not skip_duration and duration > 0:
                self.cache_video(file_path, duration, has_audio)
            
            return VideoScanResult(
                path=file_path,
                duration=duration,
                has_audio=has_audio
            )
            
        except Exception as e:
            logger.warning(f"Error scanning video {file_path}: {e}")
            return None
    
    def scan_folder_recursive(self, folder_path: str, skip_duration: bool = False) -> List[VideoScanResult]:
        """
        Scan tất cả videos trong folder (bao gồm subfolder) với cache.
        Tối ưu tốc độ bằng cache PostgreSQL.
        
        Args:
            folder_path: Đường dẫn folder gốc
            skip_duration: Nếu True, không check duration (scan nhanh hơn)
            
        Returns:
            List VideoScanResult
        """
        logger.info(f"Scanning folder recursively: {folder_path} (skip_duration={skip_duration})")
        
        results = []
        folder_path_obj = Path(folder_path)
        
        # Recursive scan tất cả video files
        for video_file in folder_path_obj.rglob('*'):
            if video_file.is_file() and video_file.suffix.lower() in VIDEO_EXTENSIONS:
                video_result = self.scan_video_with_cache(str(video_file), skip_duration=skip_duration)
                if video_result:
                    results.append(video_result)
        
        logger.info(f"Scanned {folder_path}: found {len(results)} videos (cache hits: {self.cache_hits}, misses: {self.cache_misses})")
        return results
    
    def scan_folders_parallel(self, folder_paths: List[str], max_workers: Optional[int] = None,
                             skip_duration: bool = False) -> Dict[str, List[VideoScanResult]]:
        """
        Scan nhiều folders song song với ThreadPoolExecutor.
        Tối ưu cho I/O-bound tasks.
        
        Args:
            folder_paths: List đường dẫn folders
            max_workers: Số threads (mặc định = CPU cores * 2)
            skip_duration: Nếu True, không check duration (scan nhanh hơn)
            
        Returns:
            Dict {folder_path: [VideoScanResult]}
        """
        if not folder_paths:
            return {}
        
        if max_workers is None:
            max_workers = min(len(folder_paths), os.cpu_count() * 2)
        
        logger.info(f"Scanning {len(folder_paths)} folders in parallel with {max_workers} workers")
        
        results = {}
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {
                executor.submit(self.scan_folder_recursive, path, skip_duration): path 
                for path in folder_paths
            }
            
            for future in as_completed(future_to_path):
                folder_path = future_to_path[future]
                try:
                    videos = future.result()
                    results[folder_path] = videos
                except Exception as e:
                    logger.error(f"Error scanning {folder_path}: {e}")
                    results[folder_path] = []
        
        total_videos = sum(len(vids) for vids in results.values())
        logger.info(f"Parallel scan complete: {total_videos} total videos, cache hits: {self.cache_hits}, misses: {self.cache_misses}")
        
        return results
    
    def get_videos_lazy(self, folder_path: str) -> Generator[VideoScanResult, None, None]:
        """
        Generator để scan videos từng cái một (memory efficient).
        Dùng khi folder rất lớn.
        """
        folder_path_obj = Path(folder_path)
        
        for video_file in folder_path_obj.rglob('*'):
            if video_file.is_file() and video_file.suffix.lower() in VIDEO_EXTENSIONS:
                video_result = self.scan_video_with_cache(str(video_file))
                if video_result:
                    yield video_result
    
    def cleanup_invalid_cache(self) -> int:
        """
        Xóa cache entries cho files không còn tồn tại.
        
        Returns:
            Số entries đã xóa
        """
        from video_management.models import LocalVideoFile
        
        deleted_count = 0
        
        for cached in LocalVideoFile.objects.all():
            if not os.path.isfile(cached.file_path):
                cached.delete()
                deleted_count += 1
        
        logger.info(f"Cleanup invalid cache: deleted {deleted_count} entries")
        return deleted_count
    
    def cleanup_old_cache(self, days: int = 30) -> int:
        """
        Xóa cache entries không được access trong X ngày.
        
        Args:
            days: Số ngày threshold
            
        Returns:
            Số entries đã xóa
        """
        from video_management.models import LocalVideoFile
        
        threshold = timezone.now() - timedelta(days=days)
        deleted, _ = LocalVideoFile.objects.filter(last_accessed_at__lt=threshold).delete()
        
        logger.info(f"Cleanup old cache: deleted {deleted} entries (>{days} days old)")
        return deleted
    
    def get_cache_stats(self) -> Dict:
        """
        Lấy thống kê cache.
        
        Returns:
            Dict với stats: total, hit_rate, avg_duration, etc.
        """
        from video_management.models import LocalVideoFile
        
        total = LocalVideoFile.objects.count()
        
        if total == 0:
            return {
                'total_cached': 0,
                'cache_hits': self.cache_hits,
                'cache_misses': self.cache_misses,
                'hit_rate': 0.0
            }
        
        stats = LocalVideoFile.objects.aggregate(
            avg_duration=django_models.Avg('duration'),
            total_size=django_models.Sum('file_size'),
            total_videos=django_models.Count('id')
        )
        
        total_requests = self.cache_hits + self.cache_misses
        hit_rate = (self.cache_hits / total_requests * 100) if total_requests > 0 else 0
        
        return {
            'total_cached': stats['total_videos'] or 0,
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'hit_rate': round(hit_rate, 2),
            'avg_duration': round(stats['avg_duration'] or 0, 2),
            'total_size_mb': round((stats['total_size'] or 0) / 1024 / 1024, 2),
        }
