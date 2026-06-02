import argparse
import json
import os
import re
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from auth import require_bilibili_login


API_BASE = "https://api.live.bilibili.com"
ANCHOR_STREAM_API = "/xlive/app-blink/v1/anchorVideo/GetSliceStream"
USER_STREAM_API = "/xlive/web-room/v1/videoService/GetUserSliceStream"
DEFAULT_REFERER = "https://live.bilibili.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DIRECT_PROGRESS_FILENAME = "direct_download_state.json"
DEFAULT_MAX_RETRIES = 3
DEFAULT_STALL_TIMEOUT = 180
FFMPEG_PROGRESS_KEYS = {
    "bitrate",
    "drop_frames",
    "dup_frames",
    "fps",
    "frame",
    "out_time",
    "out_time_ms",
    "out_time_us",
    "progress",
    "speed",
    "stream_0_0_q",
    "total_size",
}


@dataclass
class DownloadPart:
    index: int
    url: str
    input_offset: float
    start_time: float
    end_time: float
    duration: float
    path: str


def parse_part_selection(text):
    if not text:
        return None

    selected = set()
    for item in re.split(r"[,\s]+", text.strip()):
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"無效的 part 範圍：{item}")
            selected.update(range(start, end + 1))
        else:
            part = int(item)
            if part < 1:
                raise ValueError(f"無效的 part 編號：{item}")
            selected.add(part)

    return selected


def parse_webcut_url(url):
    parsed = urlparse(url)
    params = {key: values[0] for key, values in parse_qs(parsed.query).items()}

    required = ["start_time", "end_time", "live_key"]
    missing = [key for key in required if key not in params]
    if missing:
        raise ValueError(f"URL 缺少必要參數：{', '.join(missing)}")

    params["start_time"] = int(float(params["start_time"]))
    params["end_time"] = int(float(params["end_time"]))
    params["anchor_name"] = unquote(params.get("anchor_name", "bilibili_webcut"))
    return params


def parse_time_range_url(url):
    parsed = urlparse(url)
    params = {key: values[0] for key, values in parse_qs(parsed.query).items()}

    required = ["start_time", "end_time"]
    missing = [key for key in required if key not in params]
    if missing:
        raise ValueError(f"URL 缺少必要參數：{', '.join(missing)}")

    return {
        "start_time": int(float(params["start_time"])),
        "end_time": int(float(params["end_time"])),
        "anchor_name": "bilibili_record",
    }


def url_with_time_range(url, start_time, end_time):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["start_time"] = [str(int(start_time))]
    query["end_time"] = [str(int(end_time))]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def sanitize_filename(text):
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return text or "bilibili_webcut"


def ensure_tool(name):
    if not shutil.which(name):
        raise RuntimeError(f"找不到 {name}，請先安裝並確認它在 PATH 中。")


def http_get_json(url, cookie="", timeout=20):
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://live.bilibili.com/web-cut/quick-publish.html",
    }
    if cookie:
        headers["Cookie"] = cookie

    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_slice_streams(params, cookie="", role="auto"):
    query = {
        "live_key": params["live_key"],
        "start_time": params["start_time"],
        "end_time": params["end_time"],
    }
    anchor_id = params.get("anchor_id")

    endpoints = []
    if role in ("auto", "anchor"):
        endpoints.append((ANCHOR_STREAM_API, dict(query)))
    if role in ("auto", "user"):
        user_query = dict(query)
        if anchor_id:
            user_query["live_uid"] = anchor_id
        endpoints.append((USER_STREAM_API, user_query))

    errors = []
    for endpoint, endpoint_query in endpoints:
        url = f"{API_BASE}{endpoint}?{urlencode(endpoint_query)}"
        data = http_get_json(url, cookie=cookie)
        if data.get("code") == 0:
            items = data.get("data", {}).get("list", [])
            streams = [item for item in items if item.get("stream")]
            if streams:
                return streams
            errors.append(f"{endpoint}: API 成功但沒有 stream")
        else:
            errors.append(f"{endpoint}: {data.get('code')} {data.get('message')}")

    raise RuntimeError("無法取得 Bilibili stream 清單：" + "；".join(errors))


