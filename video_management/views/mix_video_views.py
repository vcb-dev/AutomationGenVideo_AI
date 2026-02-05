"""
API View: Mix 7 phần video thành 1 video A4 (FFmpeg).
Mỗi phần đều 5 giây.

Phần 1: video 5s
Phần 2: video 5s
Phần 3: 2 video ghép trên/dưới, tổng 5s
Phần 4: 2 video ghép trên/dưới, tổng 5s
Phần 5: 2 video ghép trên/dưới, tổng 5s
Phần 6: video 5s
Phần 7: video 5s

Cần đúng 10 file video (thứ tự: 1, 2, 3_trên, 3_dưới, 4_trên, 4_dưới, 5_trên, 5_dưới, 6, 7).
Sử dụng FFmpeg (subprocess), không dùng MoviePy.
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
    width = getattr(settings, "MIX_VIDEO_OUTPUT_WIDTH", 720) or 720
    height = getattr(settings, "MIX_VIDEO_OUTPUT_HEIGHT", 0) or 0
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
    duration_sec: int,
    width: int,
    height: int,
    start_sec: float = 0,
    step_name: Optional[str] = None,
) -> Tuple[bool, str]:
    # Thêm fps=30 để đồng bộ tốc độ khung hình
    # Thêm setsar=1 để ép pixel vuông (1:1) tránh lỗi concat do lệch SAR
    vf = f"{_scale_filter(width, height)},fps=30,setsar=1,setpts=PTS-STARTPTS"
    args = [
        "-ss", str(start_sec),
        "-t", str(duration_sec),
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-ar", "44100",
        "-ac", "2", # Ép 2 kênh audio
        "-map_metadata", "-1",
        # Xóa bỏ video_track_timescale nếu không cần thiết hoặc để mặc định
        output_path,
    ]
    return _run_ffmpeg(args, step_name=step_name)


def _stack_two_to_file(
    path_top: str,
    path_bottom: str,
    output_path: str,
    duration_sec: int,
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
        
        f"[1:v]{scale_bottom},fps=30,setsar=1,"
        f"pad={width}:{height}:0:{top_h - overlap},"
        f"setpts=PTS-STARTPTS[bot];"
        
        "[bot][top]overlay=0:0:format=auto,setpts=PTS-STARTPTS[v]"
    )
    # Thêm audio im lặng để concat không lỗi khi các đoạn single có audio
    args = [
        "-ss", str(start_top),
        "-t", str(duration_sec),
        "-i", path_top,
        "-ss", str(start_bottom),
        "-t", str(duration_sec),
        "-i", path_bottom,
        "-f", "lavfi", "-t", str(duration_sec),
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-c:a", "aac", "-ar", "44100",
        "-t", str(duration_sec),
        output_path,
    ]
    return _run_ffmpeg(args, step_name=step_name)


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
            "-c", "copy",
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
) -> Tuple[bool, str]:
    """
    Tạo 1 video mix từ 10 input.
    durations: list 7 float cho 7 phần.
    """
    if len(input_paths) != REQUIRED_FILES or len(starts) != REQUIRED_FILES:
        return False, f"Cần đúng {REQUIRED_FILES} file và {REQUIRED_FILES} start."
    if len(durations) != 7:
        return False, "Cần đúng 7 giá trị thời lượng cho 7 phần."

    if height <= 0:
        height = max(360, (width * 16) // 9)
        if height % 2:
            height += 1

    temp_dir = tempfile.mkdtemp(prefix="mix_ffmpeg_")
    part_files = []
    try:
        for i in range(7):
            part_files.append(os.path.join(temp_dir, f"seg_{i}.mp4"))

        # Part 1: Folders 0
        ok, err = _trim_single_to_file(
            input_paths[0], part_files[0], durations[0], width, height, start_sec=starts[0], step_name="trim part 1"
        )
        if not ok: return False, f"Phần 1: {err}"

        # Part 2: Folder 1
        ok, err = _trim_single_to_file(
            input_paths[1], part_files[1], durations[1], width, height, start_sec=starts[1], step_name="trim part 2"
        )
        if not ok: return False, f"Phần 2: {err}"

        # Part 3: Folders 2, 3
        ok, err = _stack_two_to_file(
            input_paths[2], input_paths[3], part_files[2], durations[2], width, height,
            start_top=starts[2], start_bottom=starts[3], step_name="stack part 3",
        )
        if not ok: return False, f"Phần 3: {err}"

        # Part 4: Folders 4, 5
        ok, err = _stack_two_to_file(
            input_paths[4], input_paths[5], part_files[3], durations[3], width, height,
            start_top=starts[4], start_bottom=starts[5], step_name="stack part 4",
        )
        if not ok: return False, f"Phần 4: {err}"

        # Part 5: Folders 6, 7
        ok, err = _stack_two_to_file(
            input_paths[6], input_paths[7], part_files[4], durations[4], width, height,
            start_top=starts[6], start_bottom=starts[7], step_name="stack part 5",
        )
        if not ok: return False, f"Phần 5: {err}"

        # Part 6: Folder 8
        ok, err = _trim_single_to_file(
            input_paths[8], part_files[5], durations[5], width, height, start_sec=starts[8], step_name="trim part 6"
        )
        if not ok: return False, f"Phần 6: {err}"

        # Part 7: Folder 9 (Outrol)
        ok, err = _trim_single_to_file(
            input_paths[9], part_files[6], durations[6], width, height, start_sec=starts[9], step_name="trim part 7"
        )
        if not ok: return False, f"Phần 7: {err}"

        ok, err = _concat_files(part_files, output_path, step_name="concat")
        if not ok: return False, f"Concat: {err}"
        return True, ""
    finally:
        for p in part_files:
            try:
                if os.path.exists(p): os.remove(p)
            except OSError: pass
        try: os.rmdir(temp_dir)
        except OSError: pass


# Lưu tiến trình mix theo progress_id (khi chạy mix trong thread)
_mix_progress: Dict[str, Any] = {}
_mix_progress_lock = threading.Lock()


def _run_mix_task(temp_dir: str, progress_id: str, width: int, height: int, num_outputs_cap: int) -> None:
    """Chạy logic mix trong thread, cập nhật _mix_progress[progress_id]."""
    try:
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
            f9_dir = os.path.join(temp_dir, "folder_9")
            f9_files = [str(p) for p in Path(f9_dir).glob("*") if p.suffix.lower() in ALLOWED_EXTENSIONS]
            if not f9_files:
                raise ValueError("Folder 9 (Outtrol) không có video hợp lệ.")
            
            for f9 in f9_files:
                d9 = _get_duration(f9) or 5.0
                seg_len = max(0.1, (audio_dur - d9) / 6.0)
                
                # Part durations for this specific Outtrol
                current_part_durs = [seg_len] * 6 + [d9]
                
                # Build pools for Folders 0-8 for this specific seg_len
                current_pools = []
                for i in range(9):
                    f_dir = os.path.join(temp_dir, f"folder_{i}")
                    files_i = [str(p) for p in Path(f_dir).glob("*") if p.suffix.lower() in ALLOWED_EXTENSIONS]
                    if not files_i:
                        raise ValueError(f"Folder {i} không có video hợp lệ.")
                    
                    folder_segments = []
                    for p_v in files_i:
                        d_v = _get_duration(p_v) or seg_len
                        starts = _possible_starts(d_v, int(seg_len))
                        for s in starts:
                            folder_segments.append((p_v, s))
                    current_pools.append(folder_segments)
                
                # Thêm Outtrol (Folder 9) là pool 1 file duy nhất
                current_pools.append([(f9, 0.0)])
                
                # Tạo tổ hợp cho Outtrol này
                # Cảnh báo: num_outputs_cap giới hạn tổng số video cuối cùng
                for combo in itertools.product(*current_pools):
                    combinations.append((combo, current_part_durs))
                    if len(combinations) >= num_outputs_cap:
                        break
                if len(combinations) >= num_outputs_cap:
                    break
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
                s_list = _random_starts(d_v, int(target_dur), 1)
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
            
            ok, err = _build_one_mix(selected_paths, starts, str(output_path), width, height, durations=part_durs)
            
            if ok and audio_path:
                final_path = str(output_path).replace(".mp4", "_final.mp4")
                ok_a, err_a = _replace_audio(str(output_path), audio_path, final_path)
                if ok_a:
                    time.sleep(0.2)
                    os.replace(final_path, output_path)
                else:
                    logger.warning("Replace audio failed: %s", err_a)

            if not ok:
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
    POST: multipart/form-data.
    Chế độ 1: 10 files (videos).
    Chế độ 2: audio + folder_0..9.
    """
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
