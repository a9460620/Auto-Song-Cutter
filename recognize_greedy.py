import asyncio
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from shazamio import Shazam

# ================= 智能配置区 =================
# 脚本会自动按顺序查找这些文件夹，找到第一个存在的就开工
# 你也可以把你的文件夹名字加到这个列表里
POSSIBLE_FOLDERS = [
    "Songs_Export",       # 默认切片输出
    "hires",              # 转码后的输出
    "Bilibili_Ready",     # 修复后的输出
    "Bilibili_Upload",    # MKV输出
    "Songs_Final_V6"      # 旧配置
]

SAMPLE_RATIOS = [0.00, 0.15, 0.25, 0.50, 0.65, 0.85]
SAMPLE_DURATION = 15
RECOGNIZE_TIMEOUT = 60
MIN_VOTES = 2
REPORT_FILENAME = "recognition_candidates.txt"
UNCHECKED_FILENAME_PATTERN = re.compile(r"^(\d{2}_\d{2}_\d{2}_\d{2}|Song_\d{2})$")
# ============================================

@dataclass
class RecognitionSample:
    ratio: float
    offset: float
    title: str | None = None
    artist: str | None = None
    error: str | None = None

def sanitize_filename_part(text):
    """移除不适合放在文件名里的字符"""
    return "".join([c for c in text if c not in r'/:*?"<>|']).strip()

def normalize_title(title):
    """用于投票的歌名标准化，避免大小写与常见尾巴造成误分组"""
    title = title.lower()
    title = re.sub(r"\b(live|official|cover|mv|audio|伴奏|翻唱)\b", "", title)
    title = re.sub(r"[\s\W_]+", "", title, flags=re.UNICODE)
    return title

def is_unrecognized_file(filename):
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in [".mp4", ".mkv"]:
        return False
    if filename.startswith("temp_"):
        return False
    return bool(UNCHECKED_FILENAME_PATTERN.match(stem))

def file_sort_key(filename):
    stem, _ = os.path.splitext(filename)
    match = re.match(r"^(?:Song_)?(\d{2})", stem)
    if match:
        return int(match.group(1)), filename
    return 9999, filename

def build_recognized_name(filename, title):
    stem, ext = os.path.splitext(filename)
    safe_title = sanitize_filename_part(title) or "Unknown"
    return f"{stem}_{safe_title}{ext}"

def format_seconds(seconds):
    minutes, sec = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{sec:06.3f}"

def get_media_duration(input_file):
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input_file,
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float(result.stdout.strip())

def temp_audio_path(input_file, ratio):
    stem = sanitize_filename_part(os.path.splitext(os.path.basename(input_file))[0]) or "sample"
    return f"temp_{stem}_{int(ratio * 100):03d}.wav"

async def get_audio_sample(input_file, start_time, output_audio, duration=15):
    """用 ffmpeg 截取音频片段"""
    try:
        cmd = [
            'ffmpeg', '-y', 
            '-ss', str(start_time), 
            '-t', str(duration),
            '-i', input_file,
            '-vn', '-ac', '1', '-ar', '16000', 
            '-loglevel', 'error',
            output_audio
        ]
        # creationflags=0x08000000 用于在 Windows 上隐藏弹出的 CMD 窗口
        subprocess.run(cmd, check=True, creationflags=0x08000000 if os.name == 'nt' else 0)
        return True
    except Exception:
        return False

async def recognize_at(shazam, input_file, ratio, offset):
    print(f"   Trying {int(ratio * 100)}% ({format_seconds(offset)}) ... ", end="", flush=True)
    audio_path = temp_audio_path(input_file, ratio)

    if not await get_audio_sample(input_file, offset, audio_path, SAMPLE_DURATION):
        print("Skip (无法读取)")
        return RecognitionSample(ratio=ratio, offset=offset, error="无法读取 sample")

    try:
        out = await asyncio.wait_for(shazam.recognize(audio_path), timeout=RECOGNIZE_TIMEOUT)
        if "track" not in out:
            print("无结果")
            return RecognitionSample(ratio=ratio, offset=offset)

        track = out["track"]
        title = track.get("title", "").strip()
        artist = track.get("subtitle", "").strip()
        if not title:
            print("无结果")
            return RecognitionSample(ratio=ratio, offset=offset)

        print(f"✅ 命中! -> [{title} - {artist}]")
        return RecognitionSample(ratio=ratio, offset=offset, title=title, artist=artist)
    except asyncio.TimeoutError:
        print("Timeout")
        return RecognitionSample(ratio=ratio, offset=offset, error=f"API timeout {RECOGNIZE_TIMEOUT}s")
    except Exception as e:
        print(f"Err: {e}")
        return RecognitionSample(ratio=ratio, offset=offset, error=str(e))
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