def split_streams_to_parts(streams, output_dir, chunk_seconds, extension):
    parts = []
    part_index = 1

    for item in streams:
        stream_url = item["stream"]
        start_time = float(item.get("start_time", 0))
        end_time = float(item.get("end_time", start_time))
        duration = max(end_time - start_time, 0)

        if duration <= 0:
            duration = float(chunk_seconds)

        offset = 0.0
        while offset < duration:
            part_duration = min(chunk_seconds, duration - offset)
            part_start = start_time + offset
            part_end = part_start + part_duration
            part_path = os.path.join(output_dir, "parts", f"part_{part_index:04d}.{extension}")
            parts.append(DownloadPart(
                index=part_index,
                url=stream_url,
                input_offset=offset,
                start_time=part_start,
                end_time=part_end,
                duration=part_duration,
                path=part_path,
            ))
            offset += part_duration
            part_index += 1

    return parts


def filter_parts(parts, selected_parts):
    if selected_parts is None:
        return parts
    return [part for part in parts if part.index in selected_parts]


def build_webcut_parts(params, output_dir, chunk_seconds, extension, cookie="", role="auto", selected_parts=None):
    parts = []
    current_start = int(params["start_time"])
    final_end = int(params["end_time"])
    part_index = 1

    while current_start < final_end:
        current_end = min(current_start + chunk_seconds, final_end)
        if selected_parts is not None and part_index not in selected_parts:
            current_start = current_end
            part_index += 1
            continue

        part_params = dict(params)
        part_params["start_time"] = current_start
        part_params["end_time"] = current_end
        streams = get_slice_streams(part_params, cookie=cookie, role=role)
        item = streams[0]
        stream_url = item["stream"]
        stream_start = float(item.get("start_time", current_start))
        stream_end = float(item.get("end_time", current_end))
        duration = max(stream_end - stream_start, current_end - current_start)
        part_path = os.path.join(output_dir, "parts", f"part_{part_index:04d}.{extension}")
        parts.append(DownloadPart(
            index=part_index,
            url=stream_url,
            input_offset=0.0,
            start_time=stream_start,
            end_time=stream_start + duration,
            duration=duration,
            path=part_path,
        ))

        part_index += 1
        current_start = current_end

    return parts


def build_ffmpeg_headers(cookie=""):
    headers = [
        f"User-Agent: {DEFAULT_USER_AGENT}",
        f"Referer: {DEFAULT_REFERER}",
        "Origin: https://live.bilibili.com",
    ]
    if cookie:
        headers.append(f"Cookie: {cookie}")
    return "\r\n".join(headers) + "\r\n"


def build_direct_ffmpeg_headers(cookie=""):
    headers = [
        f"Referer: {DEFAULT_REFERER}",
        "Origin: https://live.bilibili.com",
    ]
    if cookie:
        headers.append(f"Cookie: {cookie}")
    return "\r\n".join(headers) + "\r\n"


def parse_progress_line(line):
    if line.startswith(("out_time_ms=", "out_time_us=")):
        value = line.split("=", 1)[1]
        try:
            return max(float(value) / 1_000_000, 0)
        except ValueError:
            return None
    if not line.startswith("out_time="):
        return None
    value = line.split("=", 1)[1]
    match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", value)
    if not match:
        return None
    try:
        hours = float(match.group(1))
        minutes = float(match.group(2))
        seconds = float(match.group(3))
        return max(hours * 3600 + minutes * 60 + seconds, 0)
    except ValueError:
        return None


def is_ffmpeg_progress_status(line):
    key = line.split("=", 1)[0]
    return key in FFMPEG_PROGRESS_KEYS


