"""
⚠️ DEPRECATED: This module contains OLD realtime mix-video logic.

NEW APPROACH: Use smart_mix_video_views.py instead!

Performance comparison:
- OLD (this file): 2-3 minutes per mix (slow encoding)
- NEW (smart_mix): 5-13 seconds per mix (pre-processing + lazy loading)

This file is KEPT for:
1. Backward compatibility
2. Fallback if smart preprocessing fails
3. Reference for old logic

TODO: Remove this file after migrating all users to smart_mix_video_views.py
"""

import os
import re
import random
import itertools
import time
import subprocess
import tempfile
import threading
import uuid
import logging
from pathlib import Path
from datetime import datetime
import shutil
from typing import Tuple, List, Optional, Dict, Any

from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)

logger.warning("⚠️ Using DEPRECATED mix_video_views.py - Consider migrating to smart_mix_video_views.py for 20-30x performance!")

ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v'}

PARTS_CONFIG = [
    {"type": "single", "duration": 5},
    {"type": "single", "duration": 5},
    {"type": "stack", "duration": 5},
    {"type": "stack", "duration": 5},
    {"type": "stack", "duration": 5},
    {"type": "single", "duration": 5},
    {"type": "single", "duration": 5},
]
REQUIRED_FILES = 10

CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _get_output_size(request) -> Tuple[int, int]:
    """
    Lấy width, height cho output từ request (form width/height) hoặc settings.
    Trả về (width, height). height=0 nghĩa là scale theo width giữ tỉ lệ.
    """
    width = getattr(settings, "MIX_VIDEO_OUTPUT_WIDTH", 1080) or 1080
    height = getattr(settings, "MIX_VIDEO_OUTPUT_HEIGHT", 1920) or 1920
    try:
        if request.POST.get("width"):
            width = int(request.POST.get("width"))
        if request.POST.get("height"):
            height = int(request.POST.get("height"))
    except (TypeError, ValueError):
        pass
    width = max(2, min(width, 4096))
    if width % 2:
        width += 1
    if height:
        height = max(2, min(height, 4096))
        if height % 2:
            height += 1
    return width, height


def _scale_filter(width: int, height: int) -> str:
    """Scale + pad về đúng width x height."""
    if not height:
        return f"scale={width}:-2"
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"


def _scale_filter_half_height(width: int, half_height: int) -> str:
    """Scale + pad cho mỗi nửa khi stack (mỗi stream → width x half_height)."""
    return f"scale={width}:{half_height}:force_original_aspect_ratio=decrease,pad={width}:{half_height}:(ow-iw)/2:(oh-ih)/2"


def _scale_to_width_then_pad(width: int, height_target: int) -> str:
    """Scale cả hai video cùng chiều rộng (width), crop hoặc pad đến height_target. Hai phần trên/dưới có content cùng rộng.
    Pad không được dùng để thu nhỏ → dùng crop khi chiều cao sau scale > height_target, rồi pad khi < height_target.
    """
    # scale=width:-2 (cùng rộng) → crop xuống height_target nếu cao hơn (center crop) → pad lên height_target nếu thấp hơn
    # Trong min(ih,H) dấu phẩy escape \\ để FFmpeg không hiểu là phân tách tham số filter
    m = f"min(ih\\,{height_target})"
    return f"scale={width}:-2,crop={width}:{m}:0:(ih-{m})/2,pad={width}:{height_target}:(ow-iw)/2:(oh-ih)/2"


def _get_ffmpeg_cmd() -> str:
    """Đường dẫn ffmpeg: từ settings.FFMPEG_PATH hoặc biến môi trường FFMPEG_PATH hoặc 'ffmpeg' (PATH)."""
    path = (
        getattr(settings, "FFMPEG_PATH", None)
        or os.environ.get("FFMPEG_PATH", "")
        or ""
    )
    if path:
        path = str(path).strip().strip('"').strip("'")
    return path if path else "ffmpeg"


def _get_ffprobe_cmd() -> str:
    """Đường dẫn ffprobe: từ settings.FFPROBE_PATH hoặc env, nếu không có thì suy từ FFMPEG_PATH."""
    path = (
        getattr(settings, "FFPROBE_PATH", None)
        or os.environ.get("FFPROBE_PATH", "")
        or ""
    )
    if path:
        path = str(path).strip().strip('"').strip("'")
        if path and os.path.isfile(path):
            return path
        logger.warning(
            "FFPROBE_PATH không trỏ tới file tồn tại: %s (kiểm tra .env). Đang thử suy từ FFMPEG_PATH.",
            path,
        )
    ffmpeg = _get_ffmpeg_cmd()
    if ffmpeg == "ffmpeg":
        return "ffprobe"
    # C:\...\ffmpeg.exe → C:\...\ffprobe.exe
    base = ffmpeg.replace("ffmpeg.exe", "").replace("ffmpeg", "")
    out = (base + "ffprobe.exe") if ".exe" in ffmpeg else (base + "ffprobe")
    if os.path.isfile(out):
        return out
    return "ffprobe"


