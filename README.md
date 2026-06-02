# Auto-Song-Cutter

Auto-Song-Cutter 是一個用 Python 執行的直播歌回自動切片與識曲工具。

目前架構很單純：

- `main.py`：使用 `inaSpeechSegmenter` 偵測音樂片段，呼叫 FFmpeg 依原影片無轉碼切出歌曲片段。
- `recognize_greedy.py`：使用 Shazam 介面對切好的影片做多點取樣識曲，成功後自動改名。
- `recut_from_log.py`：讀取 `Songs_Export/segments_log.txt`，依記錄的來源影片與時間戳重新切割。
- `bilibili_tools/download_bilibili_webcut.py`：讀取 Bilibili 回放直鏈或 web-cut URL，使用 FFmpeg 分段下載、續跑與合併。
- `bilibili_tools/bilibili_upload_parts.py`：將切好的 mp4 依序批次上傳到 Bilibili。
- `requirements.txt`：Python 依賴清單，已針對 Mac Intel 與 Python 3.10 調整版本限制。

## 功能

- 自動偵測影片中的 `music` 區段。
- 合併間隔很短的音樂片段，避免同一首歌被切太碎。
- 依指定秒數修正片頭與片尾。
- 輸出 `Songs_Export` 資料夾。
- 產生 `segments_log.txt`，記錄來源影片、歌曲數量、檔名、起訖時間、長度與檔案大小。
- 可依 `segments_log.txt` 重新切割，不需要重新跑 AI 偵測。
- 可依 Bilibili web-cut URL 分段下載直播回放片段，失敗後可從已完成秒數續跑。
- 對 `.mp4` 或 `.mkv` 檔進行多點比例取樣識曲與投票。
- 識曲成功後以 `編號_開始時間_歌名.ext` 重新命名。

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
- `biliup`：Bilibili 投稿上傳工具。

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
├── 01_00_00_03.mp4
├── 02_00_11_58.mp4
└── segments_log.txt
```

切片檔名格式為 `編號_開始時間.mp4`，例如 `02_00_11_58.mp4` 代表第 2 段，開始時間為 `00:11:58`。

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

- 掃描 `.mp4` 與 `.mkv`，只處理尚未加歌名的切片檔，例如 `01_00_00_03.mp4`。
- 依檔名前綴從 `01` 開始排序處理；中間跳號不影響執行。
- 先用 `ffprobe` 讀取每個影片的實際長度，再依比例取樣，不會硬取超過影片長度的位置。
- 每首取 `0%`、`15%`、`25%`、`50%`、`65%`、`85%` 六個位置送到 Shazam。
- 每次 Shazam API 呼叫最多等待 `60` 秒；逾時會記為該取樣點失敗並繼續下一個取樣點。
- 所有取樣完成後才投票；同一個歌名出現 `2` 次以上，且是唯一最高票時才自動改名。
- 自動改名格式為 `編號_開始時間_歌名.ext`，例如 `01_00_00_03_歌名.mp4`。
- 如果沒有歌名達到 2 票，或兩組以上歌名同票打平，保留原檔名，不自動改名。
- 每處理完一首就會立刻追加寫入 `recognition_candidates.txt`，包含自動改名、沒有共識、同票打平、讀取失敗等狀態與每個取樣點的結果。

識曲清單會輸出在找到的切片資料夾內，例如：

```text
Songs_Export/recognition_candidates.txt
```

### 3. 依 log 重新切割

如果 `Songs_Export` 裡已有 `segments_log.txt`，可以不用重新跑 AI 偵測，直接依 log 記錄的時間戳重新切割：

```bash
python recut_from_log.py
```

預設行為：

- 讀取 `Songs_Export/segments_log.txt`。
- 使用 log 第一行的 `Source Video` 當來源影片。
- 依每列的 `Start Time` 與 `End Time` 重切；如果 `End Time` 早於 `Start Time`，會嘗試用 `Duration` 自動修正結束時間。
- 輸出到 `Songs_Recut`，避免覆蓋原本的 `Songs_Export`。
- 重新複製一份 `segments_log.txt` 到輸出資料夾。

參數說明：

| 參數 | 預設值 | 說明 |
| --- | --- | --- |
| `--log` | `Songs_Export/segments_log.txt` | 指定要讀取的 log 檔案。 |
| `--output` | `Songs_Recut` | 指定重新切割後的輸出資料夾。 |
| `--source` | log 內的 `Source Video` | 手動指定來源影片路徑，會覆蓋 log 內記錄的來源影片。原始影片移動位置時很常用。 |
| `--overwrite` | 關閉 | 覆蓋輸出資料夾內已存在的同名檔案。未使用時，已存在的檔案會被跳過。 |
| `--dry-run` | 關閉 | 只讀取 log 並列出會切割的檔名與時間，不實際執行 FFmpeg。 |

常用範例：

```bash
python recut_from_log.py --dry-run
python recut_from_log.py --output Songs_Export_Retry
python recut_from_log.py --overwrite
python recut_from_log.py --source /path/to/source.flv
python recut_from_log.py --log Songs_Export/segments_log.txt --source /path/to/source.flv --output Songs_Recut
```

### 4. Bilibili 工具登入

Bilibili 相關工具集中在 `bilibili_tools/`。使用前必須先提供登入 Cookie，否則工具會直接停止。

可以先用 `biliup login` 登入並產生 cookies：

```bash
biliup login
```

登入後把產生的 cookies 檔放到下列任一位置，或用 `--cookie-file` 指定。

預設會尋找：

```text
bilibili_tools/cookies.json
bilibili_tools/cookies.txt
cookies.json
cookies.txt
~/cookies.json
~/cookies.txt
~/Documents/歌回/cookies.json
~/Documents/歌回/cookies.txt
```

也可以用參數指定：

```bash
--cookie "SESSDATA=...; bili_jct=..."
--cookie-file /path/to/cookies.txt
```

### 5. 下載 Bilibili 回放片段

`bilibili_tools/download_bilibili_webcut.py` 可以讀取 Bilibili 直播回放直鏈，解析 URL 裡的 `start_time` 與 `end_time`，用 FFmpeg 下載。若下載中斷，會記錄已完成秒數並自動從中斷位置繼續；若整個程式被關閉，下次執行同一個命令也會把 URL 的 `start_time` 往後推並續跑。

直鏈範例：

```bash
python bilibili_tools/download_bilibili_webcut.py "https://bvc-live.bilivideo.com/hls-record-gateway/videoPlay?biz_id=live2vod-clip&start_time=1779970646&end_time=1779982079&..."
```

這種 `hls-record-gateway/videoPlay` 直鏈不需要向 Bilibili API 取得 stream，但工具仍會要求已登入 Cookie。

也可以讀取 Bilibili `quick-publish.html` 網址，再向 Bilibili API 取得 stream：

```bash
python bilibili_tools/download_bilibili_webcut.py "https://live.bilibili.com/web-cut/quick-publish.html?start_time=...&end_time=...&live_key=..."
```

使用指定 Cookie：

```bash
python bilibili_tools/download_bilibili_webcut.py "https://live.bilibili.com/web-cut/quick-publish.html?start_time=...&end_time=...&live_key=..." \
  --cookie "SESSDATA=...; bili_jct=..."
