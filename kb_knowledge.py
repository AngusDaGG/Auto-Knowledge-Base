"""Local, dependency-free knowledge card validation and literal rg search."""
from __future__ import annotations

import csv
from datetime import date
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

PLATFORMS = {"douyin", "youtube", "instagram", "tiktok", "bilibili", "local", "document"}
REQUIRED = ("type", "platform", "title", "author", "processed_at", "topics", "evidence_level")
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\s*\)")
TIMESTAMP = re.compile(r"(?<!\d)(?:\d{1,3}:)?[0-5]?\d:[0-5]\d(?:\.\d+)?(?!\d)")


def card_filename(title: str, platform: str, source_id: str) -> str:
    """Create a Windows-safe display name; metadata remains the source identity."""
    if not isinstance(title, str) or platform not in PLATFORMS or not isinstance(source_id, str):
        raise ValueError("知識檔名需要有效標題、平台與來源 ID")
    identity = source_id
    if platform not in {"local", "document"} and identity.startswith(platform + "_"):
        identity = identity[len(platform) + 1:]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", identity):
        raise ValueError("來源 ID 不能含路徑或不安全字元")
    cleaned = unicodedata.normalize("NFC", title)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip(" ._") or "知識卡"
    device_stem = cleaned.split(".", 1)[0].rstrip(" ")
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", device_stem):
        cleaned = "知識_" + cleaned
    suffix = f"__{platform}__{identity}.md"
    budget = 150 - len(suffix.encode("utf-16-le")) // 2
    while len(cleaned.encode("utf-16-le")) // 2 > budget:
        cleaned = cleaned[:-1]
    cleaned = cleaned.rstrip(" ._") or "知識卡"
    return cleaned + suffix


def is_card_filename(name: str) -> bool:
    """Allow only canonical title/platform/ID names, never arbitrary Markdown."""
    if not isinstance(name, str) or not name.endswith(".md"):
        return False
    parts = name[:-3].split("__", 2)
    if len(parts) != 3:
        return False
    try:
        return card_filename(*parts) == name
    except (ValueError, TypeError):
        return False


def _scalar(value: str):
    value = value.strip()
    if not value:
        raise ValueError("frontmatter 欄位不可留白")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("frontmatter 雙引號字串不合法") from exc
        if not isinstance(parsed, str):
            raise ValueError("frontmatter 僅支援字串及字串清單")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("frontmatter 單引號字串不合法")
        return value[1:-1].replace("''", "'")
    if value.startswith("["):
        if not value.endswith("]"):
            raise ValueError("frontmatter 清單未關閉")
        inside = value[1:-1].strip()
        if not inside:
            return []
        # Double-quoted JSON lists support commas inside aliases.
        if inside.startswith('"'):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                    return parsed
            except json.JSONDecodeError:
                pass
        return [_scalar(item) for item in next(csv.reader([inside], skipinitialspace=True))]
    if value[0] in "{&*!|>" or value in {"null", "true", "false", "~"}:
        raise ValueError("frontmatter 不支援 YAML 物件、標籤或隱含型別")
    return value


def parse_card(path: Path) -> dict:
    """Parse a deliberately small YAML subset; no executable YAML constructors."""
    path = Path(path)
    if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
           for part in (path.absolute(), *path.absolute().parents)) or not path.is_file():
        raise ValueError(f"知識卡必須是一般檔案：{path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"無法讀取 UTF-8 知識卡：{path}") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"知識卡缺少 frontmatter：{path.name}")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"frontmatter 未關閉：{path.name}") from exc
    metadata = {}
    list_key = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.fullmatch(r"\s+-\s+(.+)", line)
        if item and list_key:
            metadata[list_key].append(_scalar(item.group(1)))
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_]*):\s*(.*)", line)
        if not match:
            raise ValueError(f"不支援的 frontmatter 語法：{path.name}")
        key, value = match.groups()
        if key in metadata:
            raise ValueError(f"frontmatter 重複欄位 {key}：{path.name}")
        list_key = key if not value else None
        metadata[key] = [] if not value else _scalar(value)
    body = "\n".join(lines[end + 1:]).strip()
    return {"path": str(path), "metadata": metadata, "body": body}