def get_file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def terminate_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def format_download_error(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return f"FFmpeg 失敗，exit code={exc.returncode}"
    return str(exc)


def watch_ffmpeg_process(
    process,
    temp_path,
    duration=0,
    stall_timeout=DEFAULT_STALL_TIMEOUT,
    normalize_timestamps=False,
):
    last_seconds = 0.0
    progress_base = None
    last_size = get_file_size(temp_path)
    last_size_change = time.monotonic()
    error_lines = []
    stalled = False

    assert process.stdout is not None
    while process.poll() is None:
        readable, _, _ = select.select([process.stdout], [], [], 1)
        if readable:
            line = process.stdout.readline()
            if line:
                line = line.strip()
                progress_seconds = parse_progress_line(line)
                if progress_seconds is None:
                    if line and not is_ffmpeg_progress_status(line):
                        error_lines.append(line)
                        print(f"\n{line}")
                else:
                    if normalize_timestamps and progress_base is None:
                        progress_base = progress_seconds
                    display_seconds = progress_seconds
                    if progress_base is not None:
                        display_seconds = max(progress_seconds - progress_base, 0)
                    last_seconds = min(display_seconds, duration) if duration else display_seconds
                    percent = min(last_seconds / duration * 100, 100) if duration else 0
                    print(
                        f"\r進度 {percent:6.2f}%  目前 {format_duration(last_seconds)} / {format_duration(duration)}",
                        end="",
                        flush=True,
                    )

        current_size = get_file_size(temp_path)
        if current_size != last_size:
            last_size = current_size
            last_size_change = time.monotonic()
        elif time.monotonic() - last_size_change >= stall_timeout:
            stalled = True
            terminate_process(process)
            break

    for line in process.stdout:
        line = line.strip()
        progress_seconds = parse_progress_line(line)
        if progress_seconds is not None:
            if normalize_timestamps and progress_base is None:
                progress_base = progress_seconds
            display_seconds = progress_seconds
            if progress_base is not None:
                display_seconds = max(progress_seconds - progress_base, 0)
            last_seconds = min(display_seconds, duration) if duration else display_seconds
        elif line and not is_ffmpeg_progress_status(line):
            error_lines.append(line)

    return_code = process.wait()
    return return_code, last_seconds, error_lines, stalled


def run_ffmpeg_direct_once(url, output_path, duration, cookie="", overwrite=False, stall_timeout=DEFAULT_STALL_TIMEOUT):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    root, ext = os.path.splitext(output_path)
    temp_path = f"{root}.download{ext or '.mp4'}"

    if os.path.exists(output_path) and not overwrite:
        return "exists", duration

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-user_agent", DEFAULT_USER_AGENT,
        "-headers", build_direct_ffmpeg_headers(cookie),
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", url,
        "-c", "copy",
        temp_path,
    ]
    if overwrite and os.path.exists(temp_path):
        os.remove(temp_path)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    return_code, last_seconds, error_lines, stalled = watch_ffmpeg_process(
        process,
        temp_path,
        duration,
        stall_timeout=stall_timeout,
    )
    print()

    if return_code == 0:
        os.replace(temp_path, output_path)
        return "done", duration

    if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0 and last_seconds > 0:
        os.replace(temp_path, output_path)
        return "partial", last_seconds

    if os.path.exists(temp_path):
        os.remove(temp_path)
    if stalled:
        raise RuntimeError(f"下載超過 {format_duration(stall_timeout)} 檔案大小沒有變動")
    if error_lines:
        print("\n".join(error_lines[-5:]))
    raise subprocess.CalledProcessError(return_code, cmd)


def run_ffmpeg_direct(
    url,
    output_path,
    duration,
    cookie="",
    overwrite=False,
    max_retries=DEFAULT_MAX_RETRIES,
    stall_timeout=DEFAULT_STALL_TIMEOUT,
):
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"重試 direct part：第 {attempt}/{max_retries} 次")
            return run_ffmpeg_direct_once(
                url,
                output_path,
                duration,
                cookie=cookie,
                overwrite=overwrite,
                stall_timeout=stall_timeout,
            )
        except Exception as exc:
            if attempt >= max_retries:
                raise
            print(f"本次下載失敗：{format_download_error(exc)}")
            time.sleep(2)


