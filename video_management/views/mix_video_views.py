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
import subprocess
import tempfile
import threading
import uuid
import logging
from pathlib import Path
from datetime import datetime
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


def _possible_starts(duration: float, segment_len: int) -> List[float]:
    """Danh sách vị trí bắt đầu có thể cho đoạn segment_len giây.
    Nếu dư giây cuối, lấy thêm 1 đoạn (overlap) để tận dụng hết video."""
    if duration < segment_len:
        return [0.0]
    starts = [float(s) for s in range(0, int(duration) - segment_len + 1, segment_len)]
    # Nếu đoạn cuối cùng chưa chạm tới hết video (còn dư), thêm đoạn cuối cùng (chồng lấn)
    last_end = starts[-1] + segment_len
    if last_end < duration:
        starts.append(duration - segment_len)
    return starts


def _random_starts(duration: float, segment_len: int, count: int) -> List[float]:
    """Lấy count vị trí bắt đầu ngẫu nhiên (không trùng) cho đoạn segment_len giây. Các đoạn không chồng lấn."""
    if duration < segment_len or count <= 0:
        return [0.0] * max(1, count)
    step = segment_len
    possible = [float(s) for s in range(0, int(duration) - segment_len + 1, step)]
    if not possible:
        possible = [0.0]
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


def _concat_files(part_paths: List[str], output_path: str, step_name: Optional[str] = None) -> Tuple[bool, str]:
    """Nối bằng filter_complex để ép lại Timeline chuẩn xác nhất."""
    num_files = len(part_paths)
    inputs = []
    for p in part_paths:
        inputs.extend(["-i", p])
    
    # Tạo chuỗi filter: [0:v][0:a][1:v][1:a]...concat=n=7:v=1:a=1[v][a]
    filter_str = "".join([f"[{i}:v][{i}:a]" for i in range(num_files)])
    filter_str += f"concat=n={num_files}:v=1:a=1[v][a]"
    
    args = inputs + [
        "-filter_complex", filter_str,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-ar", "44100",
        output_path
    ]
    return _run_ffmpeg(args, step_name=step_name)


