import asyncio
import os
import re
import subprocess
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

TEMP_AUDIO = "temp_sample.wav"
CHECK_POINTS = [0, 60, 120] # 0秒, 60秒, 120秒 三次贪心采样
UNCHECKED_FILENAME_PATTERN = re.compile(r"^(\d{2}_\d{2}_\d{2}_\d{2}|Song_\d{2})$")
# ============================================

def sanitize_filename_part(text):
    """移除不适合放在文件名里的字符"""
    return "".join([c for c in text if c not in r'/:*?"<>|']).strip()

def is_unrecognized_file(filename):
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in [".mp4", ".mkv"]:
        return False
    if filename.startswith("temp_"):
        return False
    return bool(UNCHECKED_FILENAME_PATTERN.match(stem))

def build_recognized_name(filename, title):
    stem, ext = os.path.splitext(filename)
    safe_title = sanitize_filename_part(title) or "Unknown"
    return f"{stem}_{safe_title}{ext}"

async def get_audio_sample(input_file, start_time, duration=15):
    """用 ffmpeg 截取音频片段"""
    try:
        cmd = [
            'ffmpeg', '-y', 
            '-ss', str(start_time), 
            '-t', str(duration),
            '-i', input_file,
            '-vn', '-ac', '1', '-ar', '16000', 
            '-loglevel', 'error',
            TEMP_AUDIO
        ]
        # creationflags=0x08000000 用于在 Windows 上隐藏弹出的 CMD 窗口
        subprocess.run(cmd, check=True, creationflags=0x08000000 if os.name == 'nt' else 0)
        return True
    except Exception:
        return False

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
    files = [f for f in os.listdir(target_dir) if is_unrecognized_file(f)]

    if not files:
        print(f"📂 [{target_dir}] 里没有需要改名的视频文件。")
        print("如果是文件名格式问题，请手动修改脚本过滤条件。")
        return

    print(f">>> 准备处理 {len(files)} 个文件 (贪心模式)...")

    # --- 3. 开始处理 ---
    for file in files:
        full_path = os.path.join(target_dir, file)
        print(f"\n🎵 分析: {file}")
        
        found_song = False
        
        for offset in CHECK_POINTS:
            print(f"   Trying {offset}s ... ", end="", flush=True)
            
            if not await get_audio_sample(full_path, offset):
                print("Skip (无法读取)")
                continue
                
            try:
                out = await shazam.recognize(TEMP_AUDIO)
                
                if 'track' in out:
                    track = out['track']
                    title = track['title']
                    subtitle = track['subtitle']
                    
                    print(f"✅ 命中! -> [{title} - {subtitle}]")
                    
                    # 保持原始编号与开始时间前缀，例如 01_00_00_03_歌名.mp4
                    new_name = build_recognized_name(file, title)
                    new_path = os.path.join(target_dir, new_name)
                    
                    if not os.path.exists(new_path):
                        os.rename(full_path, new_path)
                        print(f"      └── 重命名完成")
                    else:
                        print(f"      └── 目标文件已存在，跳过")
                    
                    found_song = True
                    break 
                else:
                    print("无结果")
            except Exception as e:
                print(f"Err: {e}")
                
            await asyncio.sleep(1) # 冷却防封
            
        if not found_song:
            print(f"❌ 失败，这首歌可能是翻唱太冷门。")

    if os.path.exists(TEMP_AUDIO):
        os.remove(TEMP_AUDIO)

if __name__ == "__main__":
    # 修复 DeprecationWarning 的正确写法
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户手动停止。")