def format_duration(seconds):
    seconds = max(int(seconds), 0)
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def run_direct_resume(
    url,
    output_dir,
    output_path,
    cookie="",
    overwrite=False,
    no_merge=False,
    max_retries=DEFAULT_MAX_RETRIES,
    stall_timeout=DEFAULT_STALL_TIMEOUT,
):
    params = parse_time_range_url(url)
    total_duration = params["end_time"] - params["start_time"]
    if total_duration <= 0:
        raise ValueError("end_time 必須大於 start_time")

    if os.path.exists(output_path) and not overwrite:
        print(f"最終檔已存在，跳過下載：{output_path}")
        return 0

    os.makedirs(output_dir, exist_ok=True)
    parts_dir = os.path.join(output_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    state_path = os.path.join(output_dir, DIRECT_PROGRESS_FILENAME)
    state = read_state(state_path)

    completed_seconds = 0.0 if overwrite else float(state.get("completed_seconds", 0))
    part_index = 1 if overwrite else int(state.get("next_part_index", 1))
    part_paths = [] if overwrite else list(state.get("part_paths", []))

    while completed_seconds < total_duration:
        current_start = params["start_time"] + int(completed_seconds)
        current_url = url_with_time_range(url, current_start, params["end_time"])
        remaining = params["end_time"] - current_start
        part_path = os.path.join(parts_dir, f"direct_part_{part_index:04d}.mp4")

        print(f"開始下載 part {part_index}: start_time={current_start}, remaining={format_duration(remaining)}")
        result, downloaded_seconds = run_ffmpeg_direct(
            current_url,
            part_path,
            duration=remaining,
            cookie=cookie,
            overwrite=True,
            max_retries=max_retries,
            stall_timeout=stall_timeout,
        )

        part_paths.append(part_path)
        completed_seconds += downloaded_seconds
        completed_seconds = min(completed_seconds, total_duration)
        part_index += 1

        write_state(state_path, {
            "source_url": url,
            "output": output_path,
            "completed_seconds": completed_seconds,
            "total_duration": total_duration,
            "next_part_index": part_index,
            "part_paths": part_paths,
            "last_result": result,
            "updated_at": int(time.time()),
        })

        if result == "partial":
            print(f"下載中斷，已記錄完成秒數：{completed_seconds:.3f}s，繼續下載剩餘片段。")
            continue

    if no_merge:
        print("分段下載完成，已依 --no-merge 跳過合併。")
    else:
        concat_direct_parts(part_paths, output_path)
        print(f"合併完成：{output_path}")
    return 0


def concat_direct_parts(part_paths, output_path):
    existing_parts = [path for path in part_paths if os.path.exists(path)]
    if not existing_parts:
        raise RuntimeError("沒有可合併的分段檔案。")

    if len(existing_parts) == 1:
        transcode_for_compatibility(existing_parts[0], output_path)
        cleanup_files(existing_parts)
        return

    concat_path = os.path.join(os.path.dirname(output_path) or ".", "direct_concat_list.txt")
    merged_path = f"{os.path.splitext(output_path)[0]}.merged.mp4"
    with open(concat_path, "w", encoding="utf-8") as f:
        for path in existing_parts:
            safe_path = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")

    subprocess.run([
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_path,
        "-c", "copy",
        "-loglevel", "error",
        merged_path,
    ], check=True)
    transcode_for_compatibility(merged_path, output_path)
    safe_remove(merged_path)
    cleanup_files(existing_parts)
    safe_remove(concat_path)


def run_ffmpeg_download_once(part, cookie, overwrite=False, stall_timeout=DEFAULT_STALL_TIMEOUT):
    os.makedirs(os.path.dirname(part.path), exist_ok=True)
    root, ext = os.path.splitext(part.path)
    temp_path = f"{root}.download{ext or '.mp4'}"

    if os.path.exists(part.path) and not overwrite:
        return "exists"

    if os.path.exists(temp_path):
        os.remove(temp_path)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress", "pipe:1",
        "-ss", f"{part.input_offset:.3f}",
        "-headers", build_ffmpeg_headers(cookie),
        "-i", part.url,
        "-t", f"{part.duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-loglevel", "error",
        temp_path,
    ]
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return_code, _, error_lines, stalled = watch_ffmpeg_process(
        process,
        temp_path,
        part.duration,
        stall_timeout=stall_timeout,
        normalize_timestamps=True,
    )
    print()

    if return_code != 0:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if stalled:
            raise RuntimeError(f"下載超過 {format_duration(stall_timeout)} 檔案大小沒有變動")
        if error_lines:
            print("\n".join(error_lines[-5:]))
        raise subprocess.CalledProcessError(return_code, cmd)

    os.replace(temp_path, part.path)
    return "done"