def _get_duration(path: str) -> Optional[float]:
    """Lấy thời lượng video (giây) bằng ffprobe. Thử format rồi stream nếu lỗi. Trả về None nếu lỗi."""
    cmd = _get_ffprobe_cmd()
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        logger.warning("_get_duration: file không tồn tại %s", abs_path)
        return None
    # Kiểm tra ffprobe tồn tại trước khi gọi (tránh WinError 2 khi ffprobe không trong PATH)
    if cmd != "ffprobe" and not os.path.isfile(cmd):
        logger.warning(
            "_get_duration: ffprobe không tìm thấy tại %s. Kiểm tra FFPROBE_PATH trong .env.",
            cmd,
        )
        return None
    # Không dùng PIPE (capture_output) để tránh deadlock trên Windows khi chạy trong thread.
    # Ghi stdout/stderr ra file tạm rồi đọc sau.
    def _run_ffprobe(probe_args: List[str]) -> Optional[float]:
        out_path = None
        err_path = None
        try:
            out_fd = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".ffprobe_out")
            out_path = out_fd.name
            out_fd.close()
            err_fd = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".ffprobe_err")
            err_path = err_fd.name
            err_fd.close()
            with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
                result = subprocess.run(
                    [cmd] + probe_args + [abs_path],
                    stdout=out_file,
                    stderr=err_file,
                    timeout=30,
                    creationflags=CREATION_FLAGS,
                )
            if result.returncode != 0:
                return None
            with open(out_path, "rb") as f:
                raw = f.read().decode("utf-8", errors="replace").strip()
            if raw:
                val = float(raw)
                if val > 0:
                    return val
        except FileNotFoundError:
            logger.warning(
                "_get_duration: ffprobe không tìm thấy (WinError 2). Đặt FFPROBE_PATH trong .env trỏ tới ffprobe.exe.",
            )
            return None
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            logger.debug("_get_duration probe: %s", e)
            return None
        finally:
            for p in (out_path, err_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return None

    # Cách 1: format duration (chuẩn với hầu hết container)
    val = _run_ffprobe(["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1"])
    if val is not None:
        return val
    # Cách 2: stream duration (một số file có stream duration nhưng format duration lỗi/thiếu)
    val = _run_ffprobe(["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1"])
    if val is not None:
        return val
    logger.warning("_get_duration: không đọc được thời lượng cho %s (ffprobe format và stream đều thất bại)", abs_path)
    return None


def _has_audio(path: str) -> bool:
    """Kiểm tra file video có luồng audio không."""
    cmd = _get_ffprobe_cmd()
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return False
    try:
        # Kiểm tra ffprobe tồn tại trước khi gọi
        if cmd != "ffprobe" and not os.path.isfile(cmd):
            return False
            
        result = subprocess.run(
            [cmd, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", abs_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=CREATION_FLAGS,
            text=True,
            timeout=10
        )
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _max_segments_per_file(duration: float, segment_len: int) -> int:
    """Số đoạn không chồng lấn (segment_len giây) có thể lấy từ video duration giây."""
    if duration < segment_len:
        return 1
    return max(1, int(duration - segment_len) // segment_len + 1)


def _possible_starts(duration: float, segment_len: float) -> List[float]:
    """Danh sách vị trí bắt đầu có thể cho đoạn segment_len giây.
    Nếu dư giây cuối, lấy thêm 1 đoạn (overlap) để tận dụng hết video."""
    if duration < segment_len:
        return [0.0]
    
    starts = []
    current = 0.0
    # Lấy các đoạn không chồng lấn
    while current + segment_len <= duration:
        starts.append(round(current, 2))
        current += segment_len
    
    # Nếu đoạn cuối cùng chưa chạm tới hết video (còn dư), thêm đoạn cuối cùng (chồng lấn)
    # Chỉ thêm nếu nó lệch đáng kể (>0.1s) so với điểm bắt đầu cuối cùng
    last_possible = round(duration - segment_len, 2)
    if not starts or last_possible > starts[-1] + 0.1:
        starts.append(last_possible)
        
    return starts


def _random_starts(duration: float, segment_len: float, count: int) -> List[float]:
    """Lấy count vị trí bắt đầu ngẫu nhiên cho đoạn segment_len giây."""
    if duration < segment_len or count <= 0:
        return [0.0] * max(1, count)
    
    possible = _possible_starts(duration, segment_len)
    if count <= len(possible):
        return random.sample(possible, count)
    
    out = list(possible)
    while len(out) < count:
        out.append(random.choice(possible))
    return out


def _run_ffmpeg(args: List[str], timeout: int = 600, step_name: Optional[str] = None) -> Tuple[bool, str]:
    """
    Chạy ffmpeg an toàn trên Windows: không dùng PIPE (tránh deadlock khi buffer đầy).
    Stdout bỏ qua (DEVNULL); stderr ghi vào file tạm, chỉ đọc khi lỗi.
    step_name: nếu có thì ghi 1 dòng log để biết bước đang chạy (không flood).
    """
    cmd = _get_ffmpeg_cmd()
    if cmd != "ffmpeg" and not os.path.isfile(cmd):
        return False, (
            f"FFmpeg không tìm thấy tại: {cmd}. "
            "Kiểm tra FFMPEG_PATH trong .env có đúng đường dẫn tới file ffmpeg.exe không."
        )
    if step_name:
        logger.info("FFmpeg: %s", step_name)
    stderr_fd = None
    stderr_path = None
    try:
        stderr_fd = tempfile.NamedTemporaryFile(mode="wb", delete=False, suffix=".ffmpeg_stderr")
        stderr_path = stderr_fd.name
        stderr_fd.close()
        stderr_fd = None
        with open(stderr_path, "wb") as stderr_file:
            result = subprocess.run(
                [cmd, "-y"] + args,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                timeout=timeout,
                creationflags=CREATION_FLAGS,
            )
        if result.returncode != 0:
            try:
                with open(stderr_path, "rb") as f:
                    err_bytes = f.read()
                err_text = err_bytes.decode("utf-8", errors="replace").strip()
                if len(err_text) > 2000:
                    err_text = "... " + err_text[-1996:]
                return False, err_text or "FFmpeg failed"
            except OSError:
                return False, "FFmpeg failed (could not read stderr)"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timeout"
    except FileNotFoundError:
        return False, (
            f"FFmpeg not found (đã dùng: {cmd}). "
            "Cài FFmpeg và thêm vào PATH, hoặc đặt FFMPEG_PATH trong .env "
            "(ví dụ: FFMPEG_PATH=C:/ffmpeg/bin/ffmpeg.exe)."
        )
    except Exception as e:
        return False, str(e)
    finally:
        if stderr_path and os.path.exists(stderr_path):
            try:
                os.remove(stderr_path)
            except OSError:
                pass


def _trim_single_to_file(
    input_path: str,
    output_path: str,
    duration_sec: float,
    width: int,
    height: int,
    start_sec: float = 0,
    keep_audio: bool = False,
    step_name: Optional[str] = None,
) -> Tuple[bool, str]:
    # Thêm fps=30 để đồng bộ tốc độ khung hình
    # Thêm setsar=1 để ép pixel vuông (1:1) tránh lỗi concat do lệch SAR
    # Buộc dùng yuv420p để tránh lỗi profile/level khi nối
    vf = f"{_scale_filter(width, height)},fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS"
    
    # Kiểm tra audio nếu muốn giữ
    use_source_audio = keep_audio and _has_audio(input_path)

    args = [
        "-ss", str(start_sec),
        "-i", input_path,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(duration_sec),
        "-vf", vf,
        "-map", "0:v:0",
    ]
    
    if use_source_audio:
        # Lấy audio từ source, nếu lệch độ dài thì sẽ tự trim theo -t
        args.extend(["-map", "0:a:0"])
    else:
        # Luôn dùng audio im lặng để đồng nhất các phần khi concat
        args.extend(["-map", "1:a"])

    args.extend([
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2",
        "-video_track_timescale", "15360",
        "-map_metadata", "-1",
        output_path,
    ])
    return _run_ffmpeg(args, step_name=step_name)


def _stack_two_to_file(
    path_top: str,
    path_bottom: str,
    output_path: str,
    duration_sec: float,
    width: int,
    height: int,
    start_top: float = 0,
    start_bottom: float = 0,
    step_name: Optional[str] = None,
) -> Tuple[bool, str]:
    """Ghép 2 video trên/dưới (vstack): trên 3/10 chiều cao, dưới 7/10. Lấy duration_sec, scale, ghi output_path."""
    # Đảm bảo height hợp lệ (stack cần height >= 4)
    height = max(4, height)
    if height % 2:
        height += 1
    top_h = max(2, (int(round(height * 3 / 10)) // 2) * 2)
    bottom_h = height - top_h
    if bottom_h < 2:
        bottom_h = 2
        top_h = height - bottom_h

    # Kích thước vùng giao thoa (overlap)
    overlap = 50 
    
    scale_top = _scale_to_width_then_pad(width, top_h + overlap)
    scale_bottom = _scale_to_width_then_pad(width, bottom_h + overlap)
    
    filter_complex = (
        f"[0:v]{scale_top},fps=30,setsar=1,format=yuva420p,"
        f"geq=lum='p(X,Y)':a='if(gte(Y,H-{overlap}),255*(H-Y)/{overlap},255)',"
        f"setpts=PTS-STARTPTS[top];"
        
        f"[1:v]{scale_bottom},fps=30,setsar=1,format=yuva420p,"
        f"pad={width}:{height}:0:{top_h - overlap},"
        f"setpts=PTS-STARTPTS[bot];"
        
        "[bot][top]overlay=0:0:format=auto,format=yuv420p,setpts=PTS-STARTPTS[v]"
    )
    # Thêm audio im lặng để concat không lỗi khi các đoạn single có audio
    args = [
        "-ss", str(start_top),
        "-i", path_top,
        "-ss", str(start_bottom),
        "-i", path_bottom,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(duration_sec),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-c:a", "aac", "-ar", "44100",
        "-video_track_timescale", "15360",
        output_path,
    ]
    return _run_ffmpeg(args, step_name=step_name)


def _replace_audio(video_path: str, audio_path: str, output_path: str) -> Tuple[bool, str]:
    """Thay thế audio của video bằng file audio khác."""
    args = [
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_path
    ]
    return _run_ffmpeg(args, step_name="replace audio")


def _concat_files(part_files: List[str], output_path: str, step_name: Optional[str] = None) -> Tuple[bool, str]:
    """Nối danh sách file video bằng concat demuxer."""
    if not part_files:
        return False, "Không có file để nối."
    
    list_file = output_path + ".list.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in part_files:
                # FFmpeg concat demuxer yêu cầu thoát dấu nháy đơn
                safe_p = os.path.abspath(p).replace("'", "'\\''")
                f.write(f"file '{safe_p}'\n")
        
        args = [
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-c:a", "aac",
            "-ar", "44100",
            "-ac", "2",
            output_path
        ]
        return _run_ffmpeg(args, step_name=step_name)
    finally:
        if os.path.exists(list_file):
            try: os.remove(list_file)
            except OSError: pass


def _build_one_mix(
    input_paths: List[str],
    starts: List[float],
    output_path: str,
    width: int,
    height: int,
    durations: List[float],
    audio_path: Optional[str] = None,
    check_cancellation: Optional[callable] = None,
) -> Tuple[bool, str]:
    """
    Tạo 1 video mix từ 10 input.
    durations: list 7 float cho 7 phần.
    Nếu audio_path có, nó chỉ được gắn cho 6 phần đầu.
    """
    if check_cancellation and check_cancellation():
        return False, "cancelled"

    if len(input_paths) != REQUIRED_FILES or len(starts) != REQUIRED_FILES:
        return False, f"Cần đúng {REQUIRED_FILES} file và {REQUIRED_FILES} start."
    if len(durations) != 7:
        return False, "Cần đúng 7 giá trị thời lượng cho 7 phần."

    if height <= 0:
        height = max(360, (width * 16) // 9)
        if height % 2:
            height += 1

    temp_dir = tempfile.mkdtemp(prefix="mix_step_")
    part_files = []
    try:
        for i in range(7):
            part_files.append(os.path.join(temp_dir, f"seg_{i}.mp4"))

        if check_cancellation and check_cancellation(): return False, "cancelled"
        
        # Part 1: Folder 0
        ok, err = _trim_single_to_file(
            input_paths[0], part_files[0], durations[0], width, height, start_sec=starts[0], step_name="trim part 1"
        )
        if not ok: return False, f"Phần 1: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 2: Folder 1
        ok, err = _trim_single_to_file(
            input_paths[1], part_files[1], durations[1], width, height, start_sec=starts[1], step_name="trim part 2"
        )
        if not ok: return False, f"Phần 2: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 3: Folders 2, 3
        ok, err = _stack_two_to_file(
            input_paths[2], input_paths[3], part_files[2], durations[2], width, height,
            start_top=starts[2], start_bottom=starts[3], step_name="stack part 3",
        )
        if not ok: return False, f"Phần 3: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 4: Folders 4, 5
        ok, err = _stack_two_to_file(
            input_paths[4], input_paths[5], part_files[3], durations[3], width, height,
            start_top=starts[4], start_bottom=starts[5], step_name="stack part 4",
        )
        if not ok: return False, f"Phần 4: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 5: Folders 6, 7
        ok, err = _stack_two_to_file(
            input_paths[6], input_paths[7], part_files[4], durations[4], width, height,
            start_top=starts[6], start_bottom=starts[7], step_name="stack part 5",
        )
        if not ok: return False, f"Phần 5: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 6: Folder 8
        ok, err = _trim_single_to_file(
            input_paths[8], part_files[5], durations[5], width, height, start_sec=starts[8], step_name="trim part 6"
        )
        if not ok: return False, f"Phần 6: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        # Part 7: Folder 9 (Outrol) - Giữ audio gốc
        ok, err = _trim_single_to_file(
            input_paths[9], part_files[6], durations[6], width, height, 
            start_sec=starts[9], keep_audio=True, step_name="trim part 7"
        )
        if not ok: return False, f"Phần 7: {err}"
        if check_cancellation and check_cancellation(): return False, "cancelled"

        if audio_path and os.path.exists(audio_path):
            # Bước A: Nối 6 phần đầu (đang là âm thanh im lặng)
            intro_silent = os.path.join(temp_dir, "intro_silent.mp4")
            ok, err = _concat_files(part_files[:6], intro_silent, step_name="concat intro segments")
            if not ok: return False, f"Nối 6 đoạn đầu: {err}"
            if check_cancellation and check_cancellation(): return False, "cancelled"

            # Bước B: Gắn nhạc vào 6 phần đầu
            intro_with_music = os.path.join(temp_dir, "intro_music.mp4")
            ok, err = _replace_audio(intro_silent, audio_path, intro_with_music)
            if not ok: return False, f"Gắn nhạc intro: {err}"
            if check_cancellation and check_cancellation(): return False, "cancelled"

            # Bước C: Nối Intro (có nhạc) + Outrol (có audio gốc)
            # Dùng _concat_files có re-encode để khớp các luồng audio khác nhau
            ok, err = _concat_files([intro_with_music, part_files[6]], output_path, step_name="final join music and outrol")
            if not ok: return False, f"Nối intro và outrol: {err}"
            if check_cancellation and check_cancellation(): return False, "cancelled"
        else:
            # Fallback nếu không có audio_path
            ok, err = _concat_files(part_files, output_path, step_name="concat all fallback")
            if not ok: return False, f"Concat: {err}"

        return True, ""
    finally:
        # Dọn dẹp hết temp_dir (bao gồm cả các file trung gian intro_silent...)
        shutil.rmtree(temp_dir, ignore_errors=True)


# Lưu tiến trình mix theo progress_id (khi chạy mix trong thread)
_mix_progress: Dict[str, Any] = {}
_mix_progress_lock = threading.Lock()


def _run_mix_task(temp_dir: str, progress_id: str, width: int, height: int, num_outputs_cap: int) -> None:
    """Chạy logic mix trong thread, cập nhật _mix_progress[progress_id]."""
    try:
        # Get fast_mode and videos_per_folder from progress dict
        with _mix_progress_lock:
            fast_mode = _mix_progress.get(progress_id, {}).get('fast_mode', False)
            videos_per_folder_limit = _mix_progress.get(progress_id, {}).get('videos_per_folder', 10)
        
        # Set FFmpeg preset based on fast_mode
        ffmpeg_preset = "ultrafast" if fast_mode else "veryfast"
        ffmpeg_crf = "23"  # CRF 23 = default quality (lower = better)
        
        # Store in progress dict for helper functions to access
        with _mix_progress_lock:
            _mix_progress[progress_id]['ffmpeg_preset'] = ffmpeg_preset
            _mix_progress[progress_id]['ffmpeg_crf'] = ffmpeg_crf
        
        logger.info(f"Mix task started - Fast mode: {fast_mode}, Preset: {ffmpeg_preset}, CRF: {ffmpeg_crf}, Videos/folder limit: {videos_per_folder_limit}")
        
        # Import cache service
        from video_management.services.video_cache_service import get_cache_service
        cache_service = get_cache_service()
        
        # Kiểm tra chế độ
        audio_path = None
        for f in os.listdir(temp_dir):
            if f.startswith("audio"):
                audio_path = os.path.join(temp_dir, f)
                break
        
        folders_mode = os.path.isdir(os.path.join(temp_dir, "folder_0"))
        
        audio_dur = _get_duration(audio_path) if audio_path else 35.0
        if not audio_dur: audio_dur = 35.0
        
        combinations = [] # List of (combo, durations)
        
        if folders_mode:
            logger.info("Using folders mode with cache service for optimal performance")
            
            # Scan folder 9 (Outtrol) với cache - limit videos
            f9_dir = os.path.join(temp_dir, "folder_9")
            f9_videos_all = cache_service.scan_folder_recursive(f9_dir)
            f9_videos = f9_videos_all[:videos_per_folder_limit]  # LIMIT!
            logger.info(f"Folder 9: Using {len(f9_videos)} of {len(f9_videos_all)} videos")
            
            if not f9_videos:
                raise ValueError("Folder 9 (Outtrol) không có video hợp lệ.")
            
            # 1. Thu thập dữ liệu cho từng Outtrol với parallel scan
            logger.info(f"Scanning folders 0-8 in parallel (limit: {videos_per_folder_limit} videos/folder)...")
            folder_paths = [os.path.join(temp_dir, f"folder_{i}") for i in range(9)]
            folders_results_all = cache_service.scan_folders_parallel(folder_paths)
            
            # Limit videos in each folder
            folders_results = []
            for result in folders_results_all:
                limited_videos = result[:videos_per_folder_limit]
                folders_results.append(limited_videos)
                logger.info(f"Folder: Using {len(limited_videos)} of {len(result)} videos")
            
            # Log cache statistics
            stats = cache_service.get_cache_stats()
            logger.info(f"Cache stats - Hit rate: {stats['hit_rate']}%, Hits: {stats['cache_hits']}, Misses: {stats['cache_misses']}")
            
            outtrol_contexts = []
            total_possible = 0
            
            for f9_video in f9_videos:
                f9 = f9_video.path
                d9 = f9_video.duration
                slen = audio_dur / 6.0
                
                # Part durations cho Outtrol này
                part_durs = [slen] * 6 + [d9]
                
                # Xây dựng pools cho Folders 0-8 với slen tương ứng
                pools = []
                p_count = 1
                
                for i in range(9):
                    folder_key = os.path.join(temp_dir, f"folder_{i}")
                    videos_i = folders_results.get(folder_key, [])
                    
                    if not videos_i:
                        raise ValueError(f"Folder {i} không có video hợp lệ.")
                    
                    folder_segments = []
                    for video_result in videos_i:
                        p_v = video_result.path
                        d_v = video_result.duration
                        starts = _possible_starts(d_v, slen)
                        for s in starts:
                            folder_segments.append((p_v, s))
                    
                    pools.append(folder_segments)
                    p_count *= len(folder_segments)
                
                outtrol_contexts.append({
                    "f9": f9,
                    "pools": pools, # Folders 0-8
                    "part_durs": part_durs,
                    "possible": p_count
                })
                total_possible += p_count
            
            logger.info(f"Total possible combinations: {total_possible}")

            # 2. Bốc ngẫu nhiên tổ hợp (bao gồm cả Outtrol)
            used_overall = set()
            max_tries = num_outputs_cap * 10 
            tries = 0
            
            while len(combinations) < num_outputs_cap and \
                  len(used_overall) < total_possible and \
                  tries < max_tries:
                
                # Bốc ngẫu nhiên 1 Outtrol
                ctx = random.choice(outtrol_contexts)
                
                # Bốc ngẫu nhiên index cho 9 folder đầu
                indices = tuple(random.randrange(len(p)) for p in ctx["pools"])
                
                # Dấu vân tay = (Đường dẫn Outtrol, indices 0-8)
                fingerprint = (ctx["f9"], indices)
                
                if fingerprint not in used_overall:
                    used_overall.add(fingerprint)
                    
                    # Tạo combo: 9 đoạn đầu + đoạn Outtrol
                    combo = [ctx["pools"][i][indices[i]] for i in range(9)]
                    combo.append((ctx["f9"], 0.0))
                    
                    combinations.append((tuple(combo), ctx["part_durs"]))
                
                tries += 1
        else:
            # Chế độ cũ (10 files đơn) - Giữ logic 5s cho tương thích hoặc fallback
            target_dur = 5.0
            parts = list(Path(temp_dir).glob("part_*"))
            parts.sort(key=lambda p: int(re.search(r"part_(\d+)", p.name).group(1)))
            input_paths = [str(p) for p in parts[:REQUIRED_FILES]]
            if len(input_paths) < REQUIRED_FILES:
                raise ValueError(f"Thiếu file (cần {REQUIRED_FILES}).")
            
            selected_paths = []
            starts = []
            for p_v in input_paths:
                d_v = _get_duration(p_v) or target_dur
                s_list = _random_starts(d_v, target_dur, 1)
                selected_paths.append(p_v)
                starts.append(s_list[0])
            
            combinations.append(((selected_paths, starts), [5.0]*7))

        num_outputs = len(combinations)
        num_outputs = max(1, num_outputs)

        with _mix_progress_lock:
            _mix_progress[progress_id]["num_outputs"] = num_outputs

        out_dir = Path(settings.MEDIA_ROOT) / "mix_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_urls = []
        output_filenames = []
        base_id = uuid.uuid4().hex[:12]

        for k, (combo_data, part_durs) in enumerate(combinations):
            # Check for cancellation
            with _mix_progress_lock:
                if _mix_progress.get(progress_id, {}).get("status") == "cancelled":
                    logger.info("Mix task %s cancelled by user.", progress_id)
                    return

            # Với mode folders, combo_data là danh sách (path, start)
            # Với mode cũ, combo_data là (paths, starts)
            if folders_mode:
                selected_paths = [c[0] for c in combo_data]
                starts = [c[1] for c in combo_data]
            else:
                selected_paths = combo_data[0]
                starts = combo_data[1]
            
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"mixed_{timestamp_str}_{base_id}_{k}.mp4"
            output_path = out_dir / output_filename
            
            def check_cancel():
                with _mix_progress_lock:
                    return _mix_progress.get(progress_id, {}).get("status") == "cancelled"

            ok, err = _build_one_mix(
                selected_paths, starts, str(output_path), width, height, 
                durations=part_durs, audio_path=audio_path,
                check_cancellation=check_cancel
            )
            
            if not ok:
                if err == "cancelled":
                    logger.info("Mix task %s cancelled mid-build.", progress_id)
                    return
                with _mix_progress_lock:
                    _mix_progress[progress_id].update(status="error", error=f"Lỗi ghép video (mix {k + 1}): {err}")
                return

            output_urls.append(f"{settings.MEDIA_URL.rstrip('/')}/mix_output/{output_filename}")
            output_filenames.append(output_filename)
            with _mix_progress_lock:
                _mix_progress[progress_id]["percent"] = round((k + 1) / num_outputs * 100)

        with _mix_progress_lock:
            _mix_progress[progress_id].update(
                status="done",
                percent=100,
                output_urls=output_urls,
                output_filenames=output_filenames,
                output_url=output_urls[0],
                output_filename=output_filenames[0],
                num_outputs=num_outputs,
            )
    except Exception as e:
        logger.exception("Mix task error: %s", e)
        with _mix_progress_lock:
            _mix_progress[progress_id].update(status="error", error=str(e))
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


@api_view(['POST'])
def mix_videos(request):
    """
    POST: multipart/form-data OR JSON with local folder paths.
    
    Chế độ 1 (upload): 10 files (videos).
    Chế độ 2 (upload): audio + folder_0..9 files.
    Chế độ 3 (path):   JSON { "audio_path" or audio file, "folder_paths": [...10 paths...], "width", "height", "num_outputs" }
    """
    # Check if this is a path-based request (JSON body with folder_paths)
    content_type = request.content_type or ''
    
    if 'application/json' in content_type:
        return _mix_videos_path_mode(request)
    
    # Check if form data has folder_paths field (multipart with paths)
    folder_paths_raw = request.POST.getlist('folder_paths')
    if folder_paths_raw or request.POST.get('folder_paths_0'):
        return _mix_videos_path_mode_form(request)
    
    # Original upload-based modes
    width, height = _get_output_size(request)
    try:
        requested = int(request.POST.get("num_outputs", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    num_outputs_cap = max(1, min(100, requested)) if requested else 100

    temp_dir = tempfile.mkdtemp(prefix='mix_video_')
    folder_mode = ('audio' in request.FILES)
    
    try:
        if folder_mode:
            audio_f = request.FILES['audio']
            ext_a = Path(audio_f.name).suffix or '.mp3'
            audio_p = os.path.join(temp_dir, f"audio{ext_a}")
            with open(audio_p, 'wb') as out:
                for chunk in audio_f.chunks(): out.write(chunk)
            
            for i in range(10):
                f_path = os.path.join(temp_dir, f"folder_{i}")
                os.makedirs(f_path, exist_ok=True)
                key = f'folder_{i}'
                files_list = request.FILES.getlist(key)
                for j, f in enumerate(files_list):
                    ext_v = Path(f.name).suffix or '.mp4'
                    if ext_v.lower() in ALLOWED_EXTENSIONS:
                        p_v = os.path.join(f_path, f"v_{j}{ext_v}")
                        with open(p_v, 'wb') as out:
                            for chunk in f.chunks(): out.write(chunk)
        else:
            files = []
            if request.FILES:
                for key in request.FILES:
                    if key == 'audio': continue
                    for f in request.FILES.getlist(key):
                        if Path(f.name).suffix.lower() in ALLOWED_EXTENSIONS:
                            files.append(f)
            if len(files) < REQUIRED_FILES:
                shutil.rmtree(temp_dir)
                return Response({'error': f'Cần {REQUIRED_FILES} video.'}, status=status.HTTP_400_BAD_REQUEST)
            
            for i, f in enumerate(files[:REQUIRED_FILES]):
                ext = Path(f.name).suffix or '.mp4'
                with open(os.path.join(temp_dir, f"part_{i}{ext}"), 'wb') as out:
                    for chunk in f.chunks(): out.write(chunk)

    except Exception as e:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    progress_id = uuid.uuid4().hex
    with _mix_progress_lock:
        _mix_progress[progress_id] = {
            "status": "processing", "percent": 0, "num_outputs": None, "error": None,
            "output_urls": None, "output_filenames": None, "output_url": None, "output_filename": None,
        }
    
    threading.Thread(
        target=_run_mix_task,
        args=(temp_dir, progress_id, width, height, num_outputs_cap),
        daemon=True
    ).start()
    
    return Response({"progress_id": progress_id}, status=status.HTTP_202_ACCEPTED)


def _mix_videos_path_mode_form(request):
    """Handle mix with local folder paths sent via multipart form (audio file + folder paths)."""
    width, height = _get_output_size(request)
    try:
        requested = int(request.POST.get("num_outputs", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    num_outputs_cap = max(1, min(100, requested)) if requested else 100

    # Get videos_per_folder limit
    try:
        videos_per_folder = int(request.POST.get("videos_per_folder", 10) or 10)
        videos_per_folder = max(1, min(100, videos_per_folder))  # Clamp 1-100
    except (TypeError, ValueError):
        videos_per_folder = 10
    
    # Get fast_mode (NEW)
    fast_mode = request.POST.get("fast_mode", "false").lower() == "true"
    
    logger.info(f"Videos per folder limit: {videos_per_folder}, Fast mode: {fast_mode}")

    # Collect folder paths
    folder_paths = []
    for i in range(10):
        p = request.POST.get(f'folder_paths_{i}', '').strip()
        folder_paths.append(p)

    temp_dir = tempfile.mkdtemp(prefix='mix_path_')
    
    try:
        # Save audio file
        if 'audio' in request.FILES:
            audio_f = request.FILES['audio']
            ext_a = Path(audio_f.name).suffix or '.mp3'
            audio_p = os.path.join(temp_dir, f"audio{ext_a}")
            with open(audio_p, 'wb') as out:
                for chunk in audio_f.chunks(): out.write(chunk)
        elif request.POST.get('audio_path'):
            audio_src = request.POST.get('audio_path').strip()
            if not os.path.isfile(audio_src):
                shutil.rmtree(temp_dir)
                return Response({'error': f'Audio file not found: {audio_src}'}, status=status.HTTP_400_BAD_REQUEST)
            ext_a = Path(audio_src).suffix or '.mp3'
            shutil.copy2(audio_src, os.path.join(temp_dir, f"audio{ext_a}"))
        else:
            shutil.rmtree(temp_dir)
            return Response({'error': 'Audio file or audio_path required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create folders with limited videos (symlink individual files for speed)
        for i, fp in enumerate(folder_paths):
            folder_path = os.path.join(temp_dir, f"folder_{i}")
            os.makedirs(folder_path, exist_ok=True)
            
            if fp and os.path.isdir(fp):
                # Fast scan: only check top-level files, no deep recursion
                video_files = []
                try:
                    # List files in root folder only (fast)
                    for item in os.listdir(fp):
                        if len(video_files) >= videos_per_folder:
                            break
                        item_path = os.path.join(fp, item)
                        if os.path.isfile(item_path):
                            ext = Path(item).suffix.lower()
                            if ext in ALLOWED_EXTENSIONS:
                                video_files.append(item_path)
                    
                    # If not enough, scan one level deeper
                    if len(video_files) < videos_per_folder:
                        for item in os.listdir(fp):
                            if len(video_files) >= videos_per_folder:
                                break
                            item_path = os.path.join(fp, item)
                            if os.path.isdir(item_path):
                                try:
                                    for subitem in os.listdir(item_path):
                                        if len(video_files) >= videos_per_folder:
                                            break
                                        subitem_path = os.path.join(item_path, subitem)
                                        if os.path.isfile(subitem_path):
                                            ext = Path(subitem).suffix.lower()
                                            if ext in ALLOWED_EXTENSIONS:
                                                video_files.append(subitem_path)
                                except (PermissionError, OSError):
                                    pass
                except (PermissionError, OSError) as e:
                    logger.warning(f"Cannot scan {fp}: {e}")
                
                # Symlink individual files (fast, works with network paths)
                for j, src_video in enumerate(video_files[:videos_per_folder]):
                    ext = Path(src_video).suffix
                    dst_video = os.path.join(folder_path, f"video_{j}{ext}")
                    try:
                        # Try symlink first (fast if supported)
                        os.symlink(src_video, dst_video)
                    except (OSError, NotImplementedError):
                        # Fallback: copy file (works everywhere)
                        shutil.copy2(src_video, dst_video)
                
                logger.info(f"Folder {i}: Collected {len(video_files[:videos_per_folder])} videos from {fp}")
            else:
                if fp:
                    logger.warning(f"Folder path not found: {fp}")

    except Exception as e:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    progress_id = uuid.uuid4().hex
    with _mix_progress_lock:
        _mix_progress[progress_id] = {
            "status": "processing", "percent": 0, "num_outputs": None, "error": None,
            "output_urls": None, "output_filenames": None, "output_url": None, "output_filename": None,
            "fast_mode": fast_mode,  # Store fast_mode in progress dict
            "videos_per_folder": videos_per_folder,  # Store limit
        }

    # Note: don't rmtree temp_dir in _run_mix_task if symlinks are used
    # The task already handles cleanup
    threading.Thread(
        target=_run_mix_task,
        args=(temp_dir, progress_id, width, height, num_outputs_cap),
        daemon=True
    ).start()

    return Response({"progress_id": progress_id}, status=status.HTTP_202_ACCEPTED)


def _mix_videos_path_mode(request):
    """Handle mix with local folder paths sent via JSON body."""
    import json as json_mod
    try:
        data = json_mod.loads(request.body)
    except Exception:
        return Response({'error': 'Invalid JSON'}, status=status.HTTP_400_BAD_REQUEST)
    
    folder_paths = data.get('folder_paths', [])
    audio_path = data.get('audio_path', '')
    width = int(data.get('width', 720))
    height = int(data.get('height', 1280))
    
    try:
        requested = int(data.get("num_outputs", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    num_outputs_cap = max(1, min(100, requested)) if requested else 100

    if not audio_path or not os.path.isfile(audio_path):
        return Response({'error': f'Audio file not found: {audio_path}'}, status=status.HTTP_400_BAD_REQUEST)

    # Pad to 10 folders
    while len(folder_paths) < 10:
        folder_paths.append('')

    temp_dir = tempfile.mkdtemp(prefix='mix_path_')

    try:
        ext_a = Path(audio_path).suffix or '.mp3'
        shutil.copy2(audio_path, os.path.join(temp_dir, f"audio{ext_a}"))

        for i, fp in enumerate(folder_paths[:10]):
            link_path = os.path.join(temp_dir, f"folder_{i}")
            fp = (fp or '').strip()
            if fp and os.path.isdir(fp):
                try:
                    os.symlink(fp, link_path, target_is_directory=True)
                except OSError:
                    shutil.copytree(fp, link_path, dirs_exist_ok=True)
            else:
                os.makedirs(link_path, exist_ok=True)
                if fp:
                    logger.warning(f"Folder path not found: {fp}")

    except Exception as e:
        if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    progress_id = uuid.uuid4().hex
    with _mix_progress_lock:
        _mix_progress[progress_id] = {
            "status": "processing", "percent": 0, "num_outputs": None, "error": None,
            "output_urls": None, "output_filenames": None, "output_url": None, "output_filename": None,
        }

    threading.Thread(
        target=_run_mix_task,
        args=(temp_dir, progress_id, width, height, num_outputs_cap),
        daemon=True
    ).start()

    return Response({"progress_id": progress_id}, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
def scan_folder_batch(request):
    """
    POST /api/videos/scan-folder-batch/
    
    Scan nhiều folders cùng lúc (dành cho BE gọi).
    BE sẽ gửi list paths, AI Service scan và return metadata.
    
    Body (JSON):
    {
        "folder_paths": ["D:\\Videos\\Folder1", "D:\\Videos\\Folder2", ...],
        "use_cache": true,
        "recursive": true
    }
    
    Returns:
    {
        "success": true,
        "results": [
            {
                "path": "D:\\Videos\\Folder1",
                "video_count": 25,
                "videos": [
                    {"path": "...", "duration": 120.5, "has_audio": true},
                    ...
                ]
            },
            ...
        ],
        "cache_stats": {"hit_rate": 95.5, ...}
    }
    """
    import json as json_mod
    from video_management.services.video_cache_service import get_cache_service
    
    try:
        data = json_mod.loads(request.body)
    except Exception:
        return Response({'error': 'Invalid JSON'}, status=status.HTTP_400_BAD_REQUEST)
    
    folder_paths = data.get('folder_paths', [])
    use_cache = data.get('use_cache', True)
    recursive = data.get('recursive', True)
    
    if not folder_paths or not isinstance(folder_paths, list):
        return Response({'error': 'folder_paths is required (array)'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Validate all paths exist
    invalid_paths = []
    valid_paths = []
    for path in folder_paths:
        path = str(path).strip()
        if not path:
            continue
        if not os.path.isdir(path):
            invalid_paths.append(path)
        else:
            valid_paths.append(path)
    
    if invalid_paths:
        return Response({
            'error': 'Some paths not found',
            'invalid_paths': invalid_paths
        }, status=status.HTTP_404_NOT_FOUND)
    
    if not valid_paths:
        return Response({'error': 'No valid paths provided'}, status=status.HTTP_400_BAD_REQUEST)
    
    try:
        cache_service = get_cache_service()
        
        # Parallel scan với cache
        logger.info(f"Batch scanning {len(valid_paths)} folders with cache")
        scan_results = cache_service.scan_folders_parallel(valid_paths)
        
        # Format results
        results = []
        for folder_path in valid_paths:
            videos = scan_results.get(folder_path, [])
            results.append({
                'path': folder_path,
                'name': os.path.basename(folder_path),
                'video_count': len(videos),
                'videos': [
                    {
                        'path': v.path,
                        'duration': v.duration,
                        'has_audio': v.has_audio
                    }
                    for v in videos
                ] if data.get('include_details', False) else None
            })
        
        cache_stats = cache_service.get_cache_stats()
        
        logger.info(
            f"Batch scan complete: {len(results)} folders, "
            f"total {sum(r['video_count'] for r in results)} videos, "
            f"cache hit rate: {cache_stats['hit_rate']}%"
        )
        
        return Response({
            'success': True,
            'results': results,
            'total_folders': len(results),
            'total_videos': sum(r['video_count'] for r in results),
            'cache_stats': cache_stats
        })
        
    except Exception as e:
        logger.exception("Error in batch scan")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
def scan_folder(request):
    """
    POST /api/videos/scan-folder/
    Body: { 
        "path": "D:\\Videos\\MixVideo",
        "use_cache": true,  // Optional: dùng cache để scan nhanh hơn
        "recursive": true   // Optional: scan recursive vào subfolder
    }
    Returns subfolders with video file counts.
    """
    import json as json_mod
    try:
        data = json_mod.loads(request.body)
    except Exception:
        return Response({'error': 'Invalid JSON'}, status=status.HTTP_400_BAD_REQUEST)
    
    folder_path = data.get('path', '').strip()
    use_cache = data.get('use_cache', True)
    recursive = data.get('recursive', True)
    
    if not folder_path:
        return Response({'error': 'path is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not os.path.isdir(folder_path):
        return Response({'error': f'Folder not found: {folder_path}'}, status=status.HTTP_404_NOT_FOUND)
    
    subfolders = []
    root_video_count = 0
    total_video_count = 0
    cache_stats = None
    
    try:
        if use_cache:
            # Dùng cache service để scan nhanh hơn
            from video_management.services.video_cache_service import get_cache_service
            cache_service = get_cache_service()
            
            # Scan root folder
            if recursive:
                root_videos = cache_service.scan_folder_recursive(folder_path)
                root_video_count = len(root_videos)
                total_video_count = root_video_count
            else:
                # Chỉ scan cấp 1
                for f in os.listdir(folder_path):
                    fp = os.path.join(folder_path, f)
                    if os.path.isfile(fp) and Path(f).suffix.lower() in ALLOWED_EXTENSIONS:
                        root_video_count += 1
                total_video_count = root_video_count
            
            # Scan subfolders
            subfolder_paths = []
            for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
                if entry.is_dir():
                    subfolder_paths.append(entry.path)
            
            if subfolder_paths:
                # Scan parallel
                results = cache_service.scan_folders_parallel(subfolder_paths)
                
                for folder_path_key in subfolder_paths:
                    videos = results.get(folder_path_key, [])
                    video_count = len(videos)
                    total_video_count += video_count
                    subfolders.append({
                        'name': os.path.basename(folder_path_key),
                        'path': folder_path_key,
                        'video_count': video_count
                    })
            
            cache_stats = cache_service.get_cache_stats()
        else:
            # Old scan method (không dùng cache)
            # Count videos directly in root folder
            for f in os.listdir(folder_path):
                fp = os.path.join(folder_path, f)
                if os.path.isfile(fp) and Path(f).suffix.lower() in ALLOWED_EXTENSIONS:
                    root_video_count += 1
            total_video_count += root_video_count
            
            # Scan subfolders
            for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
                if entry.is_dir():
                    video_count = 0
                    if recursive:
                        for root, dirs, files in os.walk(entry.path):
                            for f in files:
                                if Path(f).suffix.lower() in ALLOWED_EXTENSIONS:
                                    video_count += 1
                    else:
                        for f in os.listdir(entry.path):
                            fp = os.path.join(entry.path, f)
                            if os.path.isfile(fp) and Path(f).suffix.lower() in ALLOWED_EXTENSIONS:
                                video_count += 1
                    total_video_count += video_count
                    subfolders.append({
                        'name': entry.name,
                        'path': entry.path,
                        'video_count': video_count
                    })
    except PermissionError:
        return Response({'error': f'Permission denied: {folder_path}'}, status=status.HTTP_403_FORBIDDEN)
    except Exception as e:
        logger.exception("Error scanning folder")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    response_data = {
        'success': True,
        'base_path': folder_path,
        'root_video_count': root_video_count,
        'total_video_count': total_video_count,
        'subfolders': subfolders,
        'total_subfolders': len(subfolders),
        'used_cache': use_cache,
        'recursive': recursive
    }
    
    if cache_stats:
        response_data['cache_stats'] = cache_stats
    
    return Response(response_data)


@api_view(['GET'])
def mix_status(request, progress_id: str):
    """GET: trả về tiến trình và kết quả mix theo progress_id."""
    with _mix_progress_lock:
        data = _mix_progress.get(progress_id)
    if not data:
        return Response(
            {"error": "progress_id không tồn tại hoặc đã hết hạn."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(data, status=status.HTTP_200_OK)


@api_view(['POST'])
def mix_cancel(request, progress_id: str):
    """POST: Huỷ bỏ quá trình mix video."""
    with _mix_progress_lock:
        if progress_id in _mix_progress:
            if _mix_progress[progress_id]["status"] == "processing":
                _mix_progress[progress_id]["status"] = "cancelled"
                _mix_progress[progress_id]["error"] = "Quá trình đã bị huỷ bởi người dùng."
                return Response({"message": "Đã gửi yêu cầu huỷ bỏ."}, status=status.HTTP_200_OK)
            return Response({"message": "Quá trình không ở trạng thái có thể huỷ."}, status=status.HTTP_400_BAD_REQUEST)
    
    return Response({"error": "progress_id không tồn tại."}, status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
def mix_videos_auto(request):
    """
    POST: Auto mix video - User chỉ cần cung cấp folder tổng + audio.
    BE tự động scan, tìm 10 subfolder, và mix.
    
    Body (multipart/form-data):
    - base_folder_path: Đường dẫn folder tổng (string)
    - audio: File audio (file)
    - width: Width output (optional, default 720)
    - height: Height output (optional, default 1280)
    - num_outputs: Số video output (optional, default 5)
    - min_videos_per_folder: Số video tối thiểu mỗi folder (optional, default 1)
    
    Returns:
    - progress_id để poll status
    """
    from video_management.services.video_cache_service import get_cache_service
    
    # Get parameters
    base_folder_path = request.POST.get('base_folder_path', '').strip()
    width, height = _get_output_size(request)
    
    try:
        num_outputs = int(request.POST.get('num_outputs', 5) or 5)
    except (TypeError, ValueError):
        num_outputs = 5
    num_outputs = max(1, min(100, num_outputs))
    
    try:
        min_videos = int(request.POST.get('min_videos_per_folder', 1) or 1)
    except (TypeError, ValueError):
        min_videos = 1
    min_videos = max(1, min_videos)
    
    # Validate inputs
    if not base_folder_path:
        return Response({'error': 'base_folder_path is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    if not os.path.isdir(base_folder_path):
        return Response({'error': f'Folder not found: {base_folder_path}'}, status=status.HTTP_404_NOT_FOUND)
    
    if 'audio' not in request.FILES:
        return Response({'error': 'Audio file is required'}, status=status.HTTP_400_BAD_REQUEST)
    
    # Step 1: Scan base folder với cache service
    logger.info(f"Auto-mix: Scanning base folder {base_folder_path}")
    cache_service = get_cache_service()
    
    try:
        # Scan folder tổng để lấy tất cả subfolder
        all_subfolders = []
        for entry in os.scandir(base_folder_path):
            if entry.is_dir():
                all_subfolders.append(entry.path)
        
        if len(all_subfolders) < 10:
            return Response({
                'error': f'Cần ít nhất 10 subfolder. Tìm thấy: {len(all_subfolders)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Parallel scan tất cả subfolder với cache
        logger.info(f"Auto-mix: Parallel scanning {len(all_subfolders)} subfolders with cache")
        scan_results = cache_service.scan_folders_parallel(all_subfolders)
        
        # Filter folders có đủ video
        valid_folders = []
        for folder_path, videos in scan_results.items():
            if len(videos) >= min_videos:
                valid_folders.append({
                    'path': folder_path,
                    'name': os.path.basename(folder_path),
                    'video_count': len(videos)
                })
        
        if len(valid_folders) < 10:
            return Response({
                'error': f'Cần ít nhất 10 folders có >= {min_videos} video. Tìm thấy: {len(valid_folders)}',
                'scanned_folders': valid_folders
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Sort by video count (descending) và lấy 10 folders đầu
        valid_folders.sort(key=lambda x: x['video_count'], reverse=True)
        selected_folders = valid_folders[:10]
        
        logger.info(
            f"Auto-mix: Selected 10 folders with total {sum(f['video_count'] for f in selected_folders)} videos. "
            f"Cache hit rate: {cache_service.get_cache_stats()['hit_rate']}%"
        )
        
    except Exception as e:
        logger.exception("Auto-mix: Error scanning folders")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Step 2: Create temp dir và prepare for mix
    temp_dir = tempfile.mkdtemp(prefix='mix_auto_')
    
    try:
        # Save audio file
        audio_file = request.FILES['audio']
        ext_a = Path(audio_file.name).suffix or '.mp3'
        audio_path = os.path.join(temp_dir, f"audio{ext_a}")
        with open(audio_path, 'wb') as out:
            for chunk in audio_file.chunks():
                out.write(chunk)
        
        # Create symlinks to selected folders
        for i, folder_info in enumerate(selected_folders):
            link_path = os.path.join(temp_dir, f"folder_{i}")
            try:
                os.symlink(folder_info['path'], link_path, target_is_directory=True)
            except OSError:
                # Fallback: copy tree (slower but works everywhere)
                shutil.copytree(folder_info['path'], link_path, dirs_exist_ok=True)
        
    except Exception as e:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        logger.exception("Auto-mix: Error preparing temp dir")
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Step 3: Start mix task in background
    progress_id = uuid.uuid4().hex
    with _mix_progress_lock:
        _mix_progress[progress_id] = {
            "status": "processing", 
            "percent": 0, 
            "num_outputs": None, 
            "error": None,
            "output_urls": None, 
            "output_filenames": None, 
            "output_url": None, 
            "output_filename": None,
            "selected_folders": [f['name'] for f in selected_folders],  # Info for user
            "scan_cache_hit_rate": cache_service.get_cache_stats()['hit_rate']
        }
    
    threading.Thread(
        target=_run_mix_task,
        args=(temp_dir, progress_id, width, height, num_outputs),
        daemon=True
    ).start()
    
    return Response({
        "progress_id": progress_id,
        "selected_folders": selected_folders,
        "total_videos": sum(f['video_count'] for f in selected_folders),
        "cache_stats": cache_service.get_cache_stats()
    }, status=status.HTTP_202_ACCEPTED)


@api_view(['POST'])
def mix_videos_upload(request):
    """
    Mix video với uploaded folders từ FE
    Nhận folder_0..folder_9 (videos) và audio từ multipart/form-data
    """
    try:
        audio = request.FILES.get('audio')
        if not audio:
            return Response({'error': 'Audio file is required'}, status=400)
        
        width = int(request.POST.get('width', 720))
        height = int(request.POST.get('height', 1280))
        num_outputs = int(request.POST.get('num_outputs', 5))
        
        # Group files by folder slots
        folder_slots = {}
        for key in request.FILES.keys():
            if key.startswith('folder_'):
                slot_index = int(key.replace('folder_', ''))
                folder_slots[slot_index] = request.FILES.getlist(key)
        
        if not folder_slots:
            return Response({'error': 'At least one folder with videos is required'}, status=400)
        
        # Create temp directory structure
        import tempfile
        import shutil
        from pathlib import Path
        
        temp_base = Path(tempfile.mkdtemp(prefix='mix_upload_'))
        
        try:
            # Save uploaded videos into folder structure
            folder_dirs = []
            for slot_index in range(10):
                slot_dir = temp_base / f'slot_{slot_index}'
                slot_dir.mkdir(exist_ok=True)
                
                if slot_index in folder_slots:
                    files = folder_slots[slot_index]
                    for file in files:
                        file_path = slot_dir / file.name
                        with open(file_path, 'wb+') as dest:
                            for chunk in file.chunks():
                                dest.write(chunk)
                
                folder_dirs.append(str(slot_dir))
            
            # Save audio
            audio_path = temp_base / 'audio' / audio.name
            audio_path.parent.mkdir(exist_ok=True)
            with open(audio_path, 'wb+') as dest:
                for chunk in audio.chunks():
                    dest.write(chunk)
            
            logger.info(f"[MIX-UPLOAD] Saved {sum(len(folder_slots.get(i, [])) for i in range(10))} videos to {temp_base}")
            
            # Call existing mix logic
            from threading import Thread
            progress_id = _generate_progress_id()
            
            # Initialize progress
            _mix_progress[progress_id] = {
                'status': 'processing',
                'percent': 0,
                'num_outputs': num_outputs,
                'output_urls': [],
                'output_filenames': [],
                'error': None
            }
            
            # Run mix task in background
            def run_mix():
                try:
                    result = _run_mix_task(
                        folder_dirs=folder_dirs,
                        audio_path=str(audio_path),
                        width=width,
                        height=height,
                        num_outputs=num_outputs,
                        progress_id=progress_id
                    )
                    
                    _mix_progress[progress_id].update({
                        'status': 'done',
                        'percent': 100,
                        'output_url': result.get('output_url'),
                        'output_filename': result.get('output_filename'),
                        'output_urls': result.get('output_urls', []),
                        'output_filenames': result.get('output_filenames', []),
                    })
                    
                except Exception as e:
                    logger.error(f"[MIX-UPLOAD] Error: {str(e)}")
                    _mix_progress[progress_id].update({
                        'status': 'error',
                        'error': str(e)
                    })
                
                finally:
                    # Cleanup temp directory
                    try:
                        shutil.rmtree(temp_base)
                        logger.info(f"[MIX-UPLOAD] Cleaned up temp dir: {temp_base}")
                    except Exception as e:
                        logger.warning(f"[MIX-UPLOAD] Failed to cleanup: {e}")
            
            thread = Thread(target=run_mix, daemon=True)
            thread.start()
            
            return Response({
                'progress_id': progress_id,
                'message': 'Mix process started'
            }, status=202)
            
        except Exception as e:
            # Cleanup on error
            try:
                shutil.rmtree(temp_base)
            except:
                pass
            raise e
            
    except Exception as e:
        logger.error(f"[MIX-UPLOAD] Error: {str(e)}")
        return Response({'error': str(e)}, status=500)


@api_view(['GET', 'POST'])
def video_cache_manage(request):
    """
    GET: Lấy thống kê cache
    POST: Cleanup cache
    
    POST body options:
    {
        "action": "cleanup_invalid",  // Xóa cache không valid
        "action": "cleanup_old",      // Xóa cache cũ
        "days": 30                    // Số ngày (cho cleanup_old)
    }
    """
    from video_management.services.video_cache_service import get_cache_service
    
    cache_service = get_cache_service()
    
    if request.method == 'GET':
        # Return cache stats
        stats = cache_service.get_cache_stats()
        
        from video_management.models import LocalVideoFile
        from django.db.models import Count, Min, Max
        
        # Additional stats
        folder_stats = LocalVideoFile.objects.values('folder_name').annotate(
            count=Count('id')
        ).order_by('-count')[:10]
        
        date_range = LocalVideoFile.objects.aggregate(
            oldest=Min('last_accessed_at'),
            newest=Max('last_accessed_at')
        )
        
        return Response({
            'success': True,
            'stats': stats,
            'top_folders': list(folder_stats),
            'date_range': {
                'oldest_access': date_range['oldest'].isoformat() if date_range['oldest'] else None,
                'newest_access': date_range['newest'].isoformat() if date_range['newest'] else None
            }
        })
    
    elif request.method == 'POST':
        import json as json_mod
        try:
            data = json_mod.loads(request.body)
        except Exception:
            return Response({'error': 'Invalid JSON'}, status=status.HTTP_400_BAD_REQUEST)
        
        action = data.get('action', '').strip()
        
        if action == 'cleanup_invalid':
            deleted = cache_service.cleanup_invalid_cache()
            return Response({
                'success': True,
                'action': 'cleanup_invalid',
                'deleted_count': deleted,
                'message': f'Deleted {deleted} invalid cache entries'
            })
        
        elif action == 'cleanup_old':
            days = data.get('days', 30)
            try:
                days = int(days)
            except (ValueError, TypeError):
                return Response({'error': 'days must be an integer'}, status=status.HTTP_400_BAD_REQUEST)
            
            deleted = cache_service.cleanup_old_cache(days)
            return Response({
                'success': True,
                'action': 'cleanup_old',
                'days': days,
                'deleted_count': deleted,
                'message': f'Deleted {deleted} cache entries older than {days} days'
            })
        
        elif action == 'clear_all':
            from video_management.models import LocalVideoFile
            count = LocalVideoFile.objects.count()
            LocalVideoFile.objects.all().delete()
            return Response({
                'success': True,
                'action': 'clear_all',
                'deleted_count': count,
                'message': f'Deleted all {count} cache entries'
            })
        
        else:
            return Response({
                'error': 'Invalid action. Use: cleanup_invalid, cleanup_old, or clear_all'
            }, status=status.HTTP_400_BAD_REQUEST)
