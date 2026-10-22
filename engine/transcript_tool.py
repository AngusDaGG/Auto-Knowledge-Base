"""Download a Douyin video's audio and write an unedited local transcript."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import hashlib
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from time import sleep
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

MODEL_PATH = Path(os.environ.get("AKB_MODEL_PATH", str(ROOT.parent / "models" / "faster-whisper-large-v3-turbo")))
URL_PATTERN = re.compile(r"https?://[^\s<>\"'，。；：！？、）】」』]+", re.IGNORECASE)
OUTPUTS = ROOT / "outputs"
LOG = ROOT / "results.jsonl"
KNOWLEDGE_TRANSCRIPTS = ROOT / "videos" / "douyin"

import douyin_download as downloads
import download_diagnostics as trace
import platforms


def extract_url(text: str) -> str:
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?，。；：！？、)]}）】>」』")
        host = (urlsplit(url).hostname or "").lower()
        if host == "douyin.com" or host.endswith(".douyin.com"):
            return url
        reference = platforms.identify(url)
        if reference:
            return reference.url
    raise ValueError("找不到支援的單一影片網址（抖音、YouTube、Instagram、TikTok、Bilibili）")


def stamp(seconds: float, srt: bool = False) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{',' if srt else '.'}{millis:03}"


def safe_id(value: object) -> str:
    result = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or ""))[:120]
    if not result:
        raise ValueError("下載器未提供影片 ID")
    return result


def safe_title(value: object, video_id: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or ""))
    title = re.sub(r"\s+", " ", title).strip(" .")[:120].rstrip(" .")
    if not title or title.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        return video_id
    return title


def output_for_video_id(video_id: str, directory: Path | None = None) -> Path | None:
    directory = directory or OUTPUTS
    for path in directory.glob("*.md"):
        try:
            if re.search(rf"(?m)^- 影片 ID：{re.escape(video_id)}$", path.read_text(encoding="utf-8")):
                return path
        except (OSError, UnicodeError):
            continue
    legacy = directory / safe_id(video_id) / "transcript.md"
    return legacy if legacy.is_file() else None


def choose_output_path(directory: Path, title: object, video_id: str) -> Path:
    base = safe_title(title, video_id)
    candidate = directory / f"{base}.md"
    suffix = 2
    while candidate.exists():
        try:
            existing = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            existing = ""
        if re.search(rf"(?m)^- 影片 ID：{re.escape(video_id)}$", existing):
            return candidate
        candidate = directory / f"{base} ({suffix}).md"
        suffix += 1
    return candidate


def write_outputs(directory: Path, metadata: dict, segments: list, language: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    existing = output_for_video_id(str(metadata["id"]), directory)
    target = existing if existing and existing.parent == directory else choose_output_path(
        directory, metadata.get("title"), str(metadata["id"])
    )
    lines = [
        f"# {metadata.get('title') or metadata['id']}",
        "",
        f"- 原始分享連結：{metadata['source_url']}",
        f"- 影片 ID：{metadata['id']}",
        f"- 原作者：{metadata.get('uploader') or '未知'}",
        f"- 轉錄日期：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 辨識模型：{MODEL_PATH.name}",
        f"- 語言：{language or '未知'}",
        "",
        "## 完整逐字稿",
        "",
    ]
    if metadata.get("platform"):
        lines.insert(4, f"- 來源平台：{metadata['platform']}")
    has_speech = False
    for segment in segments:
        spoken = segment.text.strip()
        if not spoken:
            continue
        has_speech = True
        lines.append(f"[{stamp(segment.start)} → {stamp(segment.end)}] {spoken}")
    if not has_speech:
        lines.append("（未辨識到語音）")
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def is_complete(path: Path) -> bool:
    if path.is_dir():
        path = path / "transcript.md"
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return bool("## 完整逐字稿" in contents and "- 原始分享連結：" in contents
                and "- 辨識模型：" in contents and "- 語言：" in contents
                and (re.search(r"(?m)^\[\d{2}:\d{2}:\d{2}\.\d{3} → ", contents)
                     or "（未辨識到語音）" in contents))


def require_knowledge_transcripts(directory: Path | None = None) -> Path:
    directory = directory or KNOWLEDGE_TRANSCRIPTS
    if not directory.is_dir():
        raise RuntimeError(f"本機知識庫逐字稿目錄不存在：{directory}")
    return directory


def knowledge_transcript(video_id: str, directory: Path | None = None) -> Path | None:
    video_id = safe_id(video_id)
    if directory is None and platforms.platform_for_key(video_id) != "douyin":
        directory = KNOWLEDGE_TRANSCRIPTS.parent / platforms.platform_for_key(video_id)
        if not directory.is_dir():
            return None
    else:
        directory = require_knowledge_transcripts(directory)
    path = directory / f"{video_id}.md"
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"無法讀取本機知識庫逐字稿 {path.name}：{error}") from error
    if (not re.search(rf"(?m)^- 影片 ID：{re.escape(video_id)}$", contents)
            or not is_complete(path)):
        return None
    return path


def migrate_legacy_outputs(directory: Path | None = None) -> tuple[int, list[Path]]:
    directory = directory or OUTPUTS
    migrated = 0
    conflicts = []
    for old in sorted(directory.glob("[0-9]*/transcript.md")):
        video_id = safe_id(old.parent.name)
        try:
            contents = old.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            conflicts.append(old)
            continue
        if (not is_complete(old) or not re.search(
                rf"(?m)^- 影片 ID：{re.escape(video_id)}$", contents)
                or "- 原始分享連結：" not in contents
                or "- 轉錄日期：" not in contents):
            conflicts.append(old)
            continue
        represented = output_for_video_id(video_id, directory)
        if represented and represented.parent == directory:
            if is_complete(represented):
                continue
            conflicts.append(old)
            continue
        target = directory / f"{video_id}.md"
        if target.exists():
            if target.read_text(encoding="utf-8") != contents:
                conflicts.append(old)
            continue
        target.write_text(contents, encoding="utf-8")
        migrated += 1
    return migrated, conflicts


def completed_source_id(source: str) -> str:
    reference = platforms.identify(source)
    video_id = reference.key if reference else downloads.video_id_from(source)
    if video_id and (path := output_for_video_id(safe_id(video_id))) and is_complete(path):
        return video_id
    if LOG.is_file():
        for line in reversed(LOG.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
                if record.get("status") not in ("success", "skipped"):
                    continue
                video_id = safe_id(record.get("video_id"))
                if extract_url(record.get("source", "")) == source and (path := output_for_video_id(video_id)) and is_complete(path):
                    return video_id
            except (ValueError, TypeError, AttributeError):
                continue
    return ""


def log_result(source: str, status: str, video_id: str = "", reason: str = "", category: str = "") -> None:
    with LOG.open("a", encoding="utf-8") as record:
        record.write(json.dumps({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": trace.safe_reason(source),
            "video_id": video_id,
            "status": status,
            "reason": trace.safe_reason(reason),
            "category": category,
        }, ensure_ascii=False) + "\n")


def download_for_transcription(
    source: str,
    temporary: Path,
    ytdlp_first: bool = False,
    seen_ids: set[str] | None = None,
    before_download=None,
):
    """Existing providers only; recoverable source failures get at most two attempts."""
    from urllib.error import URLError

    reference = platforms.identify(source)
    if reference:
        candidate = reference.key
        if seen_ids is not None and candidate in seen_ids:
            return {"id": candidate, "_skip_status": "skipped_duplicate"}, None
        if seen_ids is not None:
            seen_ids.add(candidate)
        if knowledge_transcript(candidate):
            return {"id": candidate, "_skip_status": "skipped_existing"}, None
        existing = output_for_video_id(candidate)
        if existing and is_complete(existing):
            return {"id": candidate, "_skip_status": "skipped"}, None
        import platform_download
        return platform_download.download(reference, temporary, ROOT, before_download, ytdlp_first)

    video_id = downloads.video_id_from(source)
    inspected_ids = set()
    model_ready = False
    errors = []
    last_error = None
    share_cache = None
    share_attempts = 0

    def inspect_video(candidate_id):
        if not candidate_id:
            return None
        candidate_id = safe_id(candidate_id)
        if candidate_id in inspected_ids:
            return None
        if seen_ids is not None and candidate_id in seen_ids:
            return {"id": candidate_id, "_skip_status": "skipped_duplicate"}
        inspected_ids.add(candidate_id)
        if seen_ids is not None:
            seen_ids.add(candidate_id)
        if knowledge_transcript(candidate_id):
            return {"id": candidate_id, "_skip_status": "skipped_existing"}
        existing = output_for_video_id(candidate_id)
        if existing and is_complete(existing):
            return {"id": candidate_id, "_skip_status": "skipped"}
        return None

    def remember_error(method, error, attempt):
        nonlocal last_error
        last_error = error
        errors.append(method + "：" + trace.safe_reason(error))
        category = trace.category(error)
        reason = trace.retry_reason(error)
        trace.event("route", "failed", method=method, error_type=type(error).__name__,
                    attempt=attempt, max_attempts=2, reason_code=reason or "non_retryable")
        if category in ("BLOCKED_PLATFORM", "INVALID_TARGET", "PROTECTED_MEDIA"):
            raise downloads.FallbackError(trace.safe_reason("；".join(errors)), video_id,
                                          category, getattr(error, "stage", "")) from error
        if trace.local_failure(error):
            raise error
        if reason and attempt < 2:
            trace.event("route_retry", "scheduled", method=method, attempt=attempt,
                        max_attempts=2, reason_code=reason)
            sleep(1)
            return True
        return False

    skipped = inspect_video(video_id)
    if skipped:
        return skipped, None

    # Resolve short links before opening a player, downloading, or loading a model.
    # Cache any share metadata for the second-priority route without downloading it.
    if not video_id and not ytdlp_first:
        for resolve_attempt in range(1, 3):
            share_attempts = resolve_attempt
            trace.event("route_attempt", "started", method="share-page", attempt=resolve_attempt, max_attempts=2)
            try:
                share_cache = downloads.share_info(source, video_id)
            except Exception as error:
                if remember_error("share-page", error, resolve_attempt):
                    continue
                raise
            break
        _, video_id, _ = share_cache
        skipped = inspect_video(video_id)
        if skipped:
            return skipped, None

    methods = ("yt-dlp", "share-page", "playwright") if ytdlp_first else (
        "visible-browser", "share-page", "playwright", "yt-dlp")
    options = {"quiet": True, "no_warnings": True, "noplaylist": True,
               "format": "bestaudio/best", "retries": 0, "fragment_retries": 0,
               "extractor_retries": 0, "socket_timeout": 20,
               "max_filesize": downloads.MAX_MEDIA_BYTES, "logger": trace.SafeYtdlpLogger()}
    if os.environ.get("AKB_FFMPEG"):
        options["ffmpeg_location"] = os.environ["AKB_FFMPEG"]
    for method in methods:
        first_attempt = share_attempts if method == "share-page" and share_cache is not None else 1
        for attempt in range(first_attempt, 3):
            cached = method == "share-page" and share_cache is not None and attempt == first_attempt
            if method == "share-page" and attempt > first_attempt:
                share_cache = None
            attempt_temporary = Path(tempfile.mkdtemp(prefix=f"{method}-{attempt}-", dir=temporary))
            trace.event("route_attempt", "cached" if cached else "started", method=method,
                        attempt=attempt, max_attempts=2)
            print({"visible-browser": "主方案：可見瀏覽器擷取原影片播放音訊…",
                   "share-page": "備案：公開分享頁影片資訊…",
                   "playwright": "備案：獨立無頭瀏覽器影片資訊…",
                   "yt-dlp": "第三順位：yt-dlp…"}[method], flush=True)
            try:
                if method == "visible-browser":
                    info = downloads.visible_browser_info(source, video_id, ROOT / "state" / "visible-browser-profile")
                elif method == "share-page":
                    if share_cache is None:
                        share_attempts += 1
                        share_cache = downloads.share_info(source, video_id)
                    info, resolved_id, diagnostics = share_cache
                    if resolved_id and video_id and resolved_id != video_id:
                        raise downloads.FallbackError("分享頁與目標影片 ID 不一致", video_id,
                                                      "INVALID_TARGET", "metadata")
                    video_id = resolved_id or video_id
                    skipped = inspect_video(video_id)
                    if skipped:
                        return skipped, None
                    if not info:
                        raise downloads.FallbackError("；".join(diagnostics) or "分享頁沒有目標影片資料",
                                                      video_id, "NO_SUPPORTED_MEDIA", "metadata")
                elif method == "playwright":
                    info = downloads.browser_info(video_id)
                else:
                    import yt_dlp
                    with trace.stage("metadata", method="yt-dlp"), yt_dlp.YoutubeDL(options) as probe:
                        info = probe.extract_info(source, download=False)
                if not isinstance(info, dict):
                    raise downloads.FallbackError("下載器未傳回影片資訊", video_id, "NO_SUPPORTED_MEDIA", "metadata")
                if info.get("is_live") or info.get("has_drm") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                    raise downloads.FallbackError("不處理直播或受保護媒體", video_id, "PROTECTED_MEDIA", "metadata")
                candidate_id = safe_id(info.get("id"))
                if video_id and candidate_id != video_id:
                    raise downloads.FallbackError("下載來源與目標影片 ID 不一致", video_id,
                                                  "INVALID_TARGET", "metadata")
                video_id = candidate_id
            except Exception as error:
                if remember_error(method, error, attempt):
                    continue
                if ytdlp_first and method == "yt-dlp" and not trace.retry_reason(error) and not downloads.should_fallback(error):
                    raise
                break

            skipped = inspect_video(video_id)
            if skipped:
                return skipped, None
            # Preflight failures are local problems, never reasons to try another site path.
            if before_download and not model_ready:
                try:
                    before_download()
                except Exception as error:
                    error.video_id = video_id
                    raise
                model_ready = True
            try:
                if method == "yt-dlp":
                    download_options = dict(options, outtmpl=str(attempt_temporary / "media.%(ext)s"))
                    with trace.stage("media_download", method="yt-dlp"), yt_dlp.YoutubeDL(download_options) as downloader:
                        downloaded = downloader.extract_info(source, download=True)
                    if not isinstance(downloaded, dict) or safe_id(downloaded.get("id")) != video_id:
                        raise downloads.FallbackError("下載前後影片 ID 不一致", video_id,
                                                      "INVALID_TARGET", "media_download")
                    candidates = [p for p in attempt_temporary.glob("media.*")
                                  if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
                    if not candidates:
                        raise downloads.FallbackError("下載後找不到音訊或媒體檔", video_id,
                                                      "NO_SUPPORTED_MEDIA", "media_download")
                    media = max(candidates, key=lambda p: p.stat().st_size)
                    if not 0 < media.stat().st_size <= downloads.MAX_MEDIA_BYTES:
                        raise downloads.FallbackError("媒體空白或超過 512 MiB 限制", video_id,
                                                      "NO_SUPPORTED_MEDIA", "media_download")
                    downloads.validate_media(media)
                    downloaded["title"] = downloaded.get("title") or info.get("title")
                    downloaded["uploader"] = downloaded.get("uploader") or info.get("uploader")
                    downloaded["download_method"] = method
                    info = downloaded
                else:
                    media = downloads.download_media(info, attempt_temporary)
                print(f"{method} 媒體已下載並驗證", flush=True)
                trace.event("route_attempt", "success", method=method, attempt=attempt, max_attempts=2)
                return info, media
            except Exception as error:
                if remember_error(method, error, attempt):
                    continue
                if ytdlp_first and method == "yt-dlp" and not trace.retry_reason(error) and not downloads.should_fallback(error):
                    raise
                break
    raise downloads.FallbackError(trace.safe_reason("；".join(errors)), video_id,
                                  trace.category(last_error), getattr(last_error, "stage", "")) from last_error


def acquisition_metadata(info: dict, source: str) -> dict:
    """Trusted observed metadata only; never infer duration from ASR segment ends."""
    key = safe_id(info.get("id"))
    platform = platforms.platform_for_key(key)
    reference = platforms.identify(source)
    if reference:
        if reference.key != key:
            raise downloads.FallbackError("來源與取得影片 ID 不符", key, "INVALID_TARGET", "metadata")
        canonical = reference.url
        raw_id = reference.raw_id
    else:
        if platform != "douyin" or not key.isdigit() or not downloads.supported_page(source):
            raise downloads.FallbackError("無法確認原始影片來源", key, "INVALID_TARGET", "metadata")
        canonical = f"https://www.douyin.com/video/{key}"
        raw_id = key
    if info.get("is_live") or info.get("has_drm") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise downloads.FallbackError("不處理直播或受保護媒體", key, "PROTECTED_MEDIA", "metadata")
    duration = info.get("expected_duration")
    evidence = "player_metadata"
    if duration is None:
        duration = info.get("duration")
        evidence = "extractor_metadata"
    if duration is None and isinstance(info.get("video"), dict):
        milliseconds = info["video"].get("duration")
        if type(milliseconds) in (int, float):
            duration = milliseconds / 1000
            evidence = "public_video_metadata"
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise downloads.FallbackError("影片片長無法確認，停止轉錄", key, "INVALID_TARGET", "metadata")
    return {"version": 1, "platform": platform, "video_id": key, "raw_id": raw_id,
            "canonical_url": canonical, "source_url": canonical,
            "duration_seconds": duration, "duration_evidence": evidence,
            "title": str(info.get("title") or key), "author": str(info.get("uploader") or "未知"),
            "content_type": "whisper_asr", "method": str(info.get("download_method") or "unknown")}


def save_acquisition(record: dict, transcript: Path) -> None:
    folder = ROOT / "state" / "acquisitions"
    folder.mkdir(parents=True, exist_ok=True)
    record = dict(record, acquired_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                  transcript_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest())
    target = folder / f"{safe_id(record['video_id'])}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def process_one(
    text: str,
    model,
    ytdlp_first: bool = False,
    seen_ids: set[str] | None = None,
    seen_sources: set[str] | None = None,
) -> tuple[str, str, str]:
    require_knowledge_transcripts()
    source = extract_url(text)
    if seen_sources is not None:
        if source in seen_sources:
            return "skipped_duplicate", "", "略過同批次重複連結"
        seen_sources.add(source)
    existing_id = completed_source_id(source)
    if existing_id:
        existing_id = safe_id(existing_id)
        if seen_ids is not None and existing_id in seen_ids:
            return "skipped_duplicate", existing_id, f"略過同批次重複影片：{existing_id}"
        if seen_ids is not None:
            seen_ids.add(existing_id)
        if knowledge_transcript(existing_id):
            return "skipped_existing", existing_id, f"略過知識庫既有完整逐字稿：{existing_id}"
        return "skipped", existing_id, f"略過已完成影片：{existing_id}"
    temporary = Path(tempfile.mkdtemp(prefix="douyin_", dir=ROOT))
    media = None
    try:
        downloaded, media = download_for_transcription(
            source, temporary, ytdlp_first=ytdlp_first, seen_ids=seen_ids,
            before_download=model if callable(model) else None,
        )
        video_id = safe_id(downloaded.get("id"))
        if media is None:
            status = downloaded.get("_skip_status", "skipped")
            if status == "skipped_existing":
                message = f"略過知識庫既有完整逐字稿：{video_id}"
            elif status == "skipped_duplicate":
                message = f"略過同批次重複影片：{video_id}"
            else:
                message = f"略過已完成影片：{video_id}"
            return status, video_id, message
        acquisition = acquisition_metadata(downloaded, source)
        print("媒體下載完成，開始本機辨識…", flush=True)
        with trace.stage("transcription"):
            active_model = model() if callable(model) else model
            segments, result = active_model.transcribe(str(media), beam_size=5, vad_filter=True)
            segment_list = list(segments)
        metadata = {
            "id": video_id,
            "title": downloaded.get("title"),
            "uploader": downloaded.get("uploader"),
            "source_url": source,
        }
        if downloaded.get("platform"):
            metadata["platform"] = downloaded["platform"]
        output = write_outputs(OUTPUTS, metadata, segment_list, result.language)
        save_acquisition(acquisition, output)
    finally:
        if media is not None and media.exists():
            media.unlink()
        try:
            temporary.rmdir()
        except OSError:
            pass
    return "success", video_id, f"完成：{video_id} → {output}"


def main(argv: list[str] | None = None) -> int:
    global ROOT, OUTPUTS, LOG, KNOWLEDGE_TRANSCRIPTS, MODEL_PATH
    parser = argparse.ArgumentParser(description="影片連結本機逐字稿工具", allow_abbrev=False)
    parser.add_argument("--batch", action="store_true", help="依序處理 urls.txt")
    parser.add_argument("--url", help="支援的單一影片連結或分享文字")
    parser.add_argument("--model-path", type=Path, help="已備妥的本機 faster-whisper 模型目錄；不下載模型")
    parser.add_argument("--yt-dlp-first", action="store_true", help="改用舊的 yt-dlp 優先流程")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--workspace", type=Path, help="日常入口資料夾；outputs/state/urls.txt 留在此處")
    parser.add_argument("--vendor-dir", type=Path, help="沿用既有 yt-dlp vendor，不安裝套件")
    parser.add_argument("--no-sync", action="store_true", help="只轉錄；開源引擎一律停用遠端同步")
    args = parser.parse_args(argv)
    if args.model_path:
        MODEL_PATH = args.model_path.expanduser().resolve()
    args.no_sync = True  # The standalone engine never publishes data.
    if args.workspace:
        ROOT = args.workspace.resolve()
        KNOWLEDGE_TRANSCRIPTS = ROOT / "videos" / "douyin"
        OUTPUTS = ROOT / "outputs"
        LOG = ROOT / "state" / "results.jsonl"
        LOG.parent.mkdir(parents=True, exist_ok=True)
    if args.vendor_dir:
        sys.path.insert(0, str(args.vendor_dir.resolve()))
    try:
        require_knowledge_transcripts()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    migrated, conflicts = migrate_legacy_outputs()
    if migrated:
        print(f"已安全遷移 {migrated} 份舊逐字稿至 outputs\\<影片 ID>.md（舊檔保留）")
    if conflicts:
        print("以下舊逐字稿未遷移，請人工檢查：" + ", ".join(str(path) for path in conflicts), file=sys.stderr)
    if args.batch:
        entries = (ROOT / "urls.txt").read_text(encoding="utf-8-sig").splitlines()
        entries = [entry.strip() for entry in entries if entry.strip() and not entry.lstrip().startswith("#")]
    else:
        entries = [args.url if args.url is not None else input("貼上影片連結／分享文字（輸入 B 讀取 urls.txt）：").strip()]
        if entries[0].upper() == "B":
            batch_args = ["--batch", "--device", args.device, "--compute-type",
                          args.compute_type or ("float16" if args.device == "cuda" else "int8")]
            if args.no_sync:
                batch_args.append("--no-sync")
            if args.yt_dlp_first:
                batch_args.append("--yt-dlp-first")
            if args.workspace:
                batch_args.extend(["--workspace", str(args.workspace)])
            if args.vendor_dir:
                batch_args.extend(["--vendor-dir", str(args.vendor_dir)])
            if args.model_path:
                batch_args.extend(["--model-path", str(args.model_path)])
            return main(batch_args)
    if not entries:
        print("沒有待處理連結")
        print("批次摘要：輸入 0 筆；新影片處理 0 筆；重複／略過 0 筆；成功 0 筆；失敗 0 筆")
        return 0
    model_cache = None

    def load_model():
        nonlocal model_cache
        if model_cache is None:
            if not MODEL_PATH.is_dir():
                raise RuntimeError("本機模型目錄不存在，請設定 --model-path 或 AKB_MODEL_PATH；不自動下載")
            from faster_whisper import WhisperModel
            model_cache = WhisperModel(
                str(MODEL_PATH), device=args.device,
                compute_type=args.compute_type or ("float16" if args.device == "cuda" else "int8"),
                local_files_only=True,
            )
        return model_cache

    failed = 0
    succeeded = 0
    skipped = 0
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    for index, entry in enumerate(entries, 1):
        print(f"[{index}/{len(entries)}] 處理中…", flush=True)
        try:
            with trace.Trace(ROOT / "state" / "download_diagnostics.jsonl") as diagnostic:
                status, video_id, message = process_one(
                    entry, load_model, ytdlp_first=args.yt_dlp_first,
                    seen_ids=seen_ids, seen_sources=seen_sources,
                )
                diagnostic.data.update(status=status, video_id=video_id)
            print(message)
            log_result(entry, status, video_id)
            if status == "success":
                succeeded += 1
            elif status.startswith("skipped"):
                skipped += 1
        except Exception as error:
            reason = trace.safe_reason(error)
            print(f"失敗：{reason}", file=sys.stderr)
            log_result(entry, "failed", getattr(error, "video_id", ""), reason, trace.category(error))
            failed += 1
    print(
        f"批次摘要：輸入 {len(entries)} 筆；新影片處理 {succeeded + failed} 筆；"
        f"重複／略過 {skipped} 筆；成功 {succeeded} 筆；失敗 {failed} 筆"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