def choose_winner(samples):
    grouped_titles = defaultdict(list)
    for sample in samples:
        if sample.title:
            grouped_titles[normalize_title(sample.title)].append(sample.title)

    if not grouped_titles:
        return None, "no_result"

    vote_counts = Counter({key: len(value) for key, value in grouped_titles.items()})
    top_votes = vote_counts.most_common(1)[0][1]
    top_keys = [key for key, votes in vote_counts.items() if votes == top_votes]

    if top_votes < MIN_VOTES:
        return None, "no_consensus"
    if len(top_keys) > 1:
        return None, "tie"

    winning_titles = grouped_titles[top_keys[0]]
    display_title = Counter(winning_titles).most_common(1)[0][0]
    return display_title, "winner"

def format_report_block(filename, status, samples, winner=None, output_name=None):
    lines = [
        f"File: {filename}",
        f"Status: {status}",
    ]

    if winner:
        lines.append(f"Selected: {winner}")
    if output_name:
        lines.append(f"Output: {output_name}")

    for sample in samples:
        label = f"{int(sample.ratio * 100)}% / {format_seconds(sample.offset)}"
        if sample.title:
            artist = f" - {sample.artist}" if sample.artist else ""
            lines.append(f"{label} -> {sample.title}{artist}")
        elif sample.error:
            lines.append(f"{label} -> error: {sample.error}")
        else:
            lines.append(f"{label} -> no result")

    return "\n".join(lines)

def reset_report(target_dir):
    report_path = os.path.join(target_dir, REPORT_FILENAME)
    if os.path.exists(report_path):
        os.remove(report_path)
    return report_path


def append_report_block(report_path, block):
    with open(report_path, "a", encoding="utf-8") as f:
        if f.tell() > 0:
            f.write("\n\n")
        f.write(block)
        f.write("\n")
        f.flush()
    return report_path

async def main():
    shazam = Shazam()
    
    # --- 1. 自动寻找目标文件夹 ---
    target_dir = None
    print(f"正在寻找待处理文件夹...")
    for folder in POSSIBLE_FOLDERS:
        if os.path.exists(folder):
            print(f"✅ 发现目标文件夹: [{folder}]")
            target_dir = folder
            break
    
    if not target_dir:
        print("\n❌ 错误：找不到任何切片文件夹！")
        print(f"我尝试查找了这些名字: {POSSIBLE_FOLDERS}")
        print("请确认你的切片在哪个文件夹里，然后修改脚本中的 POSSIBLE_FOLDERS 列表。")
        return

    # --- 2. 扫描文件 ---
    # 只处理尚未加歌名的切片文件，例如 01_00_00_03.mp4 或旧版 Song_01.mp4
    files = sorted(
        [f for f in os.listdir(target_dir) if is_unrecognized_file(f)],
        key=file_sort_key,
    )

    if not files:
        print(f"📂 [{target_dir}] 里没有需要改名的视频文件。")
        print("如果是文件名格式问题，请手动修改脚本过滤条件。")
        return

    print(f">>> 准备处理 {len(files)} 个文件 (多点投票模式)...")
    report_path = reset_report(target_dir)

    # --- 3. 开始处理 ---
    for file in files:
        full_path = os.path.join(target_dir, file)
        print(f"\n🎵 分析: {file}")

        try:
            duration = get_media_duration(full_path)
        except Exception as e:
            print(f"❌ 无法读取影片长度: {e}")
            block = format_report_block(file, "duration_error", [
                RecognitionSample(ratio=0, offset=0, error=f"无法读取影片长度: {e}")
            ])
            append_report_block(report_path, block)
            print(f"      └── 已写入识曲清单: {report_path}")
            continue

        samples = []
        for ratio in SAMPLE_RATIOS:
            offset = max(duration * ratio, 0)
            samples.append(await recognize_at(shazam, full_path, ratio, offset))
            await asyncio.sleep(1) # 冷却防封

        winner, status = choose_winner(samples)
        if winner:
            new_name = build_recognized_name(file, winner)
            new_path = os.path.join(target_dir, new_name)

            if not os.path.exists(new_path):
                os.rename(full_path, new_path)
                print(f"      └── 投票通过，重命名为: {new_name}")
                block = format_report_block(file, "renamed", samples, winner=winner, output_name=new_name)
            else:
                print(f"      └── 目标文件已存在，跳过: {new_name}")
                block = format_report_block(file, "target_exists", samples, winner=winner, output_name=new_name)
        else:
            print(f"      └── 无唯一共识，保留原名并写入候选清单 ({status})")
            block = format_report_block(file, status, samples)

        append_report_block(report_path, block)
        print(f"      └── 已写入识曲清单: {report_path}")

    print(f"\n📝 识曲清单已保存至: {report_path}")

    for name in os.listdir("."):
        if name.startswith("temp_") and name.endswith(".wav"):
            try:
                os.remove(name)
            except OSError:
                pass

if __name__ == "__main__":
    # 修复 DeprecationWarning 的正确写法
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户手动停止。")
