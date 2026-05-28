import argparse
import os
import shutil
import subprocess
from dataclasses import dataclass


DEFAULT_LOG_PATH = os.path.join("Songs_Export", "segments_log.txt")


@dataclass
class Segment:
    filename: str
    start_time: str
    end_time: str
    duration_time: str | None = None


def timestamp_to_seconds(timestamp):
    parts = timestamp.split(":")
    if len(parts) != 3:
        raise ValueError(f"時間格式錯誤：{timestamp}")

    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_timestamp(seconds):
    if seconds < 0:
        seconds = 0
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{sec:06.3f}"


def get_effective_end_time(segment):
    start_seconds = timestamp_to_seconds(segment.start_time)
    end_seconds = timestamp_to_seconds(segment.end_time)

    if end_seconds > start_seconds:
        return segment.end_time, False

    if segment.duration_time:
        duration_seconds = timestamp_to_seconds(segment.duration_time)
        if duration_seconds > 0:
            return seconds_to_timestamp(start_seconds + duration_seconds), True

    raise ValueError(f"{segment.filename} 的 End Time 早於 Start Time，且沒有可用 Duration")


def parse_log(log_path):
    source_video = None
    segments = []

    with open(log_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("Source Video:"):
                source_video = line.split(":", 1)[1].strip()
                continue

            if "|" not in line:
                continue

            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 3 or parts[0] == "Filename":
                continue

            filename, start_time, end_time = parts[:3]
            duration_time = parts[3] if len(parts) >= 4 and parts[3] else None
            if not filename or not start_time or not end_time:
                continue

            segments.append(Segment(
                filename=filename,
                start_time=start_time,
                end_time=end_time,
                duration_time=duration_time,
            ))

    if not source_video:
        raise ValueError(f"找不到 Source Video 欄位：{log_path}")

    if not segments:
        raise ValueError(f"找不到任何切片資料：{log_path}")

    return source_video, segments


def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("找不到 ffmpeg，請先安裝並確認 ffmpeg 在 PATH 中。")


def recut_segment(source_video, output_dir, segment, overwrite):
    out_path = os.path.join(output_dir, segment.filename)
    end_time, repaired = get_effective_end_time(segment)

    if os.path.exists(out_path) and not overwrite:
        print(f"Skip exists: {out_path}")
        return "skipped"

    if repaired:
        print(f"  修正 End Time: {segment.end_time} -> {end_time}")

    cmd = [
        "ffmpeg",
        "-y" if overwrite else "-n",
        "-ss",
        segment.start_time,
        "-to",
        end_time,
        "-i",
        source_video,
        "-c",
        "copy",
        "-avoid_negative_ts",
        "1",
        "-loglevel",
        "error",
        out_path,
    ]

    subprocess.run(cmd, check=True)
    return "done"


def copy_log(log_path, output_dir):
    copied_log_path = os.path.join(output_dir, os.path.basename(log_path))
    shutil.copy2(log_path, copied_log_path)
    return copied_log_path


def main():
    parser = argparse.ArgumentParser(description="依 segments_log.txt 重新切割歌曲片段")
    parser.add_argument("--log", default=DEFAULT_LOG_PATH, help=f"segments_log.txt 路徑，預設：{DEFAULT_LOG_PATH}")
    parser.add_argument("--output", default="Songs_Recut", help="重新切割輸出資料夾，預設：Songs_Recut")
    parser.add_argument("--source", help="手動指定來源影片，會覆蓋 log 內的 Source Video")
    parser.add_argument("--overwrite", action="store_true", help="覆蓋已存在的輸出檔案")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會切割的內容，不實際執行 ffmpeg")
    args = parser.parse_args()

    if not os.path.exists(args.log):
        print(f"錯誤：找不到 log 檔案：{args.log}")
        return 1

    try:
        source_video, segments = parse_log(args.log)
        if args.source:
            source_video = args.source

        print(f"Log: {args.log}")
        print(f"Source: {source_video}")
        print(f"Output: {args.output}")
        print(f"Segments: {len(segments)}")

        source_exists = os.path.exists(source_video)
        if not source_exists:
            print(f"警告：找不到來源影片：{source_video}")

        if args.dry_run:
            for segment in segments:
                try:
                    end_time, repaired = get_effective_end_time(segment)
                    note = f" (修正 End Time: {segment.end_time} -> {end_time})" if repaired else ""
                    print(f"{segment.filename}: {segment.start_time} -> {end_time}{note}")
                except ValueError as exc:
                    print(f"跳過：{exc}")
            return 0

        if not source_exists:
            return 1

        ensure_ffmpeg()
        os.makedirs(args.output, exist_ok=True)

        done = 0
        skipped = 0
        failed = 0
        for index, segment in enumerate(segments, start=1):
            print(f"[{index}/{len(segments)}] {segment.filename}: {segment.start_time} -> {segment.end_time}")
            try:
                result = recut_segment(source_video, args.output, segment, args.overwrite)
            except (ValueError, subprocess.CalledProcessError) as exc:
                failed += 1
                print(f"  跳過：{exc}")
                continue

            if result == "done":
                done += 1
            else:
                skipped += 1

        copied_log_path = copy_log(args.log, args.output)

        print(f"\n完成：重新切割 {done} 個檔案，跳過 {skipped} 個已存在檔案，失敗 {failed} 個。")
        print(f"Log 已複製到：{copied_log_path}")
        return 1 if failed else 0
    except (RuntimeError, ValueError) as exc:
        print(f"錯誤：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