def _safe_path(root: Path, path: Path, *, exists: bool = True) -> Path:
    """Reject links/junctions at every path component before reading anything."""
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise ValueError(f"路徑超出知識庫：{path}") from exc
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError(f"拒絕 symlink／junction：{current}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"路徑逃逸：{path}")
    if exists and not path.exists():
        raise ValueError(f"相對連結或檔案不存在：{path}")
    return path


def _link_paths(root: Path, owner: Path, text: str) -> list[Path]:
    result = []
    targets = [match.group(1) for match in LINK.finditer(text)]
    definition_pattern = r"^\s*\[([^\]\n]+)\]:\s*(<[^>]+>|\S+).*?$"
    definitions = re.findall(definition_pattern, text, re.MULTILINE)
    reference_text = re.sub(definition_pattern, "", text, flags=re.MULTILINE)
    reference_pattern = r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]"
    references = [key or label for label, key in re.findall(reference_pattern, reference_text)]
    remaining = re.sub(reference_pattern, "", reference_text)
    references.extend(re.findall(r"!?\[([^\]\n]+)\](?![\[(:])", remaining))
    for label, target in definitions:
        count = sum(reference.casefold() == label.casefold() for reference in references)
        targets.extend([target] * max(1, count))
    for raw in targets:
        target = unquote(raw.strip("<>"))
        url = urlsplit(target)
        if url.scheme:
            if url.scheme not in {"http", "https", "mailto"}:
                raise ValueError(f"不支援的連結協定：{owner.name}")
            continue
        if url.netloc or "\\" in target or url.path.startswith("/"):
            raise ValueError(f"不安全的相對連結：{owner.name}")
        if url.path:
            result.append(_safe_path(root, owner.parent / url.path))
    return result


