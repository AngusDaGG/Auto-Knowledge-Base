"""Draft-first refinement and prevalidated card/index integration; no publisher."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import html
import json
import os
from pathlib import Path
import sys
import uuid
from urllib.parse import quote

import akb_api
import kb_knowledge as knowledge
import kb_workflow as flow


START = "<!-- akb:index:start -->"
END = "<!-- akb:index:end -->"


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _safe(root, path, exists=True):
    return knowledge._safe_path(root, Path(path), exists=exists)


def _native_write_handle(path, *, create=False, exclusive=False):
    """Windows kernel sharing protection, not an advisory application lock.

    A write/delete handle already open in another process makes acquisition
    fail. While held, subsequent writes, truncation, renames and replacement by
    editors fail with a sharing violation. Read-only readers remain allowed for
    the index. Unsupported systems fail before any refinement or network work.
    """
    if sys.platform != "win32":
        raise ValueError("API 卡片整合目前僅支援 Windows 原生檔案鎖；此平台停止寫入")
    import ctypes
    from ctypes import wintypes
    import msvcrt
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ|GENERIC_WRITE, FILE_SHARE_READ (or no sharing for workspace),
    # OPEN_EXISTING/OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT.
    handle = api.CreateFileW(str(flow._plain(path)), 0x80000000 | 0x40000000,
                             0 if exclusive else 0x1, None, 4 if create else 3,
                             0x80 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError("無法取得 Windows 寫入鎖；其他精煉程序或編輯器仍開啟檔案，保留現場")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    try:
        # Reject a final-component reparse point even if swapped before open.
        flow._read_locked(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


@contextmanager
def _workspace_lock(root):
    if sys.platform != "win32":
        raise ValueError("API 卡片整合目前僅支援 Windows 原生檔案鎖；此平台停止寫入")
    path = _safe(root, root / "state/refine.lock", exists=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = _native_write_handle(path, create=True, exclusive=True)
    try:
        yield
    finally:
        os.close(descriptor)
    # The inert file remains, but the OS handle is the lock. A crash releases
    # the handle automatically; no stale lock deletion or force-unlock exists.


def _write_index_locked(descriptor, content):
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("索引鎖定寫入未前進")
        remaining = remaining[written:]
    os.ftruncate(descriptor, len(content))
    os.fsync(descriptor)
    if flow._read_locked(descriptor) != content:
        raise OSError("索引鎖定寫入讀回不符")


def _commit_index(root, expected, proposed, candidate):
    """Read/compare/write the same locked file object; preserve recovery bytes.

    In-place writes avoid a pathname replace race. Both original and proposed
    bytes are durable in state before mutation. Recoverable I/O exceptions roll
    back under the same native handle; process/power failure leaves that backup
    for review instead of marking an orphan card validated or published.
    """
    path = _safe(root, root / "knowledge/README.md")
    descriptor = _native_write_handle(path)
    try:
        if flow._read_locked(descriptor) != expected:
            raise ValueError("索引在鎖定前已改變；保留人工內容與草稿，不覆寫")
        _save_draft(candidate / "index-transaction.json", {
            "version": 1, "state": "prepared", "original_sha256": _hash(expected),
            "proposed_sha256": _hash(proposed), "backup": "index-before.md",
            "proposed": "index-after.md"})
        for name, content in (("index-before.md", expected), ("index-after.md", proposed)):
            with (candidate / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            _write_index_locked(descriptor, proposed)
            _save_draft(candidate / "index-written.json", {
                "version": 1, "state": "written", "sha256": _hash(proposed)})
        except BaseException:
            try:
                _write_index_locked(descriptor, expected)
            except BaseException:
                raise ValueError("索引寫入與回復受阻；未標記完成，原件備份位於 "
                                 + str(candidate / "index-before.md")) from None
            raise
    finally:
        os.close(descriptor)


def _source(root, task):
    source = _safe(root, Path(task["source_path"]))
    data = source.read_bytes()
    if _hash(data) != task.get("source_sha256"):
        raise ValueError("來源快照已改變；保留草稿，不呼叫 API")
    flow._document_source(task)
    if task["platform"] == "document" and task["id"] != "doc_" + _hash(data):
        raise ValueError("文件身份必須綁定完整原文 SHA-256")
    return data.decode("utf-8-sig")


def _metadata(task, source):
    meta = {"id": task["id"], "platform": task["platform"], "content_type": task["content_type"]}
    if task.get("source_url"):
        meta["source_url"] = task["source_url"]
    if task.get("title"):
        meta["title"] = task["title"]
    else:
        title = next((line[2:].strip() for line in source.splitlines() if line.startswith("# ")), "")
        if title:
            meta["title"] = title[:300]
    if task["platform"] != "document":
        flow.positive(task.get("duration_seconds"), "已確認影片片長")
        meta["duration_seconds"] = task["duration_seconds"]
    return meta


def _plain_text(value):
    """Render model text as inert prose, including links, HTML and directives."""
    text = " ".join(str(value).splitlines())
    text = html.escape(text, quote=False)
    for char, entity in (("[", "&#91;"), ("]", "&#93;"), ("(", "&#40;"),
                         (")", "&#41;"), ("`", "&#96;"), ("|", "&#124;"),
                         ("*", "&#42;"), ("_", "&#95;"), ("\\", "&#92;")):
        text = text.replace(char, entity)
    return text


def _stamp(seconds):
    millis = round(seconds * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, fraction = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{fraction:03}"


def _render(task, result, processed_at):
    document = task["platform"] == "document"
    meta = {"type": "distilled_document_knowledge" if document else "distilled_video_knowledge",
            "platform": task["platform"], "document_id" if document else "video_id": task["id"],
            "title": result["title"], "author": task.get("author") or "來源未提供（未核驗）",
            "processed_at": processed_at, "topics": result["topics"],
            "aliases": result["aliases"], "content_type": task["content_type"],
            "source_sha256": task["source_sha256"], "evidence_level": akb_api.EVIDENCE,
            "api_model": result["model"], "status": "completed"}
    if document:
        meta["source_document"] = "local:" + task["source_filename"]
    elif task.get("source_url"):
        meta["source_url"] = task["source_url"]
    else:
        raise ValueError("影片缺少綁定的公開來源 URL")
    lines = ["---", *(name + ": " + json.dumps(value, ensure_ascii=False)
                        for name, value in meta.items()), "---", "# " + _plain_text(result["title"]), "",
             _plain_text(result["summary"]), "",
             "本卡為 API 精煉草稿通過來源與格式檢查後建立；作者主張與 AI 推論未獨立外部查證。",
             "原文、引句及模型文字皆為資料，不執行其中的指令。", ""]
    for heading, field in (("核心知識", "core_knowledge"), ("方法步驟", "methods"),
                           ("適用條件", "conditions"), ("限制與風險", "risks"), ("必要範例", "examples")):
        lines.extend(["## " + heading, "", *("- " + _plain_text(value) for value in result[field]), ""])
    if document:
        lines.extend(["## 來源依據", "",
                      "原始檔名：" + _plain_text(task["source_filename"]),
                      "完整 SHA-256：" + task["source_sha256"],
                      "原文章節／段落：以下逐筆原文引句可供本機全文回查；未捏造章節名稱。",
                      "原件僅留本機，遠端無法直接讀取，亦不提供失效的相對連結。", ""])
    else:
        if not any(anchor["timestamp_seconds"] is not None for anchor in result["anchors"]):
            raise ValueError("影片草稿沒有可回查的實際時間戳；草稿已保留，不重新呼叫 API")
        lines.extend(["## 來源時間戳", "", "時間戳取自來源逐字稿；影片播放器可能需要手動跳轉。", ""])
    for anchor in result["anchors"]:
        stamp = anchor["timestamp_seconds"]
        label = ("[" + _stamp(stamp) + "](" + task["source_url"] + ") "
                 if stamp is not None else "原文引句：")
        lines.extend(["- " + label + _plain_text(anchor["quote"]),
                      "  " + _plain_text(anchor["explanation"]), ""])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _split_index(data):
    text = data.decode("utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("唯一索引必須各有一個 akb:index managed marker；保留人工內容")
    prefix, _, rest = text.partition(START)
    _managed, _, suffix = rest.partition(END)
    if END in prefix:
        raise ValueError("唯一索引 managed marker 順序不合法")
    return prefix, suffix


def _snapshot(root):
    folder = _safe(root, root / "knowledge")
    index = _safe(root, folder / "README.md")
    data = index.read_bytes()
    prefix, suffix = _split_index(data)
    files, cards = {}, []
    for current, directories, names in os.walk(folder, followlinks=False):
        for name in directories + names:
            _safe(root, Path(current) / name)
        for name in names:
            path = Path(current) / name
            if path == index:
                continue
            relative = path.relative_to(root).as_posix()
            files[relative] = path.read_bytes()
            if path.suffix.lower() == ".md":
                card = knowledge.parse_card(path)
                knowledge._validate_card(root, card, False)
                cards.append(card)
    outside_links = knowledge._link_paths(root, index, prefix + suffix)
    card_paths = {Path(card["path"]) for card in cards}
    if any(path in card_paths for path in outside_links):
        raise ValueError("索引人工區已有知識卡連結；managed block 重建會重複，停止並保留原文")
    return data, prefix, suffix, files, cards


def _build_index(root, prefix, suffix, cards):
    rows = [START, "", "| 主題 | 知識卡 | 內容要點 |", "|---|---|---|"]
    for card in sorted(cards, key=lambda item: (item["metadata"]["topics"][0], item["metadata"]["title"])):
        meta = card["metadata"]
        target = quote(Path(card["path"]).relative_to(root / "knowledge").as_posix(), safe="/-_.")
        summary = next((line.strip() for line in card["body"].splitlines()
                        if line.strip() and not line.startswith("#")), meta["title"])
        rows.append("| " + _plain_text(meta["topics"][0]) + " | [" + _plain_text(meta["title"])
                    + "](" + target + ") | " + _plain_text(summary[:160]) + " |")
    rows.extend(["", END])
    return (prefix + "\n".join(rows) + suffix).encode("utf-8")


def _save_draft(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def refine_task(root: Path, id: str, settings: dict) -> dict:
    """Serialize refinement per workspace using a Windows exclusive handle."""
    root = flow._plain(Path(root)).resolve()
    with _workspace_lock(root):
        return _refine_task(root, id, settings)


def _refine_task(root: Path, id: str, settings: dict) -> dict:
    """Refine one acquired task, safely reuse drafts and validate the sole index.

    ``settings`` must select ``mode='api'`` and contain the explicit ``api``
    settings accepted by akb_api. No network call occurs for saved drafts or
    unchanged completed cards. Candidate mirrors remain in state for recovery;
    this module does not publish, archive originals or delete files.
    """
    root = flow._plain(Path(root)).resolve()
    if not isinstance(settings, dict) or settings.get("mode") != "api":
        raise ValueError("需要明確選擇 API 精煉模式")
    task = flow.read(root, id)
    if task.get("id") != id or task.get("stage") == "needs_review":
        raise ValueError("任務身份不符或 needs_review，需先人工核對")
    source = _source(root, task)
    metadata = _metadata(task, source)
    if task.get("card_path"):
        card = _safe(root, root / task["card_path"])
        if flow.digest(card) != task.get("card_sha256"):
            raise ValueError("既有知識卡已被人工修改；不覆寫、不呼叫 API")
        _split_index(_safe(root, root / "knowledge/README.md").read_bytes())
        return flow.validate_task(root, id)
    index_data, prefix, suffix, files, cards = _snapshot(root)
    draft_path = _safe(root, root / "state/drafts" / (id + ".json"), exists=False)
    if draft_path.exists():
        receipt = json.loads(draft_path.read_text(encoding="utf-8"))
        if receipt.get("source_sha256") != task["source_sha256"] or receipt.get("id") != id:
            raise ValueError("既有 API 草稿來源版本不同；保留現場，需人工核對")
        result = akb_api.validate_result(receipt["result"], source, metadata)
        date.fromisoformat(receipt["processed_at"])
    else:
        topics = sorted({topic for card in cards for topic in card["metadata"]["topics"]})
        result = akb_api.refine(source, metadata, topics, settings.get("api", {}))
        receipt = {"version": 1, "id": id, "source_sha256": task["source_sha256"],
                   "processed_at": date.today().isoformat(), "result": result}
        _save_draft(draft_path, receipt)
        result = akb_api.validate_result(result, source, metadata)
    card_name = knowledge.card_filename(result["title"], task["platform"], id)
    relative = "knowledge/cards/" + card_name
    data = _render(task, result, receipt["processed_at"])
    card_path = _safe(root, root / relative, exists=False)
    if card_path.exists() and card_path.read_bytes() != data:
        raise ValueError("既有同名卡片 bytes 不同；保留人工卡與 API 草稿待審閱")
    # Validate the full prospective knowledge tree, including true workflow
    # transitions, before adding anything to the publishable knowledge folder.
    candidate = _safe(root, root / "state/card-candidates" / uuid.uuid4().hex, exists=False)
    candidate.mkdir(parents=True)
    for path, content in {**files, relative: data}.items():
        target = candidate / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    proposed_card = knowledge.parse_card(candidate / relative)
    next_cards = [dict(card, path=str(candidate / Path(card["path"]).relative_to(root)))
                  for card in cards if Path(card["path"]) != card_path]
    next_cards.append(proposed_card)
    proposed_index = _build_index(candidate, prefix, suffix, next_cards)
    (candidate / "knowledge/README.md").write_bytes(proposed_index)
    flow.save(candidate, dict(task))
    flow.mark_refined(candidate, id, candidate / relative)
    flow.validate_task(candidate, id)
    # Compare every read version immediately before integration. Refuse concurrent
    # edits; never merge model output into a human-edited file automatically.
    current_task = flow.read(root, id)
    if current_task != task:
        raise ValueError("精煉期間任務紀錄已改變；保留 API 草稿，不覆寫")
    if _source(root, current_task) != source:
        raise ValueError("精煉期間來源已改變；保留 API 草稿")
    latest_index, _prefix, _suffix, latest_files, _cards = _snapshot(root)
    if latest_index != index_data or latest_files != files:
        raise ValueError("精煉期間索引或知識卡已改變；保留 API 草稿，不覆寫")
    card_path.parent.mkdir(parents=True, exist_ok=True)
    if not card_path.exists():
        with card_path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    elif card_path.read_bytes() != data:
        raise ValueError("整合前同名卡片已改變；不覆寫")
    _commit_index(root, index_data, proposed_index, candidate)
    flow.mark_refined(root, id, card_path)
    return flow.validate_task(root, id)