def run_ffmpeg_download(
    part,
    cookie,
    overwrite=False,
    max_retries=DEFAULT_MAX_RETRIES,
    stall_timeout=DEFAULT_STALL_TIMEOUT,
):
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                print(f"[{part.index}] 重試：第 {attempt}/{max_retries} 次")
            return run_ffmpeg_download_once(
                part,
                cookie,
                overwrite=overwrite,
                stall_timeout=stall_timeout,
            )
        except Exception as exc:
            if attempt >= max_retries:
                raise RuntimeError(f"part {part.index} 連續失敗 {max_retries} 次，停止下載") from exc
            print(f"[{part.index}] 本次下載失敗：{format_download_error(exc)}")
            time.sleep(2)


def read_state(state_path):
    if not os.path.exists(state_path):
        return {"completed_parts": [], "completed_seconds": 0, "part_metadata": {}}
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_state(state_path, state):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def cleanup_files(paths):
    for path in paths:
        safe_remove(path)


def ffprobe_json(path):
    data = subprocess.check_output([
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        path,
    ], text=True)
    return json.loads(data)


def stream_value(stream, key, default="-"):
    value = stream.get(key)
    if value in (None, ""):
        return default
    return str(value)


def first_stream(streams, codec_type):
    for stream in streams:
        if stream.get("codec_type") == codec_type:
            return stream
    return {}


def get_part_metadata(path):
    info = ffprobe_json(path)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    video = first_stream(streams, "video")
    audio = first_stream(streams, "audio")
    return {
        "path": path,
        "size_bytes": os.path.getsize(path),
        "duration": float(fmt.get("duration") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pix_fmt": video.get("pix_fmt"),
            "avg_frame_rate": video.get("avg_frame_rate"),
        },
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
        },
    }


def resolution_from_metadata(metadata):
    video = metadata.get("video", {})
    width = video.get("width")
    height = video.get("height")
    if not width or not height:
        return None
    return int(width), int(height)


def choose_target_resolution(part_metadata):
    counts = {}
    first_seen = {}
    for index, metadata in part_metadata.items():
        resolution = resolution_from_metadata(metadata)
        if not resolution:
            continue
        counts[resolution] = counts.get(resolution, 0) + 1
        first_seen.setdefault(resolution, int(index))

    if not counts:
        return None

    return sorted(
        counts,
        key=lambda item: (-counts[item], first_seen[item], -item[0] * item[1]),
    )[0]


def normalize_part_size(part, target_resolution):
    target_width, target_height = target_resolution
    root, ext = os.path.splitext(part.path)
    normalized_path = f"{root}.normalized{ext or '.mp4'}"
    temp_path = f"{normalized_path}.download.mp4"
    scale_filter = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1"
    )

    print(f"轉換尺寸 part {part.index:04d} -> {target_width}x{target_height}（等比縮放，不拉伸）")
    subprocess.run([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", part.path,
        "-vf", scale_filter,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        temp_path,
    ], check=True)
    os.replace(temp_path, normalized_path)
    return normalized_path