def _source_identity(metadata: dict) -> tuple[str, str]:
    platform, identity = metadata["platform"], metadata["video_id"]
    prefix = platform + "_"
    if identity.startswith(prefix):
        identity = identity[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ValueError("影片 ID 包含不安全字元")
    url = urlsplit(metadata["source_url"])
    host = (url.hostname or "").lower()
    domains = {"douyin": ("douyin.com",), "youtube": ("youtube.com", "youtu.be"),
               "instagram": ("instagram.com",), "tiktok": ("tiktok.com",),
               "bilibili": ("bilibili.com", "b23.tv")}[platform]
    if (any(char.isspace() for char in metadata["source_url"])
            or url.scheme != "https" or url.username or url.password or url.port not in {None, 443}
            or not any(host == domain or host.endswith("." + domain) for domain in domains)):
        raise ValueError("來源平台與 URL 不相符，或 URL 不是公開 HTTPS 來源")
    canonical_id = None
    if platform == "youtube":
        canonical_id = url.path.strip("/") if host == "youtu.be" else parse_qs(url.query).get("v", [None])[0]
        if not canonical_id:
            match = re.fullmatch(r"/(?:shorts|embed|live)/([^/]+)/?", url.path)
            canonical_id = match.group(1) if match else None
    else:
        pattern = {"douyin": r"/video/([^/]+)", "instagram": r"/(?:reel|reels|p)/([^/]+)",
                   "tiktok": r"/[^/]+/video/([^/]+)", "bilibili": r"/video/([^/]+)"}[platform]
        match = re.search(pattern, url.path)
        canonical_id = match.group(1) if match else None
    if canonical_id is not None and canonical_id != identity:
        raise ValueError("影片 ID 與來源 URL 不相符")
    # Legacy public short links cannot establish the opaque destination offline.
    short = ((platform == "douyin" and host == "v.douyin.com")
             or (platform == "tiktok" and host in {"vm.tiktok.com", "vt.tiktok.com"})
             or (platform == "bilibili" and host == "b23.tv"))
    if canonical_id is None and not (short and re.fullmatch(r"/[^/]+/?", url.path)):
        raise ValueError("來源 URL 缺少可識別影片 ID")
    return platform, identity


def _string_list(value, field: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} 必須是非空字串清單")
    if len({item.casefold() for item in value}) != len(value):
        raise ValueError(f"{field} 不可重複")


def _validate_card(root: Path, card: dict, strict: bool) -> tuple[str, str]:
    meta, path = card["metadata"], Path(card["path"])
    for key in REQUIRED:
        if key not in meta or (key != "topics" and (not isinstance(meta[key], str) or not meta[key].strip())):
            raise ValueError(f"{path.name} 缺少有效欄位 {key}")
    _string_list(meta["topics"], "topics")
    document = meta["type"] == "distilled_document_knowledge"
    if meta["type"] not in {"distilled_video_knowledge", "distilled_document_knowledge"} or meta["platform"] not in PLATFORMS:
        raise ValueError(f"{path.name} 知識卡 type／platform 不合法")
    if document != (meta["platform"] == "document"):
        raise ValueError("文件知識卡必須使用 document 平台與 distilled_document_knowledge 類型")
    if meta.get("status", "completed") not in {"completed", "complete"}:
        raise ValueError(f"{path.name} 尚未完成，不能放入 knowledge")
    field = "document_id" if document else "video_id"
    if not isinstance(meta.get(field), str) or not meta[field].strip():
        raise ValueError(f"{path.name} 缺少有效欄位 {field}")
    relative = path.relative_to(root)
    if len(relative.parts) != 3 or relative.parts[0] != "knowledge":
        raise ValueError("知識卡必須直接位於 knowledge/cards 或既有平台資料夾")
    unified = path.parent.name == "cards"
    if unified:
        if path.name != card_filename(meta["title"], meta["platform"], meta[field]):
            raise ValueError(f"{path.name} 與標題／平台／來源 ID 的標準檔名不符")
    elif document or path.parent.name != meta["platform"] or path.stem != meta[field]:
        raise ValueError(f"{path.name} 與卡片 platform／{field} 不相符")
    strict = strict or document or unified
    try:
        date.fromisoformat(meta["processed_at"])
    except ValueError as exc:
        raise ValueError(f"{path.name} processed_at 必須為 ISO 日期") from exc
    original_filename = ""
    if document:
        digest = meta.get("source_sha256")
        document_id = re.fullmatch(r"doc_([a-f0-9]{12,64})", meta["document_id"])
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not document_id or not digest.startswith(document_id[1])):
            raise ValueError("文件 document_id 必須對應完整 source_sha256")
        if meta.get("content_type") not in {"user_imported_markdown", "user_import"}:
            raise ValueError("文件 content_type 必須為 user_imported_markdown 或 user_import，不能發布原文／工程報告")
        source_document = meta.get("source_document")
        if not isinstance(source_document, str) or not source_document.startswith("local:"):
            raise ValueError("文件來源需以 source_document: local:原始檔名 描述")
        original_filename = source_document[len("local:"):].strip()
        if (not original_filename or any(char in original_filename for char in "/\\:\r\n\x00")
                or Path(original_filename).suffix.lower() not in {".md", ".txt"}):
            raise ValueError("source_document 僅保存原始文字檔名，不可含本機路徑或失效連結")
        if "video_id" in meta or "source_transcript" in meta:
            raise ValueError("文件卡片不可偽裝影片 ID 或逐字稿來源")
        public = meta.get("source_url")
        if public:
            if not isinstance(public, str):
                raise ValueError("文件公開 source_url 必須為 HTTPS 字串")
            parsed = urlsplit(public)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in {None, 443} or any(char.isspace() for char in public)):
                raise ValueError("文件公開 source_url 必須為不含憑證的 HTTPS 來源")
        identity = ("document", digest)
    elif meta["platform"] == "local":
        if not re.fullmatch(r"local_[a-f0-9]{8,64}", meta["video_id"]):
            raise ValueError("本機卡片 ID 必須為 local_<8 至 64 位十六進位 hash>")
        if strict:
            raise ValueError("本機來源沒有公開影片來源，不可發布")
        identity = ("local", meta["video_id"])
    else:
        if not isinstance(meta.get("source_url"), str) or not meta["source_url"].strip():
            raise ValueError(f"{path.name} 缺少公開 source_url")
        identity = _source_identity(meta)
    if not card["body"] or not re.search(r"^#\s+\S", card["body"], re.MULTILINE):
        raise ValueError(f"{path.name} 缺少知識卡正文與標題")
    linked_paths = _link_paths(root, path, card["body"])
    if strict and any(not linked.is_relative_to(root / "knowledge") for linked in linked_paths):
        raise ValueError("新卡不能含有 knowledge 外的相對連結；本機來源請用文字描述")
    transcript = meta.get("source_transcript")
    if transcript:
        if not isinstance(transcript, str):
            raise ValueError("source_transcript 必須為字串")
        if not transcript.startswith(("本機", "local:")):
            transcript_path = _safe_path(root, path.parent / transcript)
            if not transcript_path.is_file():
                raise ValueError("source_transcript 必須指向檔案")
            if strict and not transcript_path.is_relative_to(root / "knowledge"):
                raise ValueError("新卡 source_transcript 請省略或使用本機文字，避免發布後連結失效")
    if strict:
        if not isinstance(meta.get("content_type"), str) or not meta["content_type"].strip():
            raise ValueError(f"{path.name} 新卡缺少 content_type")
        _string_list(meta.get("aliases"), "aliases")
        sections = re.findall(r"^#{2,6}\s+(.+?)\n(.*?)(?=^#{1,6}\s|\Z)", card["body"] + "\n", re.MULTILINE | re.DOTALL)
        groups = [("核心知識", r"核心|關鍵|知識|概念"), ("方法", r"方法|步驟|流程|實作"),
                  ("適用條件", r"適用|條件|情境|何時"), ("限制", r"限制|風險|核驗")]
        groups.append(("來源依據", r"來源.*依據|來源.*證據|原文.*依據") if document else
                      ("來源時間戳", r"來源.*時間|時間戳|來源.*證據|證據.*時間"))
        for name, pattern in groups:
            matching = [content for heading, content in sections if re.search(pattern, heading) and content.strip()]
            if not matching:
                raise ValueError(f"{path.name} 新卡缺少非空章節：{name}")
            if name == "來源時間戳" and not any(TIMESTAMP.search(content) for content in matching):
                raise ValueError(f"{path.name} 來源章節缺少有效時間戳")
            if name == "來源依據" and not any(original_filename in content and meta["source_sha256"] in content
                    and re.search(r"章節|原文章|段落|section", content, re.IGNORECASE) for content in matching):
                raise ValueError(f"{path.name} 來源依據需原始檔名、完整 SHA256 與原文章節／段落")
    return identity