```

續跑與合併行為：

- 預設輸出工作資料夾是 `Bilibili_Webcut_Downloads`。
- 分段檔會放在 `Bilibili_Webcut_Downloads/parts/`。
- 直鏈模式會更新 `direct_download_state.json`，記錄已完成秒數與已下載分段。
- 直鏈下載中斷時，會把已下載內容保存成一段，接著用「原始 `start_time` + 已完成秒數」自動繼續下載剩餘內容。
- web-cut API 模式會更新 `download_state.json`，記錄已完成分段、已完成秒數與每個分段的影片尺寸。
- web-cut API 模式會依每個分段的開始與結束時間重新向 Bilibili API 取得 signed URL，不會直接修改舊 URL 的 `start_time/end_time`。
- 每個分段獨立最多重試 3 次；連續 3 次失敗才停止整個下載。
- 下載期間若超過 3 分鐘檔案大小沒有變動，會判定該次下載卡住，終止 FFmpeg 並計為一次失敗。
- 所有分段完成後，會先確認每個分段尺寸；尺寸一致時用 FFmpeg 快速合併。
- 如果有分段尺寸不一致，只會轉換尺寸不同的分段；轉換時遵守等比縮放，必要時補邊，不會硬拉伸畫面。
- 合併會使用 TS 中介重封裝，避免 MP4 concat 時間戳錯亂。
- 合併成功後會刪除 `parts/` 裡的原始分段檔。
- 如果只想下載分段、不合併，可加 `--no-merge`。
- 如果只想測試指定分段，可加 `--only-parts 1-3`；預設會跳過合併並輸出分段比較。
- 如果要測試指定分段的合併結果，可同時加 `--merge-selected`，只合併指定分段並保留原始 parts。

只下載第 1、2、3 段並合併測試：

```bash
python bilibili_tools/download_bilibili_webcut.py "https://live.bilibili.com/web-cut/quick-publish.html?start_time=...&end_time=...&live_key=..." \
  --only-parts 1-3 \
  --merge-selected \
  --output-dir Compare_123 \
  --output Compare_123/merge_123_test.mp4 \
  --overwrite
