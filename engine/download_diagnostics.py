"""Local, payload-free download stage diagnostics. No URLs or headers are stored."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
from subprocess import TimeoutExpired
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

STAGES = ("share_resolution", "page_load", "metadata", "media_url", "media_download",
          "audio_processing", "transcription")
_active = ContextVar("download_trace", default=None)
_fields = {"http_status", "body_type", "api_type", "argus_kind", "api_status",
           "byte_count", "candidate_count", "error_type", "method", "reason_code", "browser_closed",
           "source_kind", "url_count"}
_probe_counts = {"audio_resources", "active_players", "ready_players", "player_index",
                 "response_excluded_frame", "response_excluded_target", "response_excluded_path",
                 "response_excluded_mime", "response_excluded_status"}
_probe_booleans = {"paused", "muted"}
_probe_src_kinds = {"none", "blob", "allowlisted_direct", "disallowed_other"}


def safe_reason(value):
    def clean_url(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.hostname or ""
            public_page = any(host == suffix or host.endswith("." + suffix) for suffix in ("douyin.com", "iesdouyin.com"))
            return urlunsplit((parsed.scheme, host, parsed.path if public_page else "/[REDACTED]", "", ""))
        except ValueError:
            return "[URL REDACTED]"
    text = re.sub(r'https?://[^\s<>"\']+', clean_url, str(value))
    text = re.sub(r"(?im)(cookie|set-cookie|authorization)\s*[:=][^\r\n]*", r"\1=[REDACTED]", text)
    return re.sub(r"(?i)((?:[\w-]*token|[\w-]*signature|api[_-]?key|password)\s*[:=]\s*)[^\s,;\"']+",
                  r"\1[REDACTED]", text)


class SafeYtdlpLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        print(safe_reason(message), file=sys.stderr)

    error = warning


def response_kind(raw):
    prefix = raw.lstrip()
    if not prefix:
        return "empty"
    try:
        json.loads(raw)
        return "json"
    except (ValueError, UnicodeError):
        return "html" if prefix.startswith(b"<") else "text"


def _error_chain(error):
    pending, seen, errors = [error], set(), []
    while pending and len(errors) < 16:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        pending.extend((current.__cause__, current.__context__, getattr(current, "reason", None)))
        wrapped = getattr(current, "exc_info", None)
        if isinstance(wrapped, tuple) and len(wrapped) > 1:
            pending.append(wrapped[1])
    return errors


def category(error):
    non_retry_category = None
    for current in _error_chain(error):
        current_category = getattr(current, "category", "UNKNOWN")
        if current_category in {"BLOCKED_PLATFORM", "INVALID_TARGET", "PROTECTED_MEDIA"}:
            return current_category
        if current_category == "MEDIA_LIMIT":
            non_retry_category = current_category
        if isinstance(current, HTTPError) and current.code in (401, 403, 412, 429, 451) or re.search(
                r"(?i)\bHTTP(?: Error)?[ :]+(?:401|403|412|429|451)\b|sign in|log in|login.required|"
                r"captcha|not a bot|private video|\bdrm\b|not available in your country|geo.restricted|"
                r"cookies (?:are )?(?:needed|required)|ArgusSecurityPlugin|signature_invalid|"
                r"請完成驗證|请完成验证|需登[入录]|登[入录]後觀看|登录后观看|私人影片|地區限制", str(current)):
            return "BLOCKED_PLATFORM"
    return non_retry_category or getattr(error, "category", "UNKNOWN")


def _timeout_reason(error):
    for current in _error_chain(error):
        if isinstance(current, (ConnectionResetError, ConnectionAbortedError)):
            return "network_interrupted"
        diagnostic = getattr(current, "diagnostics", None)
        if isinstance(diagnostic, dict):
            if diagnostic.get("error_type") == "ProbeTimeout" and diagnostic.get("failure_phase") in ("observation", "media_download"):
                return "observation_timeout"
            if diagnostic.get("error_type") == "TimeoutError" and diagnostic.get("failure_phase") in ("navigation", "observation"):
                return "network_timeout"
        if isinstance(current, (TimeoutError, TimeoutExpired)) or re.search(
                r"(?i)\b(?:observation_deadline_exceeded|ProbeTimeout|ERR_TIMED_OUT)\b|"
                r"\b(?:network|connection|read|socket|request) (?:timed out|timeout)\b", str(current)):
            return "network_timeout"
    return None


def local_failure(error):
    for current in _error_chain(error):
        if isinstance(current, (ImportError, AssertionError)):
            return True
        if isinstance(current, OSError) and not isinstance(current, (
                HTTPError, URLError, TimeoutError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if getattr(current, "category", "") in {"BROWSER_UNAVAILABLE", "LOCAL_DEPENDENCY_ERROR"}:
            return True
        if getattr(current, "category", "") == "LOCAL_BROWSER_ERROR" and not _timeout_reason(current):
            return True
        if re.search(r"(?i)no module named|(?:ffmpeg|executable|runtime|model).*(?:missing|not found|unavailable)|"
                     r"no space left|disk (?:is )?full|permission denied", str(current)):
            return True
    return False


def retry_reason(error):
    """Only fixed, recoverable source failures receive one outer retry."""
    if category(error) in {"BLOCKED_PLATFORM", "INVALID_TARGET", "PROTECTED_MEDIA", "MEDIA_LIMIT"} or local_failure(error):
        return None
    timeout = _timeout_reason(error)
    if timeout:
        return timeout
    for current in _error_chain(error):
        status = current.code if isinstance(current, HTTPError) else None
        match = re.search(r"(?i)\bHTTP(?: Error)?[ :]+(\d{3})\b", str(current))
        status = status if status is not None else int(match[1]) if match else None
        if status == 408:
            return "http_408"
        if status is not None and 500 <= status <= 599:
            return "http_5xx"
        reason = str(current)
        if re.search(r"\b(?:complete_)?original_audio_not_observed\b", reason):
            return "original_audio_missing"
        if re.search(r"\bready_player_not_observed\b", reason):
            return "player_not_ready"
    return None


def event(stage, status, elapsed_ms=0, **fields):
    current = _active.get()
    if current is None:
        return
    item = {"stage": stage, "status": status, "elapsed_ms": round(elapsed_ms, 2)}
    for key, value in fields.items():
        if key in {"attempt", "max_attempts"}:
            if type(value) is int and 1 <= value <= 2:
                item[key] = value
        elif key in _probe_counts:
            if type(value) is int and value >= 0:
                item[key] = value
        elif key in _probe_booleans:
            if type(value) is bool:
                item[key] = value
        elif key == "current_src_kind":
            if isinstance(value, str) and value in _probe_src_kinds:
                item[key] = value
        elif key == "duration_seconds":
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                item[key] = value
        elif key in _fields and (isinstance(value, (int, float, bool)) or value is None
                               or isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)):
            item[key] = value
    current.data["events"].append(item)
    if stage in STAGES:
        current.data["stages"][stage] = status


def set_video_id(value):
    current = _active.get()
    if current is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", str(value or "")):
        current.data["video_id"] = str(value)


@contextmanager
def stage(name, **fields):
    started = perf_counter()
    try:
        yield fields
    except Exception as error:
        if isinstance(error, HTTPError):
            fields["http_status"] = error.code
        event(name, category(error) if category(error) == "BLOCKED_PLATFORM" else "failed",
              (perf_counter() - started) * 1000, error_type=type(error).__name__, **fields)
        raise
    else:
        event(name, "success", (perf_counter() - started) * 1000, **fields)


class Trace:
    def __init__(self, path, video_id=""):
        self.path = Path(path)
        self.data = {"schema": 1, "run_id": uuid4().hex, "time": datetime.now(timezone.utc).isoformat(),
                     "video_id": video_id, "status": "failed", "category": "UNKNOWN",
                     "stages": dict.fromkeys(STAGES, "not_attempted"), "events": []}

    def __enter__(self):
        self.started = perf_counter()
        self.token = _active.set(self)
        return self

    def __exit__(self, kind, error, traceback):
        if error is not None:
            self.data["category"] = category(error)
            if self.data["category"] == "BLOCKED_PLATFORM":
                self.data["status"] = "BLOCKED_PLATFORM"
            self.data["error_type"] = type(error).__name__
        else:
            self.data["category"] = "NONE"
        self.data["elapsed_ms"] = round((perf_counter() - self.started) * 1000, 2)
        _active.reset(self.token)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.data, ensure_ascii=False) + "\n")
        except OSError:
            print("無法寫入本機下載診斷；逐字稿處理結果不變。", file=sys.stderr)
