"""One-call local URL intake. No inference, publication, outer retries or new downloader.

Settings: max_batch=20; engine={model_path, vendor_dir, python,
 timeout_seconds=7200, env={explicit runtime overrides}}. Direct links have no
600-second cap. Topic discovery and its 3-video/600-second limits belong to Agent.
Returns scan_id, task IDs, unfinished source IDs, safe failures and skips.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import knowledge_base as kb
import kb_workflow as flow

ROOT = Path(__file__).resolve().parent
_ENV = {"AKB_NODE", "AKB_PLAYWRIGHT_MODULE", "AKB_BROWSER_EXECUTABLE",
        "AKB_BROWSER_CHANNEL", "AKB_FFMPEG", "AKB_MODEL_PATH", "PATH", "PYTHONPATH"}
_DONE = {"acquired", "refined", "validated", "branch_pushed", "main_pushed", "published", "needs_review"}


class IntakeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise IntakeError(code)


def _settings(settings):
    if not isinstance(settings, dict):
        _fail("invalid_settings")
    limit = settings.get("max_batch", 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        _fail("invalid_batch_limit")
    engine = settings.get("engine", {})
    allowed = {"model_path", "vendor_dir", "python", "timeout_seconds", "env"}
    if not isinstance(engine, dict) or set(engine) - allowed:
        _fail("invalid_engine_settings")
    timeout = engine.get("timeout_seconds", 7200)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 86400:
        _fail("invalid_engine_timeout")
    env = engine.get("env", {})
    if not isinstance(env, dict) or set(env) - _ENV or any(
            not isinstance(value, str) or "\x00" in value for value in env.values()):
        _fail("invalid_engine_environment")
    for name in ("model_path", "vendor_dir", "python"):
        if name in engine and (not isinstance(engine[name], (str, Path)) or not str(engine[name])
                               or "\x00" in str(engine[name])):
            _fail("invalid_engine_path")
    return limit, engine, timeout


def _urls(texts, tool):
    if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        _fail("invalid_inputs")
    for index, text in enumerate(texts):
        # Sharing apps sometimes escape ':' or '/'. Only decode these URL marks.
        normalized = re.sub(r"(https?)\\+:", r"\1:", text).replace("\\/", "/")
        values = list(tool.URL_PATTERN.finditer(normalized))
        if not values:
            yield index, None
        for match in values:
            yield index, match.group(0).rstrip(".,;:!?，。；：！？、)]}）】>」』")


def _receipt(workspace, key, source, tool):
    path = flow._plain(workspace / "state" / "acquisitions" / (key + ".json"))
    try:
        if path.stat().st_size > 65536:
            _fail("oversized_acquisition_receipt")
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail("missing_or_invalid_acquisition_receipt")
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("video_id") != key:
        _fail("invalid_acquisition_identity")
    if any(not isinstance(record.get(name), str) for name in ("canonical_url", "source_url", "platform",
                                                               "content_type", "method", "duration_evidence", "transcript_sha256")):
        _fail("invalid_acquisition_schema")
    actual_key, platform, canonical = flow.identity(record["canonical_url"], tool)
    if (actual_key != key or record.get("platform") != platform
            or record.get("source_url") != canonical or record.get("canonical_url") != canonical
            or record.get("content_type") != "whisper_asr"
            or record.get("method") not in {"yt-dlp", "visible-browser", "share-page", "playwright"}
            or record.get("duration_evidence") not in {"player_metadata", "extractor_metadata", "public_video_metadata"}):
        _fail("invalid_acquisition_identity")
    duration = record.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        _fail("invalid_acquisition_duration")
    info = flow.source_metadata(source, tool)
    if (info["id"] != key or info["platform"] != platform
            or record.get("transcript_sha256") != flow.digest(source)):
        _fail("acquisition_hash_or_source_mismatch")
    return record


def _register(workspace, key, canonical, sync, tool):
    lookup = flow.read(workspace, key) if flow.task_path(workspace, key).exists() else {}
    source = flow.available_source(workspace, lookup.get("resolved_id") or key,
                                   lookup.get("resolved_canonical_url") or canonical, tool)
    if source is None:
        _fail("engine_output_missing")
    actual = flow.source_metadata(source, tool)["id"]
    record = _receipt(workspace, actual, source, tool)
    if not key.startswith("pending_") and actual != key:
        _fail("acquisition_source_mismatch")
    if key.startswith("pending_"):
        # Persist the resolution before import so a failed import never re-fetches.
        alias = flow.read(workspace, key) if flow.task_path(workspace, key).exists() else {"id": key, "schema": 1}
        alias.update(resolved_id=actual, canonical_url=canonical,
                     resolved_canonical_url=record["canonical_url"], stage="resolved")
        flow.save(workspace, alias)
    # Import exactly this verified source. Other unfinished outputs are unaffected.
    folder = flow._plain(workspace / "state" / "intake" / "imports" / actual / record["transcript_sha256"])
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = flow._plain(folder / "source.md")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != record["transcript_sha256"]:
        _fail("acquisition_hash_or_source_mismatch")
    if snapshot.exists():
        if snapshot.read_bytes() != data:
            _fail("intake_snapshot_conflict")
    else:
        with snapshot.open("xb") as stream:
            stream.write(data)
    # The high-level CLI owns its JSON response; suppress legacy importer chatter.
    with redirect_stdout(io.StringIO()):
        kb.import_outputs(folder, workspace, sync, tool)
    imported = flow.available_source(workspace, actual, record["canonical_url"], tool)
    if imported is None:
        _fail("imported_source_missing")
    task = flow.register_source(workspace, imported, tool,
                               duration_seconds=record["duration_seconds"],
                               max_seconds=math.ceil(record["duration_seconds"]),
                               discovery={"found_via": "direct_user_intake", "selected_url": canonical})
    task["canonical_url"] = record["canonical_url"]
    return flow.save(workspace, task)


def _existing(workspace, key, canonical, tool):
    path = flow.task_path(workspace, key)
    if not path.exists():
        return None
    task = flow.read(workspace, key)
    if task.get("resolved_id"):
        actual = task["resolved_id"]
        if not isinstance(actual, str) or actual == key or actual.startswith("pending_"):
            _fail("invalid_short_link_mapping")
        if flow.task_path(workspace, actual).exists():
            return _existing(workspace, actual, task.get("resolved_canonical_url", ""), tool)
        return None
    if task.get("stage") in _DONE:
        if task.get("source_path"):
            if flow.available_source(workspace, key, canonical, tool) is None:
                _fail("existing_source_unavailable")
        elif task.get("stage") != "published":
            _fail("existing_source_unavailable")
        return task
    return None


def _run_engine(workspace, canonical, settings, timeout):
    command = [str(settings.get("python", sys.executable)), "-B", str(ROOT / "kb_engine.py"),
               str(ROOT / "engine"), "--workspace", str(workspace), "--no-sync", "--url", canonical]
    for name in ("model_path", "vendor_dir"):
        if settings.get(name):
            command.extend(["--" + name.replace("_", "-"), str(settings[name])])
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(settings.get("env", {}))
    result = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        _fail("engine_failed")


def intake(workspace: Path, texts: list[str], settings: dict) -> dict:
    workspace = flow._plain(Path(workspace))
    scan = flow.scan_markdown(workspace)
    result = {"scan_id": scan["scan_id"], "tasks": [], "pending": [], "failed": [], "skipped": []}
    for failure in scan.get("failed", []):
        result["failed"].append({"error_type": "MarkdownScanError", "reason": "markdown_scan_failed"})
    limit, engine_settings, timeout = _settings(settings)
    sync, tool = kb.engine(ROOT / "engine")
    seen, processed = set(), 0
    for index, value in _urls(texts, tool):
        key = None
        try:
            if value is None:
                _fail("no_supported_url")
            key, platform, canonical = flow.identity(value, tool)
            # Follow locally recorded short-link resolutions before deduplication.
            alias_path = flow.task_path(workspace, key)
            effective = key
            if alias_path.exists():
                alias = flow.read(workspace, key)
                effective = alias.get("resolved_id") or key
                if (not isinstance(effective, str) or effective.startswith("pending_") and effective != key
                        or effective != key and flow.identity(alias.get("resolved_canonical_url", ""), tool)[0] != effective):
                    _fail("invalid_short_link_mapping")
            if effective in seen:
                result["skipped"].append({"id": effective, "reason": "same_batch_duplicate"})
                continue
            seen.add(effective)
            if processed >= limit:
                result["skipped"].append({"id": effective, "reason": "batch_limit"})
                continue
            processed += 1
            task = _existing(workspace, key, canonical, tool)
            if task:
                result["tasks"].append(task["id"])
                result["skipped"].append({"id": task["id"], "reason": "resume_existing_task"})
                continue
            if flow.existing_card(workspace, effective, canonical):
                result["skipped"].append({"id": effective, "reason": "existing_knowledge"})
                continue
            source = flow.available_source(workspace, effective,
                                           alias.get("resolved_canonical_url", canonical) if alias_path.exists() else canonical, tool)
            if source is None and effective != key:
                _fail("existing_source_unavailable")
            if source is None:
                task = {"schema": 1, "id": key, "platform": platform, "canonical_url": canonical,
                        "source_url": canonical, "stage": "pending", "created_at": flow.now()}
                flow.save(workspace, task)
                _run_engine(workspace, canonical, engine_settings, timeout)
            task = _register(workspace, key, canonical, sync, tool)
            seen.add(task["id"])
            result["tasks"].append(task["id"])
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            reason = error.code if isinstance(error, IntakeError) else "intake_validation_failed"
            if isinstance(error, subprocess.TimeoutExpired):
                reason = "engine_timeout"
            failure = {"input_index": index, "error_type": type(error).__name__, "reason": reason}
            if key is None:
                key = "input_" + hashlib.sha256((value or f"no_url:{index}").encode()).hexdigest()[:24]
            if key:
                failure["id"] = key
                path = flow.task_path(workspace, key)
                task = flow.read(workspace, key) if path.exists() else {"id": key, "schema": 1}
                if task.get("stage") not in _DONE and not task.get("resolved_id"):
                    task.update(stage="failed", error={"error_type": type(error).__name__, "reason": reason})
                    flow.save(workspace, task)
                else:
                    task["last_intake_error"] = {"error_type": type(error).__name__, "reason": reason}
                    flow.save(workspace, task)
            result["failed"].append(failure)
    # Continue prior unfinished sources even when this call supplies no URLs.
    for task in flow.tasks(workspace):
        if task.get("source_path") and task.get("stage") not in {"published", "skipped"}:
            result["pending"].append(task["id"])
    result["tasks"] = list(dict.fromkeys([task["id"] for task in scan["tasks"]] + result["tasks"] + result["pending"]))
    return result