```

常用參數：

| 參數 | 預設值 | 說明 |
| --- | --- | --- |
| `url` | 必填 | Bilibili `hls-record-gateway` 直鏈，或 web-cut quick-publish URL。 |
| `--output-dir` | `Bilibili_Webcut_Downloads` | 下載狀態、分段檔與預設輸出的資料夾。 |
| `--output` | 依主播與時間產生 | 指定最終合併輸出檔名。 |
| `--cookie` | 空 | 直接傳入 Bilibili Cookie 字串。 |
| `--cookie-file` | 空 | 讀取 Netscape cookies.txt 或純 Cookie 字串檔。 |
| `--role` | `auto` | stream API 路徑，可選 `auto`、`anchor`、`user`。 |
| `--media-url` | 空 | 手動指定 m3u8 或媒體直鏈；指定後不呼叫 Bilibili API。 |
| `--chunk-seconds` | `600` | 每個分段下載秒數。 |
| `--only-parts` | 空 | 只下載指定分段，例如 `1,2,3` 或 `1-3`；預設不合併。 |
| `--merge-selected` | 關閉 | 搭配 `--only-parts` 使用，只合併指定分段並保留原始 parts。 |
| `--compare-parts` | 關閉 | 下載後比較分段 metadata，並檢查前段解碼訊息。 |
| `--check-seconds` | `30` | 比較分段時解碼檢查前幾秒；`0` 代表完整檢查。 |
| `--max-retries` | `3` | 每個分段最多重試次數。 |
| `--stall-timeout` | `180` | 檔案大小連續幾秒沒有變動時判定卡住。 |
| `--overwrite` | 關閉 | 重新下載已完成分段。 |
| `--no-merge` | 關閉 | 只下載分段，不合併最終檔案。 |

### 6. 上傳 Bilibili 分 P

```bash
python bilibili_tools/bilibili_upload_parts.py
```

預設是 dry-run，只會列出 `biliup upload` 指令，不會真的上傳。確認後加：

```bash
python bilibili_tools/bilibili_upload_parts.py --execute
```

常用參數：

| 參數 | 預設值 | 說明 |
| --- | --- | --- |
| `--mp4-dir` | `Songs_Export` | 要上傳的 mp4 資料夾。 |
| `--title` | 腳本預設標題 | 投稿標題。 |
| `--source` | 腳本預設來源 | 原直播或來源連結。 |
| `--description` | 腳本預設說明 | 投稿簡介。 |
| `--tags` | `歌切,seven7酱` | 逗號分隔 tag。 |
| `--episode-limit` | `180` | 每個投稿批次最多 P 數。 |
| `--cookies-file` | 自動搜尋 | 指定 biliup cookies。 |
| `--execute` | 關閉 | 真的執行上傳。 |

## 專案結構

```text
Auto-Song-Cutter/
├── main.py
├── recognize_greedy.py
├── recut_from_log.py
├── bilibili_tools/
│   ├── auth.py
│   ├── download_bilibili_webcut.py
│   └── bilibili_upload_parts.py
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