def prepare_parts_for_concat(parts):
    existing_parts = [part for part in parts if os.path.exists(part.path)]
    metadata_by_index = {
        str(part.index): get_part_metadata(part.path)
        for part in existing_parts
    }
    target_resolution = choose_target_resolution(metadata_by_index)
    if not target_resolution:
        return existing_parts, [], metadata_by_index

    target_text = f"{target_resolution[0]}x{target_resolution[1]}"
    prepared = []
    temporary_paths = []
    mismatched = []
    for part in existing_parts:
        metadata = metadata_by_index[str(part.index)]
        resolution = resolution_from_metadata(metadata)
        if resolution == target_resolution:
            prepared.append(part)
            continue

        resolution_text = "unknown" if not resolution else f"{resolution[0]}x{resolution[1]}"
        mismatched.append(f"part {part.index:04d}: {resolution_text} -> {target_text}")
        normalized_path = normalize_part_size(part, target_resolution)
        temporary_paths.append(normalized_path)
        prepared.append(DownloadPart(
            index=part.index,
            url=part.url,
            input_offset=part.input_offset,
            start_time=part.start_time,
            end_time=part.end_time,
            duration=part.duration,
            path=normalized_path,
        ))

    if mismatched:
        print("偵測到尺寸不一致，僅轉換以下分段：")
        for line in mismatched:
            print(f"  - {line}")
    else:
        print(f"所有分段尺寸一致：{target_text}")

    return prepared, temporary_paths, metadata_by_index


def make_concat_ts(path, ts_path):
    subprocess.run([
        "ffmpeg",
        "-y",
        "-i", path,
        "-c", "copy",
        "-bsf:v", "h264_mp4toannexb",
        "-f", "mpegts",
        "-loglevel", "error",
        ts_path,
    ], check=True)


def decode_check(path, seconds):
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v", "error",
        "-err_detect", "ignore_err",
        "-i", path,
    ]
    if seconds and seconds > 0:
        cmd.extend(["-t", str(seconds)])
    cmd.extend(["-map", "0:v:0", "-an", "-f", "null", "-"])
    process = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines = [line.strip() for line in process.stderr.splitlines() if line.strip()]
    return process.returncode, lines


def compare_parts(parts, check_seconds=30):
    existing_parts = [part for part in parts if os.path.exists(part.path)]
    if not existing_parts:
        print("沒有可比較的分段檔案。")
        return

    print("分段比較：")
    for part in existing_parts:
        info = ffprobe_json(part.path)
        streams = info.get("streams", [])
        fmt = info.get("format", {})
        video = first_stream(streams, "video")
        audio = first_stream(streams, "audio")
        size_mb = os.path.getsize(part.path) / 1024 / 1024
        duration = float(fmt.get("duration") or part.duration or 0)
        bitrate = int(fmt.get("bit_rate") or 0) // 1000
        fps = stream_value(video, "avg_frame_rate")
        resolution = "-"
        if video.get("width") and video.get("height"):
            resolution = f"{video['width']}x{video['height']}"

        return_code, errors = decode_check(part.path, check_seconds)
        status = "OK" if return_code == 0 and not errors else "CHECK"
        print(
            f"[{status}] part {part.index:04d}  "
            f"size={size_mb:.1f}MB  duration={format_duration(duration)}  "
            f"bitrate={bitrate}kbps  video={stream_value(video, 'codec_name')} "
            f"{resolution} fps={fps}  audio={stream_value(audio, 'codec_name')}"
        )
        if errors:
            scope = "完整" if check_seconds == 0 else f"前 {check_seconds}s"
            print(f"  {scope}解碼訊息：")
            for line in errors[:8]:
                print(f"  - {line}")
        elif return_code != 0:
            print(f"  ffmpeg exit code: {return_code}")


def transcode_for_compatibility(input_path, output_path):
    temp_output = f"{os.path.splitext(output_path)[0]}.transcoded.mp4"
    if os.path.abspath(input_path) == os.path.abspath(temp_output):
        temp_output = f"{os.path.splitext(output_path)[0]}.transcoded.final.mp4"

    subprocess.run([
        "ffmpeg",
        "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", input_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-c:a", "aac",
        "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "4096",
        "-loglevel", "error",
        temp_output,
    ], check=True)
    os.replace(temp_output, output_path)


