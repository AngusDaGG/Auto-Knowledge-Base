"""Small local receipts for the current Agent session; no inference service."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid
from urllib.parse import urlsplit


def _plain(path: Path) -> Path:
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path.absolute(), *path.absolute().parents)):
        raise ValueError("不讀取符號連結／junction 來源")
    return path.absolute()


def digest(path: Path) -> str:
    return hashlib.sha256(_plain(path).read_bytes()).hexdigest()


def _file_id(path: Path) -> list[int]:
    return _stat_id(_plain(path).stat(follow_symlinks=False))


def _stat_id(info) -> list[int]:
    # Keep Python 3.12's full Windows file ID (which may be 128 bits).
    return [info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", 0)]


def _open_locked(path: Path, *, delete: bool = False) -> int:
    """Return a CRT fd owning a native handle that denies writes and replacement."""
    if sys.platform != "win32":
        raise ValueError("此平台沒有已驗證的原件鎖定歸檔方法；保留原件")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ excludes FILE_SHARE_WRITE and FILE_SHARE_DELETE. Opening
    # the reparse point itself prevents following a swapped final-component link.
    # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
    handle = api.CreateFileW(str(_plain(path)), 0x80000000 | (0x10000 if delete else 0),
                             0x1, None, 3, 0x80 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    return descriptor  # os.close now owns CloseHandle; never close the raw handle twice.


def _read_locked(descriptor: int) -> bytes:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("鎖定對象必須是一般檔案，不接受 reparse point")
    os.lseek(descriptor, 0, os.SEEK_SET)
    data = b""
    while chunk := os.read(descriptor, 65536):
        data += chunk
        if len(data) > 2_000_000:
            raise ValueError("歸檔內容超過 2 MB，保留原件")
    return data


def _delete_locked(descriptor: int) -> None:
    """Mark only the verified native file object for deletion on handle close."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    # FileDispositionInfo = 4, FILE_DISPOSITION_INFO contains one BOOLEAN.
    # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle
    disposition = ctypes.c_ubyte(1)
    if not api.SetFileInformationByHandle(msvcrt.get_osfhandle(descriptor), 4,
                                          ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise ctypes.WinError(ctypes.get_last_error())


def _archive_locked(source: Path, destination: Path, expected_hash: str, expected_id: list[int]) -> None:
    source_fd, destination_fd = None, None
    try:
        source_fd = _open_locked(source, delete=True)
        if _stat_id(os.fstat(source_fd)) != expected_id:
            raise ValueError("原件已被替換，保留原件並停止歸檔")
        data = _read_locked(source_fd)
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("原件已改變，保留原件並停止歸檔")
        try:
            with _plain(destination).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass  # Existing bytes must pass the locked readback below.
        destination_fd = _open_locked(destination)
        if hashlib.sha256(_read_locked(destination_fd)).hexdigest() != expected_hash:
            raise ValueError("歸檔目的地已改變，保留原件並停止")
        # Source is still the same locked object: no pathname check+unlink race.
        # Keep destination locked until the source handle is actually closed.
        _delete_locked(source_fd)
        os.close(source_fd)
        source_fd = None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def positive(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(label + " 必須為有限正數")
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def task_path(repo: Path, key: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,140}", key):
        raise ValueError("不合法的任務 ID")
    return _plain(repo / "state" / "tasks" / f"{key}.json")


def save(repo: Path, task: dict) -> dict:
    path = task_path(repo, task["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    task["updated_at"] = now()
    temporary = _plain(path.with_suffix(".json.tmp"))
    temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return task


def read(repo: Path, key: str) -> dict:
    return json.loads(task_path(repo, key).read_text(encoding="utf-8"))


def tasks(repo: Path) -> list[dict]:
    return [json.loads(_plain(p).read_text(encoding="utf-8"))
            for p in sorted(_plain(repo / "state" / "tasks").glob("*.json"))]


def identity(value: str, tool) -> tuple[str, str, str]:
    url = tool.extract_url(value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("只接受不含憑證的 HTTPS 影片網址")
    reference = tool.platforms.identify(url)
    if reference:
        return reference.key, reference.platform, reference.url
    if parsed.hostname not in {"douyin.com", "www.douyin.com", "v.douyin.com", "m.douyin.com"}:
        raise ValueError("未支援的來源")
    raw_id = tool.downloads.video_id_from(url)
    if raw_id:
        return str(raw_id), "douyin", f"https://www.douyin.com/video/{raw_id}"
    # A short link is resolved by the existing engine, never by a new downloader.
    clean = f"https://{parsed.hostname}{parsed.path}"
    return "pending_" + hashlib.sha256(clean.encode()).hexdigest()[:24], "douyin", clean


def source_metadata(path: Path, tool) -> dict:
    source_hash = digest(path)
    if not tool.is_complete(path):
        raise ValueError("來源逐字稿未完成")
    text = path.read_text(encoding="utf-8-sig")
    key = re.search(r"(?m)^- 影片 ID：([A-Za-z0-9_-]+)\s*$", text)
    url = re.search(r"(?m)^- 原始分享連結：(https://\S+)\s*$", text)
    segments = re.findall(r"(?m)^\[(\d{2}:\d{2}:\d{2}\.\d{3}) → (\d{2}:\d{2}:\d{2}\.\d{3})\] (.+)$", text)
    if not key or not url or not segments or "（未辨識到語音）" in text:
        raise ValueError("來源沒有可驗證的語音內容／識別資料")
    source_key, platform, canonical = identity(url[1], tool)
    if not source_key.startswith("pending_") and source_key != key[1]:
        raise ValueError("來源網址與影片 ID 不符")
    previous = 0.0
    for start, end, words in segments:
        def seconds(stamp):
            h, m, s = stamp.split(":")
            if int(m) >= 60 or float(s) >= 60:
                raise ValueError("時間戳超出有效範圍")
            return int(h) * 3600 + int(m) * 60 + float(s)
        begin, finish = seconds(start), seconds(end)
        if begin < previous - 0.1 or finish < begin or not words.strip():
            raise ValueError("時間戳順序或內容不正確")
        previous = finish
    model = re.search(r"(?m)^- 辨識模型：(.+)$", text)
    return {"id": key[1], "platform": platform, "source_url": url[1], "canonical_url": canonical,
            "content_type": "whisper_asr", "method": "Video2Markdown/local-Whisper",
            "model": model[1] if model else "unknown", "source_path": str(path.resolve()),
            "source_sha256": source_hash, "segment_count": len(segments), "last_timestamp_seconds": previous}


def available_source(repo: Path, key: str, canonical: str, tool) -> Path | None:
    receipt = task_path(repo, key)
    if receipt.exists():
        task = read(repo, key)
        if task.get("resolved_id"):
            task = read(repo, task["resolved_id"])
        if task.get("source_path"):
            path = Path(task["source_path"])
            if digest(path) != task.get("source_sha256"):
                raise ValueError("已取得來源內容有版本差異；停止重新取得")
            source_metadata(path, tool)
            return path
    candidates = list((repo / "videos").glob("*/*.md")) + list((repo / "outputs").glob("*.md"))
    for path in candidates:
        text = path.read_text(encoding="utf-8-sig")
        match = re.search(r"(?m)^- 影片 ID：([A-Za-z0-9_-]+)\s*$", text)
        source = re.search(r"(?m)^- 原始分享連結：(https://\S+)\s*$", text)
        if match and (match[1] == key or (key.startswith("pending_") and source and identity(source[1], tool)[2] == canonical)):
            source_metadata(path, tool)
            return path
    return None


def existing_card(repo: Path, key: str, canonical: str) -> Path | None:
    from kb_knowledge import parse_card, validate_knowledge
    for path in (repo / "knowledge").glob("*/*.md"):
        try:
            card = parse_card(path)["metadata"]
        except ValueError:
            continue
        if card.get("type") != "distilled_video_knowledge" or not card.get("video_id"):
            continue  # Document source URLs do not establish a video identity.
        if str(card.get("video_id")) == key or card.get("source_url") == canonical:
            # A draft or manually changed card must not become a permanent skip.
            record = task_path(repo, str(card["video_id"]))
            if record.exists():
                task = read(repo, str(card["video_id"]))
                if task.get("stage") != "published" or task.get("card_sha256") != digest(path):
                    return None
                if task.get("source_path") and digest(Path(task["source_path"])) != task.get("source_sha256"):
                    return None
            try:
                validate_knowledge(repo, strict_paths=[path.relative_to(repo).as_posix()] if "content_type" in card else [])
            except ValueError:
                return None
            return path
    return None


def register_source(repo: Path, path: Path, tool, *, duration_seconds: float | None = None,
                    discovery: dict | None = None, max_seconds: int = 600) -> dict:
    info = source_metadata(path, tool)
    location = task_path(repo, info["id"])
    task = read(repo, info["id"]) if location.exists() else {"schema": 1, "created_at": now()}
    positive(max_seconds, "max_seconds")
    duration_seconds = duration_seconds if duration_seconds is not None else task.get("duration_seconds")
    if info["last_timestamp_seconds"] > max_seconds + 2:
        raise ValueError("既有來源超出本次片長上限；保留原稿，不截取")
    if task.get("source_sha256") and task["source_sha256"] != info["source_sha256"]:
        raise ValueError("同 ID 來源有版本差異；保留既有任務，不覆寫")
    task.update(info)
    if duration_seconds is not None:
        positive(duration_seconds, "duration_seconds")
        if duration_seconds > max_seconds or info["last_timestamp_seconds"] > duration_seconds + 2:
            raise ValueError("來源時間戳超過宣告片長")
        if duration_seconds - info["last_timestamp_seconds"] > 10 and info["last_timestamp_seconds"] < duration_seconds * 0.75:
            raise ValueError("片尾覆蓋不足，需確認是否截斷或長段無語音；不判成成功")
        task["duration_seconds"] = duration_seconds
    task["max_seconds"] = max_seconds
    if discovery:
        task["discovery"] = discovery
    if task.get("stage") not in {"refined", "validated", "branch_pushed", "main_pushed", "published", "needs_review"}:
        task["stage"] = "acquired"
    if task.get("card_path") and (not (repo / task["card_path"]).is_file() or digest(repo / task["card_path"]) != task.get("card_sha256")):
        task["stage"] = "needs_review"
    task.pop("error", None)
    return save(repo, task)


def prepare(repo: Path, urls: list[str], tool, *, max_items: int = 3, max_seconds: int = 600,
            duration_seconds: float | None = None, topic: str = "", discovered_via: str = "") -> dict:
    positive(max_items, "max_items")
    positive(max_seconds, "max_seconds")
    if not isinstance(max_items, int):
        raise ValueError("max_items 必須為整數")
    if duration_seconds is not None:
        positive(duration_seconds, "duration_seconds")
    result = {"tasks": [], "skipped": [], "failed": [], "deferred": []}
    seen = set()
    for value in urls:
        try:
            key, platform, canonical = identity(value, tool)
            if key in seen:
                result["skipped"].append({"id": key, "reason": "same_batch_duplicate"})
                continue
            seen.add(key)
            card = existing_card(repo, key, canonical)
            if card:
                result["skipped"].append({"id": key, "reason": "existing_knowledge", "card": str(card)})
                continue
            if len(result["tasks"]) >= max_items:
                result["deferred"].append({"id": key, "reason": "batch_limit"})
                continue
            if duration_seconds is not None and (duration_seconds <= 0 or duration_seconds > max_seconds):
                result["deferred"].append({"id": key, "reason": "duration_limit"})
                continue
            discovery = {"topic": topic, "found_via": discovered_via, "selected_url": canonical}
            source = available_source(repo, key, canonical, tool)
            if source:
                task = register_source(repo, source, tool, duration_seconds=duration_seconds, discovery=discovery, max_seconds=max_seconds)
            else:
                path = task_path(repo, key)
                task = read(repo, key) if path.exists() else {
                    "schema": 1, "id": key, "platform": platform, "source_url": tool.extract_url(value),
                    "canonical_url": canonical, "stage": "pending", "created_at": now()}
                task.update({"discovery": discovery, "max_seconds": max_seconds})
                if duration_seconds is not None:
                    task["duration_seconds"] = duration_seconds
                save(repo, task)
            result["tasks"].append(task)
        except (ValueError, OSError) as error:
            result["failed"].append({"error_type": type(error).__name__, "reason": str(error)})
    return result


def acquire(repo: Path, tool_dir: Path, key: str, sync, tool, importer, *, authorized: bool = False,
            duration_seconds: float | None = None, timeout_seconds: int = 1800) -> dict:
    task = read(repo, key)
    positive(timeout_seconds, "timeout_seconds")
    source = available_source(repo, key, task["canonical_url"], tool)
    if source:
        return register_source(repo, source, tool, duration_seconds=duration_seconds, max_seconds=task.get("max_seconds", 600))
    if existing_card(repo, key, task["canonical_url"]):
        return {"id": key, "stage": "skipped", "reason": "existing_knowledge"}
    if not authorized:
        raise ValueError("尚未確認此來源可取得；沒有開始下載")
    duration = duration_seconds if duration_seconds is not None else task.get("duration_seconds")
    if duration is not None:
        positive(duration, "duration_seconds")
    if duration is None or duration > positive(task.get("max_seconds", 600), "max_seconds"):
        raise ValueError("先確認片長並提供範圍內的 duration-seconds；沒有開始下載")
    task.update({"duration_seconds": duration, "stage": "pending"})
    save(repo, task)
    command = [sys.executable, "-B", str(tool_dir / "transcript_tool.py"), "--workspace", str(repo), "--no-sync",
               "--url", task["canonical_url"]]
    if (tool_dir / "vendor").is_dir():
        command.extend(["--vendor-dir", str(tool_dir / "vendor")])
    log = repo / "state" / "results.jsonl"
    log_offset = log.stat().st_size if log.exists() else 0
    try:
        # Existing engine owns diagnostics and platform refusal rules. Never calls Gemini here.
        proc = subprocess.run(command, cwd=repo, timeout=timeout_seconds)
        if proc.returncode:
            raise RuntimeError("existing_engine_failed; inspect local engine diagnostics")
        if (repo / "outputs").is_dir():
            importer(repo / "outputs", repo, sync, tool)
        source = available_source(repo, key, task["canonical_url"], tool)
        if not source and key.startswith("pending_") and log.exists():
            with log.open("rb") as handle:
                handle.seek(log_offset)
                records = [json.loads(line) for line in handle.read().decode("utf-8").splitlines() if line.strip()]
            if len(records) == 1 and records[0].get("status") in {"success", "skipped", "skipped_existing"}:
                resolved = records[0].get("video_id", "")
                if re.fullmatch(r"\d+", resolved):
                    source = available_source(repo, resolved, task["canonical_url"], tool)
        if not source:
            raise RuntimeError("engine_returned_no_valid_content")
        completed = register_source(repo, source, tool, duration_seconds=duration, discovery=task.get("discovery"), max_seconds=task.get("max_seconds", 600))
        if completed["id"] != key:
            task.update({"stage": "resolved", "resolved_id": completed["id"]})
            save(repo, task)
        return completed
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        task.update({"stage": "failed", "error": type(error).__name__, "next_step": "inspect_existing_engine_diagnostics"})
        save(repo, task)
        raise RuntimeError(f"取得失敗：{type(error).__name__}；資料與診斷留本機，不啟動 Gemini") from None


def _document_source(task: dict) -> None:
    if digest(Path(task["source_path"])) != task["source_sha256"]:
        raise ValueError("來源快照已改變，請先核對差異")
    if task.get("platform") != "document":
        return
    for origin in task.get("originals", []):
        if origin["intake"] != "markdown" or origin.get("archive_state") in {"archived", "replaced"}:
            continue
        original = _plain(Path(origin["original_path"]))
        candidate = original
        if not original.exists() and origin.get("archive_state") == "pending":
            candidate = _plain(Path(origin["archive_path"]))
        if not candidate.is_file() or digest(candidate) != task["source_sha256"]:
            raise ValueError("Markdown 原件已改變或遺失，不能沿用已完成狀態")
        if candidate == original and _file_id(original) != origin["original_file_id"]:
            raise ValueError("Markdown 原件已被替換，請重新掃描")


def import_text(repo: Path, source: Path, *, source_url: str = "", _intake: str = "external") -> dict:
    repo, source = _plain(repo), _plain(source)
    if (source.suffix.lower() not in {".txt", ".md"} or source.name.lower().startswith(".env")
            or _intake not in {"external", "markdown"}):
        raise ValueError("只匯入使用者指定的文字／Markdown 檔案")
    if _intake == "markdown" and (source.parent != repo / "markdown" or source.suffix.lower() != ".md"):
        raise ValueError("Markdown 收件只接受 markdown 頂層 .md")
    original_file_id = _file_id(source)
    data = source.read_bytes()
    text = data.decode("utf-8-sig")
    if not text.strip() or len(data) > 2_000_000:
        raise ValueError("文字為空或超過 2 MB")
    if re.search(r"AIza[\w-]{30,}|-----BEGIN .*PRIVATE KEY-----|\b(?:ghp|github_pat)_[\w]{20,}", text):
        raise ValueError("文字疑似包含秘密，停止匯入")
    source_hash = hashlib.sha256(data).hexdigest()
    key = "doc_" + source_hash
    path = task_path(repo, key)
    target = _plain(repo / "outputs" / "imported-text" / f"{key}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ValueError("來源有版本差異，拒絕覆寫")
    if not target.exists():
        with target.open("xb") as stream:
            stream.write(data)
    if digest(source) != source_hash or _file_id(source) != original_file_id:
        raise ValueError("匯入期間原件已改變，快照保留但未標成完成")
    task = read(repo, key) if path.exists() else {
        "schema": 1, "id": key, "document_id": key, "source_id": key, "platform": "document",
        "source_url": source_url, "canonical_url": source_url,
        "content_type": "user_imported_markdown" if _intake == "markdown" else "user_import",
        "method": "user_text_import", "source_path": str(target), "source_sha256": source_hash,
        "original_path": str(source), "source_filename": source.name,
        "stage": "acquired", "created_at": now(), "originals": []}
    if task.get("source_sha256") != source_hash or digest(Path(task["source_path"])) != source_hash:
        raise ValueError("同文件 ID 快照有版本差異，拒絕覆寫")
    origins = task.setdefault("originals", [])
    matching = next((entry for entry in reversed(origins) if entry["original_path"] == str(source)
                     and entry["source_sha256"] == source_hash and entry.get("archive_state") not in {"archived", "replaced"}
                     and entry.get("original_file_id") == original_file_id), None)
    changed = not path.exists()
    if matching is None:
        for entry in origins:
            if (entry["original_path"] == str(source) and entry.get("archive_state") not in {"archived", "replaced"}
                    and entry.get("original_file_id") != original_file_id):
                entry.update(archive_state="replaced", replaced_at=now())
        origins.append({"origin_id": uuid.uuid4().hex, "original_path": str(source),
                        "source_filename": source.name, "source_sha256": source_hash,
                        "original_file_id": original_file_id, "intake": _intake, "archive_state": "inbox"})
        changed = True
    elif _intake == "markdown" and matching["intake"] != "markdown":
        matching["intake"] = "markdown"
        changed = True
    if task.get("card_path") and (not (repo / task["card_path"]).is_file()
                                  or digest(repo / task["card_path"]) != task.get("card_sha256")):
        task.update(stage="needs_review", next_step="review_changed_card")
        changed = True
    return save(repo, task) if changed else task


def _scan_path(repo: Path, scan_id: str) -> Path:
    if not isinstance(scan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", scan_id):
        raise ValueError("不合法的 Markdown scan ID")
    return _plain(repo / "state" / "markdown-scans" / f"{scan_id}.json")


def scan_markdown(repo: Path) -> dict:
    """Snapshot only inbox-top .md files; immutable scan tokens never move files."""
    repo = _plain(repo)
    folder = _plain(repo / "markdown")
    result = {"scan_id": None, "tasks": [], "skipped": [], "failed": []}
    if not folder.exists():
        return result
    if not folder.is_dir():
        raise ValueError("markdown 必須是一般資料夾")
    paths = sorted((p for p in folder.iterdir() if p.suffix.lower() == ".md"), key=lambda p: p.name)
    if not paths:
        return result
    scan_id = uuid.uuid4().hex
    entries, created = [], []
    for source in paths:
        try:
            _plain(source)
            if not source.is_file():
                raise ValueError("收件 .md 必須是一般檔案")
            source_hash = digest(source)
            previous_tasks = tasks(repo)
            task = import_text(repo, source, _intake="markdown")
            if task["source_sha256"] != source_hash or task["id"] != "doc_" + task["source_sha256"]:
                raise ValueError("掃描期間原件已改變；快照保留，請重新掃描取得一致 token")
            # The imported snapshot is the only authority for token identity.
            key, source_hash = task["id"], task["source_sha256"]
            existed = any(value["id"] == key for value in previous_tasks)
            # A changed original is a new document, never a completed old task.
            for previous in previous_tasks:
                if any(origin["original_path"] == str(source) and origin["source_sha256"] != source_hash
                       and origin.get("archive_state") not in {"archived", "replaced"} for origin in previous.get("originals", [])):
                    if previous.get("stage") != "needs_review":
                        previous["stage_before_source_change"] = previous.get("stage")
                    previous.update(stage="needs_review", next_step="review_changed_original")
                    save(repo, previous)
            origin = next(entry for entry in reversed(task["originals"])
                          if entry["original_path"] == str(source) and entry.get("archive_state") not in {"archived", "replaced"})
            entries.append({"id": key, "origin_id": origin["origin_id"], "original_path": str(source),
                            "source_filename": source.name, "source_sha256": source_hash,
                            "original_file_id": origin["original_file_id"]})
            if existed or key in created:
                result["skipped"].append({"id": key, "stage": task["stage"], "reason": "same_source_hash",
                                           "original_path": str(source)})
            else:
                created.append(key)
        except (OSError, ValueError) as error:
            result["failed"].append({"original_path": str(source), "reason": str(error)})
    if entries:
        path = _scan_path(repo, scan_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"schema": 1, "scan_id": scan_id, "created_at": now(), "entries": entries}, stream, ensure_ascii=False, indent=2)
        result["scan_id"] = scan_id
    result["tasks"] = [read(repo, key) for key in created]
    return result


def archive_markdown(repo: Path, keys: list[str], *, scan_id: str) -> dict:
    """Archive only explicit, validated origins from an exact earlier scan token.

    Pending state is saved before a no-overwrite copy is made under a Windows
    source handle lock. Deletion targets that verified handle, never a pathname.
    A crash before/after deletion can resume; other platforms fail closed.
    """
    repo = _plain(repo)
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("歸檔必須明確指定不重複的任務 ID")
    manifest = json.loads(_scan_path(repo, scan_id).read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("scan_id") != scan_id:
        raise ValueError("Markdown 掃描收據不符")
    folder, processed = _plain(repo / "markdown"), _plain(repo / "markdown" / "processed")
    planned, skipped = [], []
    for key in keys:
        task = read(repo, key)
        if task.get("platform") != "document" or task.get("stage") not in {"validated", "branch_pushed", "main_pushed", "published"}:
            raise ValueError("只歸檔此次已精煉並驗證的 Markdown 任務")
        _document_source(task)
        if digest(repo / task["card_path"]) != task["card_sha256"]:
            raise ValueError("已驗證知識卡已改變，不歸檔原件")
        entries = [entry for entry in manifest["entries"] if entry["id"] == key]
        if not entries:
            raise ValueError("任務不在此次 scan 清單；外部 import-text 不自動歸檔")
        for entry in entries:
            origin = next((value for value in task.get("originals", []) if value["origin_id"] == entry["origin_id"]), None)
            if (not origin or origin["intake"] != "markdown" or entry["source_sha256"] != task["source_sha256"]
                    or origin["source_sha256"] != entry["source_sha256"]
                    or origin["original_path"] != entry["original_path"]
                    or origin["original_file_id"] != entry["original_file_id"]):
                raise ValueError("原件追溯與 scan 清單不符")
            if origin.get("archive_state") == "archived":
                archived = _plain(Path(origin["archive_path"]))
                if archived.parent != processed or digest(archived) != task["source_sha256"]:
                    raise ValueError("既有歸檔內容已改變或遺失，停止重試")
                skipped.append({"id": key, "origin_id": origin["origin_id"], "reason": "already_archived"})
                continue
            if origin.get("archive_state") == "replaced":
                raise ValueError("此 scan 的原件已被替換，請使用新 scan token")
            source = _plain(Path(entry["original_path"]))
            if source.parent != folder or source.name != entry["source_filename"] or source.suffix.lower() != ".md":
                raise ValueError("只允許歸檔 markdown 頂層原件")
            destination = _plain(processed / source.name)
            if destination.exists() and digest(destination) != task["source_sha256"]:
                raise ValueError("processed 同名檔案有版本差異，拒絕覆寫")
            if source.exists():
                if digest(source) != task["source_sha256"] or _file_id(source) != origin["original_file_id"]:
                    raise ValueError("Markdown 原件已改變，不歸檔")
            elif origin.get("archive_state") != "pending" or not destination.is_file():
                raise ValueError("Markdown 原件遺失，停止歸檔")
            planned.append((task, origin, source, destination))
    # Validate the whole explicit set before the first file move.
    processed.mkdir(parents=True, exist_ok=True)
    archived = []
    for task, origin, source, destination in planned:
        origin.update(archive_state="pending", archive_path=str(destination), archive_scan_id=scan_id)
        save(repo, task)
        if source.exists():
            _archive_locked(source, destination, task["source_sha256"], origin["original_file_id"])
        if digest(destination) != task["source_sha256"]:
            raise ValueError("歸檔內容 hash 不符，保留快照並停止")
        origin.update(archive_state="archived", archived_at=now(), archive_sha256=task["source_sha256"])
        task["archive_path"] = str(destination)
        task["archive_state"] = "partial" if any(value["intake"] == "markdown"
            and value.get("archive_state") not in {"archived", "replaced"} for value in task["originals"]) else "archived"
        save(repo, task)
        archived.append({"id": task["id"], "original_path": str(source), "archive_path": str(destination),
                         "source_sha256": task["source_sha256"]})
    return {"scan_id": scan_id, "archived": archived, "skipped": skipped}


def mark_refined(repo: Path, key: str, card_path: Path) -> dict:
    from kb_knowledge import parse_card
    task = read(repo, key)
    _document_source(task)
    # Check the supplied path before resolving away a symlink/junction.
    digest(card_path)
    card = card_path.resolve()
    relative = card.relative_to((repo / "knowledge").resolve())
    if card.is_symlink() or len(relative.parts) != 2:
        raise ValueError("知識卡必須位於既有 knowledge 分類目錄")
    meta = parse_card(card)["metadata"]
    field = "document_id" if task["platform"] == "document" else "video_id"
    if str(meta.get(field)) != key or meta.get("platform") != task["platform"]:
        raise ValueError("卡片來源識別不符")
    if task["platform"] == "document":
        if relative.parts[0] != "cards":
            raise ValueError("新文件知識卡必須位於 knowledge/cards")
        if (meta.get("source_sha256") != task["source_sha256"]
                or meta.get("source_document") != "local:" + task["source_filename"]):
            raise ValueError("文件來源 hash／原始檔名未綁定匯入紀錄")
    if meta.get("content_type") != task["content_type"]:
        raise ValueError("卡片內容類型與取得紀錄不符")
    if task["platform"] not in {"local", "document"} and meta.get("source_url") not in {task["source_url"], task["canonical_url"]}:
        raise ValueError("卡片來源 URL 未綁定已取得的來源；請使用收據原網址或標準網址")
    # Explicit Agent-authored transition: never generate a card automatically.
    relative_path, card_hash = "knowledge/" + relative.as_posix(), digest(card)
    unchanged = task.get("card_path") == relative_path and task.get("card_sha256") == card_hash
    stage = task["stage"] if unchanged and task["stage"] in {"validated", "branch_pushed", "main_pushed", "published"} else "refined"
    task.update({"card_path": relative_path, "card_sha256": card_hash, "stage": stage})
    task.pop("error", None)
    return save(repo, task)


def validate_task(repo: Path, key: str) -> dict:
    from kb_knowledge import validate_knowledge
    task = read(repo, key)
    if not task.get("card_path"):
        raise ValueError("尚未由 Agent 建立並標記精煉卡片")
    _document_source(task)
    if digest(repo / task["card_path"]) != task["card_sha256"]:
        raise ValueError("來源或卡片已改變，請先審閱並重新標記精煉")
    validate_knowledge(repo, strict_paths=[task["card_path"]])
    stage = task["stage"] if task["stage"] in {"branch_pushed", "main_pushed", "published"} else "validated"
    task.update({"stage": stage, "validated_at": now(), "index_sha256": digest(repo / "knowledge" / "README.md")})
    return save(repo, task)


def publication_paths(repo: Path, keys: list[str] | None = None) -> list[str]:
    selected = [read(repo, key) for key in keys] if keys else [task for task in tasks(repo) if task.get("stage") == "validated"]
    paths = []
    for task in selected:
        if task.get("stage") != "validated":
            raise ValueError("只發布已驗證任務；發布重試請使用原 receipt")
        _document_source(task)
        if digest(repo / task["card_path"]) != task["card_sha256"]:
            raise ValueError("已驗證來源或卡片有變動")
        if digest(repo / "knowledge" / "README.md") != task["index_sha256"]:
            raise ValueError("索引已有變動，請重新驗證任務")
        paths.append(task["card_path"])
    return sorted(set(paths + (["knowledge/README.md"] if paths else [])))
