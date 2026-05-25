# Auto-Song-Cutter

Auto-Song-Cutter 是一個用 Python 執行的直播歌回自動切片與識曲工具。

目前架構很單純：

- `main.py`：使用 `inaSpeechSegmenter` 偵測音樂片段，呼叫 FFmpeg 依原影片無轉碼切出歌曲片段。
- `recognize_greedy.py`：使用 Shazam 介面對切好的影片做多點取樣識曲，成功後自動改名。
- `requirements.txt`：Python 依賴清單，已針對 Mac Intel 與 Python 3.10 調整版本限制。

## 功能

- 自動偵測影片中的 `music` 區段。
- 合併間隔很短的音樂片段，避免同一首歌被切太碎。
- 依指定秒數修正片頭與片尾。
- 輸出 `Songs_Export` 資料夾。
- 產生 `segments_log.txt`，記錄來源影片、歌曲數量、檔名、起訖時間、長度與檔案大小。
- 對 `Song_XX.mp4` 或 `.mkv` 檔進行 0 秒、60 秒、120 秒三段式識曲。
- 識曲成功後以 `歌名 - 歌手.ext` 重新命名。

## 系統需求

- macOS Intel
- Python 3.10
- FFmpeg

安裝 FFmpeg：

```bash
brew install ffmpeg
```

確認 FFmpeg 可用：

```bash
ffmpeg -version
```

## 安裝

建議使用虛擬環境：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Mac Intel requirements 調整說明

`requirements.txt` 已針對 Mac Intel 固定主要版本，避免 pip 解析到不適合目前架構的組合：

- `inaSpeechSegmenter<0.8.0`：避開新版可能拉入不適合 Mac Intel 環境的 runtime 依賴。
- `tensorflow==2.12.1`：搭配 Python 3.10 使用。
- `numpy==1.24.3`：配合 TensorFlow 2.12.x 與 `inaSpeechSegmenter`，避免安裝到 NumPy 2.x。
- `pyannote.core<6.0.0`、`pyannote.algorithms`：補上 `inaSpeechSegmenter` 相關相容依賴。
- `tqdm`、`shazamio`、`aiohttp`：分別用於進度條與 Shazam 識曲。

如果你不是 Mac Intel，這份 requirements 不一定是最佳組合，尤其 Apple Silicon 或 Linux 可能需要不同的 TensorFlow 安裝方式。

## 使用方式

### 1. 自動切片

```bash
python main.py input.mp4
```

可調整參數：

```bash
python main.py input.mp4 \
  --output Songs_Export \
  --trim_start 3.0 \
  --extend_end 5.0 \
  --min_duration 60 \
  --gap_tolerance 15
```

參數說明：

- `video_path`：輸入影片路徑。
- `--output`：輸出資料夾，預設 `Songs_Export`。
- `--trim_start`：每段開頭往後跳過秒數，預設 `3.0`。
- `--extend_end`：每段結尾延長秒數，預設 `5.0`。
- `--min_duration`：最短歌曲秒數，預設 `60.0`。
- `--gap_tolerance`：兩段音樂間隔小於此秒數時合併，預設 `15.0`。

輸出內容：

```text
Songs_Export/
├── Song_01.mp4
├── Song_02.mp4
└── segments_log.txt
```

### 2. 識曲與改名

切片完成後執行：

```bash
python recognize_greedy.py
```

腳本會依序尋找以下資料夾，找到第一個存在的資料夾後開始處理：

- `Songs_Export`
- `hires`
- `Bilibili_Ready`
- `Bilibili_Upload`
- `Songs_Final_V6`

目前倉庫沒有提供轉檔或 Bilibili 修復腳本；上述資料夾只是 `recognize_greedy.py` 內保留的搜尋清單。一般使用只需要 `Songs_Export`。

識曲流程：

- 掃描 `.mp4` 與 `.mkv`。
- 跳過檔名已包含 ` - ` 的檔案。
- 依序截取 0 秒、60 秒、120 秒附近音訊送到 Shazam。
- 命中後自動改名。

## 專案結構

```text
Auto-Song-Cutter/
├── main.py
├── recognize_greedy.py
├── requirements.txt
└── README.md
```

## 常見問題

### 找不到 FFmpeg

請確認已安裝 FFmpeg，且 `ffmpeg -version` 可以在同一個 shell 中執行。

### 找不到切片資料夾

`recognize_greedy.py` 預設會先找 `Songs_Export`。如果你使用不同輸出資料夾，請修改程式中的 `POSSIBLE_FOLDERS`。

### 識曲結果不理想

可以先調整切片參數，例如提高 `--min_duration` 或降低 `--gap_tolerance`，減少雜談、BGM 或過短片段對識曲的影響。

## License

MIT License