def concat_parts(parts, output_path, cleanup_original_parts=True):
    existing_parts = [part for part in parts if os.path.exists(part.path)]
    if not existing_parts:
        raise RuntimeError("沒有可合併的分段檔案。")

    prepared_parts, temporary_paths, _ = prepare_parts_for_concat(existing_parts)

    if len(existing_parts) == 1:
        source_path = prepared_parts[0].path
        subprocess.run([
            "ffmpeg",
            "-y",
            "-i", source_path,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            "-loglevel", "error",
            output_path,
        ], check=True)
        if cleanup_original_parts:
            cleanup_files([part.path for part in existing_parts])
        cleanup_files(temporary_paths)
        return

    ts_paths = []
    for part in prepared_parts:
        ts_path = f"{os.path.splitext(part.path)[0]}.concat.ts"
        make_concat_ts(part.path, ts_path)
        ts_paths.append(ts_path)

    concat_input = "concat:" + "|".join(ts_paths)
    subprocess.run([
        "ffmpeg",
        "-y",
        "-i", concat_input,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        "-loglevel", "error",
        output_path,
    ], check=True)
    if cleanup_original_parts:
        cleanup_files([part.path for part in existing_parts])
    cleanup_files(temporary_paths)
    cleanup_files(ts_paths)


def make_default_output_name(params):
    anchor = sanitize_filename(params.get("anchor_name", "bilibili_webcut"))
    start = time.strftime("%Y%m%d_%H%M%S", time.localtime(params["start_time"]))
    end = time.strftime("%H%M%S", time.localtime(params["end_time"]))
    return f"{anchor}_{start}-{end}.mp4"


def is_webcut_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return "live_key" in query and "quick-publish" in parsed.path