def validate_knowledge(repo: Path, strict_paths: Iterable[str] = ()) -> dict:
    """Validate the sole index and all cards; only supplied new paths use strict rules."""
    original = Path(repo).absolute()
    if any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
           for part in (original, *original.parents)):
        raise ValueError("知識庫根目錄不可為 symlink／junction")
    root = original.resolve()
    folder = _safe_path(root, root / "knowledge")
    index = _safe_path(root, folder / "README.md")
    if not index.is_file():
        raise ValueError("knowledge/README.md 索引不存在")
    paths = []
    for current, directories, files in os.walk(folder, followlinks=False):
        for name in directories + files:
            candidate = _safe_path(root, Path(current) / name)
            if candidate.is_file() and candidate.suffix.lower() == ".md" and candidate != index:
                paths.append(candidate)
    strict = set()
    for raw in strict_paths:
        candidate = _safe_path(root, root / raw)
        if candidate not in paths:
            raise ValueError(f"strict 路徑不是索引中的知識卡：{raw}")
        strict.add(candidate)
    try:
        index_content = index.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("無法讀取知識索引") from exc
    linked = _link_paths(root, index, index_content)
    card_links = [path for path in linked if path in paths]
    if len(card_links) != len(set(card_links)):
        raise ValueError("索引重複連結同一知識卡")
    if set(paths) != set(card_links):
        missing = sorted(path.relative_to(root).as_posix() for path in set(paths) - set(card_links))
        raise ValueError("索引未列出知識卡：" + ", ".join(missing))
    identities, sources, cards = set(), set(), []
    for path in sorted(paths):
        card = parse_card(path)
        identity = _validate_card(root, card, path in strict)
        raw_source = card["metadata"].get("source_url") if identity[0] != "local" else None
        parsed_source = urlsplit(raw_source) if raw_source else None
        source = (("youtube", identity[1]) if identity[0] == "youtube" else
                  ((parsed_source.hostname or "").lower(), parsed_source.path.rstrip("/"))) if parsed_source else None
        if identity in identities or (source is not None and source in sources):
            raise ValueError(f"重複影片來源：{path.name}")
        identities.add(identity)
        if source is not None:
            sources.add(source)
        card["relative_path"] = path.relative_to(root).as_posix()
        cards.append(card)
    return {"index": "knowledge/README.md", "index_content": index_content, "cards": cards,
            "summary": f"知識索引驗證通過：{len(cards)} 張已完成知識卡；{len(strict)} 張採新版驗證"}


def search_knowledge(repo: Path, query: str, limit: int = 20) -> dict:
    """Search literal card text and aliases; no semantic expansion or shell parsing."""
    if not isinstance(query, str) or not query.strip() or any(char in query for char in "\r\n\x00"):
        raise ValueError("搜尋詞須為單行非空文字")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("搜尋筆數須介於 1 與 200")
    validated = validate_knowledge(repo)
    matches = []
    if validated["cards"]:
        executable = shutil.which("rg")
        if not executable:
            raise ValueError("找不到 ripgrep (rg)；請先確認既有工具 PATH")
        paths = [card["relative_path"] for card in validated["cards"]]
        try:
            result = subprocess.run([executable, "--no-config", "--json", "--ignore-case", "-F", "--", query, *paths],
                                    cwd=Path(repo), capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=30)
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise ValueError("ripgrep 搜尋失敗或逾時") from exc
        if result.returncode not in {0, 1}:
            raise ValueError("ripgrep 搜尋失敗：" + result.stderr.strip())
        for line in result.stdout.splitlines():
            entry = json.loads(line)
            if entry.get("type") == "match":
                data = entry["data"]
                matches.append({"path": data["path"]["text"].replace("\\", "/"),
                                "line": data["line_number"], "content": data["lines"]["text"].rstrip("\r\n")})
    return {"index": validated["index"], "index_content": validated["index_content"], "query": query,
            "matches": matches[:limit], "total_matches": len(matches), "no_results": not matches,
            "summary": f"找到 {len(matches)} 行文字命中" if matches else "沒有文字命中；此功能為字面搜尋，未執行語意搜尋"}
