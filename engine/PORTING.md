# 獨立轉錄引擎

本版從既有第一方轉錄引擎的明確程式／測試清單移植；保留平台來源驗證、CDN 白名單、媒體大小與解碼／長度檢查，以及 Douyin／YouTube 每個 provider 至多兩輪的 bounded retry。登入、驗證碼、明確拒絕、來源不符、DRM 與本機故障不以另一條路由繞過。Bilibili 仍為實驗支援。

## 呼叫契約

```powershell
python -B engine/transcript_tool.py --workspace PATH --no-sync --model-path LOCAL_MODEL_DIR --url "VIDEO_URL"
```

工作區須先有 `videos/douyin/`，主程式的初始化命令負責建立。引擎沿用 `outputs/*.md`、`state/results.jsonl`、`state/download_diagnostics.jsonl`，以平台與影片 ID 去重。成功轉錄才原子寫入 `state/acquisitions/<stable-key>.json`，包含平台／ID、去除追蹤參數的 canonical 來源、真實 metadata 片長、標題、作者、取得方法、`content_type=whisper_asr` 與稿件 SHA-256；沒有可信正值片長即停止，絕不以 ASR 末端估片長。已完成舊稿若沒有收據，不自行補造。

`--no-sync` 保留相容性；本引擎一律停用 Git／遠端同步，即使省略旗標也不發布。`knowledge_sync.py` 僅保留逐字稿欄位／來源／憑證樣式驗證與停用的相容函式。

## 環境

初始支援 Windows；離線測試使用 Python 3.12.10、Node 24.15.0。未宣稱 Linux／macOS 瀏覽器實測。Python 套件由正常環境匯入，亦可明確指定 `--vendor-dir PATH` 沿用既有安裝；本目錄不附 vendor。`requirements-transcribe.txt` 與根目錄 `package.json` 記錄現有依賴版本，不自動安裝。

| 設定 | 用途 |
|---|---|
| `--model-path`／`AKB_MODEL_PATH` | 已備妥的本機 faster-whisper 模型目錄；只以 `local_files_only=True` 載入 |
| `AKB_NODE` | Node 執行檔；未設時使用 PATH 的 `node` |
| `AKB_PLAYWRIGHT_MODULE` | 可選 Playwright 模組目錄；未設時用 Node 的正常 `require.resolve` 查找 |
| `AKB_BROWSER_EXECUTABLE` | 明確指定已安裝的 Edge／Chrome 執行檔 |
| `AKB_BROWSER_CHANNEL` | `msedge` 或 `chrome`；未設時找一般機器安裝，缺少時明確報錯 |
| `AKB_FFMPEG` | 可選 FFmpeg 執行檔或目錄；未設時由 yt-dlp 使用 PATH |

專用瀏覽器 profile 固定在工作區的 `state/visible-browser-profile/` 或 `state/<platform>/visible-browser-profile/`。不讀取個人 profile／Cookie，不自動登入、不解 CAPTCHA 或 DRM，不安裝瀏覽器、不下載模型。轉錄需讀取公開來源，ASR 在本機執行；這不代表整個 Agent 工作階段完全離線。

## 驗證範圍

```powershell
python -B run_tests.py engine
node --test engine/test_browser_probe.cjs engine/test_browser_config.cjs
```

測試使用合成 ID、mock 與隔離的系統暫存資料夾，不碰個人 profile。測試資料不遞迴刪除；受限環境可將 `TEMP`／`TMP` 指向另備的可寫測試區。離線測試驗證路由、拒絕邊界、重試、來源／長度與收據契約，不能替代新電腦或新平台的實際下載／音訊辨識驗收。本次未下載真實影片或模型。

外部方法及依賴授權見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。來源檔 SHA-256 與本機測試紀錄由匯出工作區外的審計收據保存，不將舊私人目錄或記錄包入專案。