def _build_one_mix(
    input_paths: List[str],
    starts: List[float],
    output_path: str,
    width: int,
    height: int,
) -> Tuple[bool, str]:
    """
    Tạo 1 video mix từ 10 input, mỗi phần lấy đoạn 5s từ vị trí starts[i].
    starts: 10 float (giây) – start cho file 0..9.
    """
    if len(input_paths) != REQUIRED_FILES or len(starts) != REQUIRED_FILES:
        return False, f"Cần đúng {REQUIRED_FILES} file và {REQUIRED_FILES} start."

    # Khi height <= 0 (tự động) stack sẽ bị chiều cao âm → dùng height mặc định 9:16
    if height <= 0:
        height = max(360, (width * 16) // 9)
        if height % 2:
            height += 1

    dur = 5
    temp_dir = tempfile.mkdtemp(prefix="mix_ffmpeg_")
    part_files = []
    try:
        for i in range(7):
            part_files.append(os.path.join(temp_dir, f"seg_{i}.mp4"))

        ok, err = _trim_single_to_file(
            input_paths[0], part_files[0], dur, width, height, start_sec=starts[0], step_name="trim part 1"
        )
        if not ok:
            return False, f"Phần 1: {err}"

        ok, err = _trim_single_to_file(
            input_paths[1], part_files[1], dur, width, height, start_sec=starts[1], step_name="trim part 2"
        )
        if not ok:
            return False, f"Phần 2: {err}"

        ok, err = _stack_two_to_file(
            input_paths[2], input_paths[3], part_files[2], dur, width, height,
            start_top=starts[2], start_bottom=starts[3], step_name="stack part 3",
        )
        if not ok:
            return False, f"Phần 3: {err}"

        ok, err = _stack_two_to_file(
            input_paths[4], input_paths[5], part_files[3], dur, width, height,
            start_top=starts[4], start_bottom=starts[5], step_name="stack part 4",
        )
        if not ok:
            return False, f"Phần 4: {err}"

        ok, err = _stack_two_to_file(
            input_paths[6], input_paths[7], part_files[4], dur, width, height,
            start_top=starts[6], start_bottom=starts[7], step_name="stack part 5",
        )
        if not ok:
            return False, f"Phần 5: {err}"

        ok, err = _trim_single_to_file(
            input_paths[8], part_files[5], dur, width, height, start_sec=starts[8], step_name="trim part 6"
        )
        if not ok:
            return False, f"Phần 6: {err}"

        ok, err = _trim_single_to_file(
            input_paths[9], part_files[6], dur, width, height, start_sec=starts[9], step_name="trim part 7"
        )
        if not ok:
            return False, f"Phần 7: {err}"

        ok, err = _concat_files(part_files, output_path, step_name="concat")
        if not ok:
            return False, f"Concat: {err}"
        return True, ""
    finally:
        for p in part_files:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


# Lưu tiến trình mix theo progress_id (khi chạy mix trong thread)
_mix_progress: Dict[str, Any] = {}
_mix_progress_lock = threading.Lock()


def _run_mix_task(temp_dir: str, progress_id: str, width: int, height: int, num_outputs_cap: int) -> None:
    """Chạy logic mix trong thread, cập nhật _mix_progress[progress_id] theo tiến trình."""
    input_paths = []
    try:
        parts = list(Path(temp_dir).glob("part_*"))
        parts.sort(key=lambda p: int(re.search(r"part_(\d+)", p.name).group(1)))
        input_paths = [str(p) for p in parts[:REQUIRED_FILES]]
        if len(input_paths) < REQUIRED_FILES:
            with _mix_progress_lock:
                _mix_progress[progress_id].update(status="error", error=f"Thiếu file (cần {REQUIRED_FILES}).")
            return

        segment_len = 5
        # Khi ffprobe không đọc được duration (None), dùng 10s để có ít nhất 2 đoạn 5s → nhiều tổ hợp output hơn
        duration_fallback_when_unknown = 10.0
        durations = []
        for i in range(REQUIRED_FILES):
            dur = _get_duration(input_paths[i])
            if dur is None:
                dur = duration_fallback_when_unknown
            elif dur < segment_len:
                dur = segment_len
            durations.append(dur)

        num_segments = [_max_segments_per_file(durations[i], segment_len) for i in range(REQUIRED_FILES)]
        total_combinations = 1
        for n in num_segments:
            total_combinations *= max(1, n)
        max_from_duration = min(total_combinations, 100)
        num_outputs = min(max_from_duration, num_outputs_cap)
        num_outputs = max(1, num_outputs)

        with _mix_progress_lock:
            _mix_progress[progress_id]["num_outputs"] = num_outputs

        # Pool chứa tất cả các mẩu khả dụng cho mỗi file trong số 10 file
        all_pools = []
        for i in range(REQUIRED_FILES):
            pool = _possible_starts(durations[i], segment_len)
            random.shuffle(pool)
            all_pools.append(pool)
        starts_per_mix = []
        pool_counters = [0] * REQUIRED_FILES
        for k in range(num_outputs):
            current_combination = []
            for i in range(REQUIRED_FILES):
                if pool_counters[i] >= len(all_pools[i]):
                    random.shuffle(all_pools[i])
                    pool_counters[i] = 0
                current_combination.append(all_pools[i][pool_counters[i]])
                pool_counters[i] += 1
            starts_per_mix.append(current_combination)
        out_dir = Path(settings.MEDIA_ROOT) / "mix_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        output_urls = []
        output_filenames = []
        base_id = uuid.uuid4().hex[:12]

        for k in range(num_outputs):
            starts_k = starts_per_mix[k]
            # Format: mixed_YYYYMMDD_HHMMSS_<base_id>_<k>.mp4
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"mixed_{timestamp_str}_{base_id}_{k}.mp4"
            output_path = out_dir / output_filename
            ok, err = _build_one_mix(input_paths, starts_k, str(output_path), width, height)
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
                max_from_duration=max_from_duration,
                total_combinations=total_combinations,
                num_segments_per_file=num_segments,
            )
    except Exception as e:
        logger.exception("Mix task error: %s", e)
        with _mix_progress_lock:
            _mix_progress[progress_id].update(status="error", error=str(e))
    finally:
        for p in input_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


@api_view(['POST'])
def mix_videos(request):
    """
    POST: multipart/form-data với đúng 10 file video (key: videos hoặc files).
    Trả về 202 Accepted với progress_id; frontend gọi GET /api/videos/mix/status/<progress_id>/ để lấy tiến trình và kết quả.
    """
    files = []
    if request.FILES:
        for key in request.FILES:
            file_list = request.FILES.getlist(key)
            for f in file_list:
                if not f.name:
                    continue
                ext = Path(f.name).suffix.lower()
                if ext in ALLOWED_EXTENSIONS:
                    files.append(f)

    if len(files) < REQUIRED_FILES:
        return Response(
            {
                'error': f'Cần đúng {REQUIRED_FILES} file video theo thứ tự: '
                'Phần 1 (5s), Phần 2 (5s), Phần 3 trên+dưới (5s), Phần 4 trên+dưới (5s), '
                'Phần 5 trên+dưới (5s), Phần 6 (5s), Phần 7 (5s).'
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    files = files[:REQUIRED_FILES]
    width, height = _get_output_size(request)
    try:
        requested = int(request.POST.get("num_outputs", 0) or 0)
    except (TypeError, ValueError):
        requested = 0
    num_outputs_cap = max(1, min(100, requested)) if requested else 100

    temp_dir = tempfile.mkdtemp(prefix='mix_video_')
    try:
        for i, f in enumerate(files):
            ext = Path(f.name).suffix or '.mp4'
            safe_name = f"part_{i}{ext}"
            path = os.path.join(temp_dir, safe_name)
            with open(path, 'wb') as out:
                for chunk in f.chunks():
                    out.write(chunk)
    except Exception as e:
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            "max_from_duration": None,
            "total_combinations": None,
            "num_segments_per_file": None,
        }
    thread = threading.Thread(
        target=_run_mix_task,
        args=(temp_dir, progress_id, width, height, num_outputs_cap),
        daemon=True,
    )
    thread.start()
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
