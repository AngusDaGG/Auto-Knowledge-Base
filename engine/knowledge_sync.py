"""Transcript validation compatibility helpers; this engine never publishes."""
from dataclasses import dataclass
from pathlib import Path
import re
from platforms import identify, platform_for_key

VIDEO_ID = re.compile(r"(?m)^- 影片 ID：([A-Za-z0-9_-]+)$")
SECRET_PATTERNS = (
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?im)^\s*(?:cookie|set-cookie)\s*:"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[A-Za-z0-9_./+-]{8,}"),
)


@dataclass
class SyncResult:
    ok: bool
    copied: int = 0
    committed: bool = False
    pushed: bool = False
    message: str = ""


def validate_platform_source(video_id: str, contents: str) -> None:
    if platform_for_key(video_id) != "douyin":
        source_match = re.search(r"(?m)^- 原始分享連結：(https://\S+)$", contents)
        reference = identify(source_match[1]) if source_match else None
        if not reference or reference.key != video_id:
            raise RuntimeError("來源平台／影片 ID 不符")


def collect_sources(outputs: Path) -> dict[str, Path]:
    sources = {}
    for path in sorted(outputs.glob("*.md")):
        contents = path.read_text(encoding="utf-8")
        match = VIDEO_ID.search(contents)
        if not match or "## 完整逐字稿" not in contents or "- 原始分享連結：" not in contents:
            raise RuntimeError(f"逐字稿 Markdown 欄位不完整：{path.name}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(contents):
                raise RuntimeError(f"Markdown 疑似包含憑證或 Cookie，拒絕同步：{path.name}")
        video_id = match.group(1)
        validate_platform_source(video_id, contents)
        if video_id in sources:
            raise RuntimeError(f"輸出中有重複影片 ID：{video_id}")
        sources[video_id] = path
    return sources


def sync_outputs(outputs: Path, repo: Path | None = None) -> SyncResult:
    """Fail closed for old callers; publishing belongs to the root workflow."""
    return SyncResult(False, message="獨立引擎停用遠端同步；請使用主程式的明確發布流程")
