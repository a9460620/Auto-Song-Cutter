#!/usr/bin/env python3
"""Upload cut karaoke clips to Bilibili as multi-part submissions.

This follows the original InaWrapper upload settings:
  biliup upload ... --copyright=2 --tid=31 --tag=... --title="[歌切]..." --source=...

Default mode is dry-run. Add --execute to actually upload.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

from auth import require_bilibili_login


DEFAULT_MP4_DIR = Path(
    "/Users/liangboyao/Documents/新版歌回/Auto-Song-Cutter/Songs_Export"
)

# Edit these defaults if you want the submission text to be different.
DEFAULT_TITLE = "2026-06-01-儿童节歌切"
DEFAULT_SOURCE = "https://live.bilibili.com/23899550"
DEFAULT_DESCRIPTION = """AI歌切 用的工具：
inaSpeechSegmenter: 自動偵測音樂段落
ShazamAPI: 自動辨識歌曲（還有人工修正）
ffmpeg: 依時間切出 mp4
參考
https://github.com/ngrict/Auto-Song-Cutter
https://github.com/a9460620/Auto-Song-Cutter

歌名肯定有誤請見諒，沒誤就是人工修正發力了

【seven7酱】的主页：https://space.bilibili.com/3270232
【seven7酱】的直播：https://live.bilibili.com/23899550"""
DEFAULT_TAGS = ["歌切", "seven7酱"]
SCRIPT_DIR = Path(__file__).resolve().parent


def default_cookie_candidates() -> list[Path]:
    return [
        SCRIPT_DIR / "cookies.json",
        Path.cwd() / "cookies.json",
        Path.home() / "cookies.json",
        Path.home() / "Documents" / "歌回" / "cookies.json",
    ]


def find_cookies(explicit: Path | None = None, execute: bool = False) -> Path | None:
    candidates = [explicit.expanduser()] if explicit else default_cookie_candidates()
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    if not execute:
        return candidates[0] if candidates else None

    checked = "\n".join(f"  - {path}" for path in candidates)
    raise FileNotFoundError(
        "找不到 biliup 需要的 cookies.json。請先登入 biliup，或用 --cookies-file 指定。\n"
        f"已檢查：\n{checked}"
    )


def sort_key(path: Path) -> tuple[int, int, int, int, str]:
    """Sort by the start time embedded in names like 01_00_11_58_Title.mp4."""
    match = re.match(r"^(\d+)_([0-9]+)_([0-9]+)_([0-9]+)", path.name)
    if match:
        index, hour, minute, second = map(int, match.groups())
        return hour, minute, second, index, path.name

    match = re.match(r"^(\d+)_([0-9]+)-([0-9]+)-([0-9]+)", path.name)
    if match:
        index, hour, minute, second = map(int, match.groups())
        return hour, minute, second, index, path.name

    match = re.match(r"^(\d+)", path.name)
    if match:
        return 9999, 0, 0, int(match.group(1)), path.name

    return 9999, 0, 0, 10**9, path.name


def find_biliup(explicit: str | None = None, execute: bool = False) -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(["biliup", "biliup.exe"])

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found

    if not execute:
        return explicit or "biliup"

    raise FileNotFoundError(
        "找不到 biliup / biliup.exe。請先安裝並登入 biliup，或用 --biliup 指定完整路徑。"
    )


def chunked(items: list[Path], limit: int) -> list[list[Path]]:
    if limit <= 0:
        raise ValueError("episode_limit must be > 0")
    return [items[i : i + limit] for i in range(0, len(items), limit)]


def build_upload_command(
    biliup: str,
    files: list[Path],
    title: str,
    description: str,
    tags: list[str],
    source: str,
    suffix: str = "",
) -> list[str]:
    upload_title = f"[seven7酱-歌切]{title}{suffix}"
    cmd = [
        biliup,
        "upload",
        *[str(path) for path in files],
        "--copyright=2",
        f"--desc={description}",
        "--tid=31",
        f"--tag={','.join(tags)}",
        f"--title={upload_title[:80]}",
    ]
    if source:
        cmd.append(f"--source={source}")
    return cmd


def upload_with_retry(cmd: list[str], retries: int, execute: bool, work_dir: Path) -> None:
    print("\n" + "=" * 80)
    print("即將執行：")
    print(" ".join(repr(x) if " " in x else x for x in cmd))
    print(f"執行目錄：{work_dir}")

    if not execute:
        print("DRY-RUN：未上傳。確認無誤後加 --execute 真正執行。")
        return

    for attempt in range(1, retries + 2):
        result = subprocess.run(cmd, cwd=work_dir)
        if result.returncode == 0:
            return
        print(f"biliup 失敗，return code={result.returncode}，第 {attempt} 次")
        if attempt > retries:
            raise RuntimeError(f"biliup failed after {attempt} attempts")


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload mp4 clips as Bilibili multi-part submissions.")
    parser.add_argument("--mp4-dir", type=Path, default=DEFAULT_MP4_DIR)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS), help="Comma-separated tags")
    parser.add_argument("--episode-limit", type=int, default=180)
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--biliup", default=None, help="Path to biliup or biliup.exe")
    parser.add_argument(
        "--cookies-file",
        type=Path,
        default=None,
        help="Path to biliup cookies.json. Default checks script dir, current dir, home, and ~/Documents/歌回.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually upload. Default is dry-run.")
    args = parser.parse_args()

    _, cookies_file = require_bilibili_login(cookie_file=args.cookies_file)
    mp4_dir = args.mp4_dir.expanduser()
    if not mp4_dir.is_dir():
        raise FileNotFoundError(mp4_dir)

    files = sorted(mp4_dir.glob("*.mp4"), key=sort_key)
    if not files:
        raise FileNotFoundError(f"沒有找到 mp4：{mp4_dir}")

    biliup = find_biliup(args.biliup, execute=args.execute)
    upload_work_dir = cookies_file.parent if cookies_file else SCRIPT_DIR
    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    groups = chunked(files, args.episode_limit)

    print(f"MP4 資料夾：{mp4_dir}")
    print(f"找到 {len(files)} 個 mp4，將分成 {len(groups)} 個投稿批次。")
    print(f"使用 biliup：{biliup}")
    if cookies_file:
        print(f"使用 cookies：{cookies_file}")

    for index, group in enumerate(groups):
        suffix = "" if index == 0 else f"_{chr(ord('a') + index)}"
        print(f"\n批次 {index + 1}/{len(groups)}，P 數：{len(group)}")
        for path in group:
            print("  ", path.name)
        cmd = build_upload_command(
            biliup=biliup,
            files=group,
            title=args.title,
            description=args.description,
            tags=tags,
            source=args.source,
            suffix=suffix,
        )
        upload_with_retry(cmd, retries=args.retries, execute=args.execute, work_dir=upload_work_dir)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"錯誤：{exc}")
        raise SystemExit(1)