def main():
    parser = argparse.ArgumentParser(description="使用 FFmpeg 下載 Bilibili 直播回放直鏈，支援中斷後續跑與合併")
    parser.add_argument("url", help="Bilibili hls-record-gateway 直鏈，或 web-cut quick-publish URL")
    parser.add_argument("--output-dir", default="Bilibili_Webcut_Downloads", help="下載工作資料夾")
    parser.add_argument("--output", help="最終輸出檔名，預設依主播與時間產生")
    parser.add_argument("--cookie", help="Bilibili Cookie 字串，例如 'SESSDATA=...; bili_jct=...'")
    parser.add_argument("--cookie-file", help="Cookie 檔，可為 Netscape cookies.txt 或純 Cookie 字串")
    parser.add_argument("--role", choices=["auto", "anchor", "user"], default="auto", help="stream API 身分路徑")
    parser.add_argument("--media-url", help="手動指定 m3u8/媒體 URL；指定後不呼叫 Bilibili stream API")
    parser.add_argument("--chunk-seconds", type=int, default=600, help="每段下載秒數，預設 600")
    parser.add_argument("--only-parts", help="只下載指定分段，例如 '1,2,3' 或 '1-3'；指定後不合併")
    parser.add_argument("--merge-selected", action="store_true", help="搭配 --only-parts 使用，只合併指定分段並保留原始 parts")
    parser.add_argument("--compare-parts", action="store_true", help="下載後比較分段 metadata，並檢查前段解碼訊息")
    parser.add_argument("--check-seconds", type=int, default=30, help="比較分段時解碼檢查前幾秒，預設 30；0 代表完整檢查")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="每個分段最多重試次數，預設 3")
    parser.add_argument("--stall-timeout", type=int, default=DEFAULT_STALL_TIMEOUT, help="檔案大小沒有變動幾秒後判定卡住，預設 180")
    parser.add_argument("--extension", default="mp4", help="分段檔副檔名，預設 mp4")
    parser.add_argument("--overwrite", action="store_true", help="重新下載已完成分段")
    parser.add_argument("--no-merge", action="store_true", help="只下載分段，不合併")
    args = parser.parse_args()

    try:
        if args.max_retries < 1:
            raise ValueError("--max-retries 必須大於等於 1")
        if args.stall_timeout < 1:
            raise ValueError("--stall-timeout 必須大於等於 1")
        if args.check_seconds < 0:
            raise ValueError("--check-seconds 必須大於等於 0")

        selected_parts = parse_part_selection(args.only_parts)
        if args.merge_selected and selected_parts is None:
            raise ValueError("--merge-selected 必須搭配 --only-parts 使用")

        ensure_tool("ffmpeg")
        ensure_tool("ffprobe")
        cookie, cookie_file = require_bilibili_login(args.cookie, args.cookie_file)
        if cookie_file:
            print(f"使用 Bilibili cookies：{cookie_file}")

        os.makedirs(args.output_dir, exist_ok=True)
        state_path = os.path.join(args.output_dir, "download_state.json")

        if not is_webcut_url(args.url) and not args.media_url:
            if selected_parts is not None:
                raise ValueError("--only-parts 只支援 web-cut quick-publish URL 或 --media-url 模式")
            params = parse_time_range_url(args.url)
            output_path = args.output or os.path.join(args.output_dir, make_default_output_name(params))
            return run_direct_resume(
                args.url,
                output_dir=args.output_dir,
                output_path=output_path,
                cookie=cookie,
                overwrite=args.overwrite,
                no_merge=args.no_merge,
                max_retries=args.max_retries,
                stall_timeout=args.stall_timeout,
            )

        params = parse_webcut_url(args.url)
        output_path = args.output or os.path.join(args.output_dir, make_default_output_name(params))

        if args.media_url:
            streams = [{
                "stream": args.media_url,
                "start_time": params["start_time"],
                "end_time": params["end_time"],
            }]
            parts = split_streams_to_parts(streams, args.output_dir, args.chunk_seconds, args.extension)
            parts = filter_parts(parts, selected_parts)
        else:
            parts = build_webcut_parts(
                params,
                args.output_dir,
                args.chunk_seconds,
                args.extension,
                cookie=cookie,
                role=args.role,
                selected_parts=selected_parts,
            )

        if selected_parts is not None and not parts:
            raise ValueError(f"找不到指定分段：{args.only_parts}")

        state = read_state(state_path)
        completed = set(state.get("completed_parts", []))
        completed_seconds = float(state.get("completed_seconds", 0))
        part_metadata = dict(state.get("part_metadata", {}))

        print(f"Total parts: {len(parts)}")
        print(f"Resume completed seconds: {completed_seconds:.3f}")
        print(f"Output: {output_path}")

        for part in parts:
            part_key = str(part.index)
            if part_key in completed and os.path.exists(part.path) and not args.overwrite:
                print(f"[{part.index}/{len(parts)}] skip completed: {part.path}")
                if part_key not in part_metadata:
                    part_metadata[part_key] = get_part_metadata(part.path)
                    state["part_metadata"] = part_metadata
                    write_state(state_path, state)
                continue

            print(f"[{part.index}/{len(parts)}] download {part.duration:.1f}s -> {part.path}")
            result = run_ffmpeg_download(
                part,
                cookie=cookie,
                overwrite=args.overwrite,
                max_retries=args.max_retries,
                stall_timeout=args.stall_timeout,
            )
            if result in ("done", "exists"):
                completed.add(part_key)
                part_metadata[part_key] = get_part_metadata(part.path)
                completed_seconds = sum(p.duration for p in parts if str(p.index) in completed)
                state = {
                    "source_url": args.url,
                    "output": output_path,
                    "completed_parts": sorted(completed, key=lambda x: int(x)),
                    "completed_seconds": completed_seconds,
                    "part_metadata": part_metadata,
                    "updated_at": int(time.time()),
                }
                write_state(state_path, state)

        missing = [part for part in parts if str(part.index) not in completed or not os.path.exists(part.path)]
        if missing:
            print(f"尚有 {len(missing)} 個分段未完成，已記錄進度：{completed_seconds:.3f}s")
            return 1

        if args.compare_parts or selected_parts is not None:
            compare_parts(parts, check_seconds=args.check_seconds)

        if selected_parts is not None and not args.merge_selected:
            print("已依 --only-parts 只下載指定分段，跳過合併。")
            return 0

        if not args.no_merge:
            concat_parts(parts, output_path, cleanup_original_parts=selected_parts is None)
            print(f"合併完成：{output_path}")
        else:
            print("分段下載完成，已依 --no-merge 跳過合併。")

        return 0
    except Exception as exc:
        print(f"錯誤：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
