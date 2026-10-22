"""Publish reviewed cards from an isolated checkout; development stays local.

New publications fast-forward remote main from an isolated knowledge-only commit.
It never creates PRs, merges, or changes policies. ``published`` requires a remote
main readback; old PR receipts remain verifiable but cannot authorize direct push.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

def publish_target(workspace: Path) -> dict:
    """Read only an explicit opt-in target; never infer a repo from Git remotes.

    Publication currently supports main only. A private target must already exist
    and its main branch must already contain a commit. No repo/policy mutation is
    performed. This check intentionally precedes all network and Git mutations.
    """
    workspace = Path(workspace).absolute()
    config_path = _safe_path(workspace, "akb.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RuntimeError("發布預設停用；請明確設定 workspace/akb.json 的 publish") from None
    except (OSError, ValueError):
        raise RuntimeError("無法讀取 workspace/akb.json") from None
    target = config.get("publish") if isinstance(config, dict) else None
    if not isinstance(target, dict) or target.get("enabled") is not True:
        raise RuntimeError("發布預設停用；只有 publish.enabled=true 才可發布")
    repository = target.get("repository")
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", repository)
            or repository.split("/")[1] in {".", ".."}
            or repository.endswith(".git")):
        raise RuntimeError("發布 repository 必須是明確的 owner/name，不接受 URL 或憑證")
    if target.get("branch", "main") != "main":
        raise RuntimeError("目前知識發布只支援明確的 main 分支")
    return {"repository": repository, "branch": "main",
            "remote": "https://github.com/" + repository + ".git"}

PLATFORMS = {"douyin", "youtube", "instagram", "tiktok", "bilibili"}
SECRET_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])",
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"(?im)^\s*(?:cookie|set-cookie)\s*:",
    r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[A-Za-z0-9_./+-]{8,}",
))


def _git(repo: Path, *args: str, allow_failure=False, input_bytes: bytes | None = None) -> bytes:
    # Do not inherit an alternate index/worktree/config injection from a caller.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in {"GIT_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND"}}
    result = subprocess.run(["git", "-c", "core.longpaths=true", "-c", "core.quotePath=false", "-c", "core.autocrlf=false",
                             "-c", "core.hooksPath=", "-C", str(repo), *args],
                            capture_output=True, env=env, input=input_bytes)
    if result.returncode and not allow_failure:
        # stderr may contain credential-bearing URLs. Never print raw Git errors.
        raise RuntimeError(f"Git {args[0]} 失敗（exit {result.returncode}）；本機收據保留")
    return result.stdout if not result.returncode else b""


def _text(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8").strip()


def _hash(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _paths(paths) -> list[str]:
    from kb_knowledge import is_card_filename
    result = []
    for name in paths:
        if not isinstance(name, str) or "\\" in name or ":" in name or "\0" in name:
            raise RuntimeError("發布路徑格式不符")
        parts = PurePosixPath(name).parts
        if (PurePosixPath(name).is_absolute() or any(p in {".", ".."} for p in parts)
                or "/".join(parts) != name):
            raise RuntimeError("拒絕發布路徑逃逸")
        card = (len(parts) == 3 and parts[0] == "knowledge" and parts[1] in PLATFORMS
                and re.fullmatch(r"[A-Za-z0-9_-]+\.md", parts[2])
                and parts[2].casefold() not in {"readme.md", "agents.md"})
        named_card = (len(parts) == 3 and parts[0:2] == ("knowledge", "cards")
                      and is_card_filename(parts[2])
                      and parts[2].split("__", 2)[1] in PLATFORMS | {"document"})
        if name != "knowledge/README.md" and not card and not named_card:
            raise RuntimeError("只允許明確指定的知識卡與 knowledge/README.md")
        result.append(name)
    if not result or len(result) != len(set(result)):
        raise RuntimeError("發布清單不可為空或重複")
    return sorted(result)


def _safe_path(repo: Path, name: str) -> Path:
    path = repo / name
    cursor = path
    while cursor != repo:
        if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
            raise RuntimeError("拒絕符號連結／junction")
        cursor = cursor.parent
    if path.resolve() != repo.joinpath(name).absolute():
        raise RuntimeError("拒絕發布路徑逃逸")
    return path


def _safe_blob(data: bytes, name: str) -> None:
    if len(data) > 2_000_000:
        raise RuntimeError("發布內容含大型檔案")
    try:
        contents = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("發布內容必須是 UTF-8 Markdown") from None
    if "\0" in contents or any(pattern.search(contents) for pattern in SECRET_PATTERNS):
        raise RuntimeError("發布內容疑似含憑證或二進位資料")


def scan_history(repo: Path) -> None:
    """Scan every blob reachable from every ref in the isolated checkout."""
    objects = dict(item.partition(" ")[::2] for item in
                   _text(repo, "rev-list", "--objects", "--all").splitlines())
    identifiers = "\n".join(objects).encode("ascii") + b"\n"
    metadata = _git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    input_bytes=identifiers).decode("ascii").splitlines()
    blobs = []
    for item in metadata:
        oid, kind, size = item.split()
        if kind != "blob":
            continue
        name = objects[oid]
        path = PurePosixPath(name)
        if (path.suffix.lower() in {".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".bin", ".jsonl"}
                or path.name.startswith(".env")
                or any(part in {"vendor", "state", "models", "outputs"} for part in path.parts)):
            raise RuntimeError("可達歷史含本機資料；拒絕上傳且不改寫歷史")
        if int(size) > 2_000_000:
            raise RuntimeError("可達歷史含大型檔案；拒絕上傳且不改寫歷史")
        blobs.append(oid)
    if not blobs:
        return
    batch = _git(repo, "cat-file", "--batch", input_bytes=("\n".join(blobs) + "\n").encode("ascii"))
    position = 0
    for expected in blobs:
        end = batch.index(b"\n", position)
        oid, kind, size = batch[position:end].decode("ascii").split()
        if oid != expected or kind != "blob":
            raise RuntimeError("Git 歷史物件讀回不符")
        position = end + 1
        data = batch[position:position + int(size)]
        position += int(size) + 1
        if any(p.search(data.decode("utf-8", errors="replace")) for p in SECRET_PATTERNS):
            raise RuntimeError("可達歷史疑似含憑證；拒絕上傳且不改寫歷史")


def _blob(repo: Path, ref: str, path: str) -> bytes | None:
    entry = _git(repo, "ls-tree", "-z", ref, "--", path)
    if not entry:
        return None
    metadata, actual = entry.rstrip(b"\0").split(b"\t", 1)
    mode, kind, oid = metadata.split()
    if actual.decode("utf-8") != path or mode != b"100644" or kind != b"blob":
        raise RuntimeError("知識路徑必須是一般 Markdown 檔案")
    return _git(repo, "cat-file", "blob", oid.decode("ascii"))


def _remote(repo: Path, private_confirmed: str | bool, test_remote: str | None,
            expected_remote: str | None = None) -> str:
    if test_remote is not None:
        path = Path(test_remote)
        if not path.is_absolute() or not path.is_dir() or _text(path, "rev-parse", "--is-bare-repository") != "true":
            raise RuntimeError("測試 remote 必須是既有本機 bare repo")
        expected = str(path.resolve())
    else:
        # Workspace Git remotes never authorize a publication target. Only the
        # isolated checkout is required to have the exact configured origin.
        expected = expected_remote or publish_target(repo)["remote"]
        if not isinstance(private_confirmed, str) or private_confirmed != expected:
            raise RuntimeError("既有 GitHub private 確認未綁定目前發布目的地，停止發布")
        if expected_remote is None:
            return expected
    if (_text(repo, "remote", "get-url", "origin") != expected
            or _text(repo, "remote", "get-url", "--push", "origin") != expected):
        raise RuntimeError("origin 與授權發布目的地不符")
    return expected


def _identity(repo: Path) -> tuple[str, str]:
    values = tuple(_git(repo, "config", "--get", field, allow_failure=True).decode("utf-8").strip()
                   for field in ("user.name", "user.email"))
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise RuntimeError("工作區／全域 Git 設定缺少有效 user.name／user.email，停止發布")
    return values


def _validate(repo: Path, paths: list[str]) -> None:
    from kb_knowledge import validate_knowledge
    result = validate_knowledge(repo, strict_paths=[p for p in paths if p != "knowledge/README.md"])
    if isinstance(result, dict) and (result.get("errors") or result.get("ok") is False):
        raise RuntimeError("知識卡驗證未通過，停止發布")
    if result is False or getattr(result, "ok", True) is False:
        raise RuntimeError("知識卡驗證未通過，停止發布")


def _save(receipt: dict) -> dict:
    path = Path(receipt["receipt_path"])
    pending = path.with_suffix(".pending.json")
    pending.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)
    return receipt


def _fetch(checkout: Path) -> str:
    _git(checkout, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    return _text(checkout, "rev-parse", "refs/remotes/origin/main")


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git(repo, "merge-base", older, newer, allow_failure=True).strip() == older.encode("ascii")


def _published_baselines(repo: Path, checkout: Path, base: str, remote: str) -> dict:
    """Recheck saved publications against this clone; never trust local flags alone.

    The development HEAD can intentionally remain old. The newest verified own
    publication is therefore the per-file baseline; later external edits conflict.
    No prior checkout is opened, changed, or required for recovery.
    """
    baselines = {}
    for path in sorted((repo / "state" / "publish").glob("*/receipt.json")):
        try:
            _safe_path(repo, path.relative_to(repo).as_posix())
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or saved.get("version") not in {1, 2} or saved.get("status") != "published"
                    or saved.get("repo") != str(repo) or saved.get("remote") != remote
                    or saved.get("receipt_path") != str(path)
                    or saved.get("workspace") != str(path.parent / "checkout")
                    or saved.get("dry_run") is not False
                    or not isinstance(saved.get("sha"), str)
                    or not re.fullmatch(r"[0-9a-f]{40}", saved["sha"])
                    or not isinstance(saved.get("branch"), str)
                    or not re.fullmatch(r"codex/knowledge-sync-[A-Za-z0-9-]+", saved["branch"])):
                continue
            if saved["version"] == 2 and (saved.get("publication_mode") != "direct_main"
                                           or saved.get("target_ref") != "refs/heads/main"):
                continue
            published = saved.get("published_sha", "")
            if (not isinstance(published, str) or not re.fullmatch(r"[0-9a-f]{40}", published)
                    or not _ancestor(checkout, published, base)):
                continue
            paths = _paths(saved["paths"])
            hashes = saved["hashes"]
            if (paths != saved["paths"] or not isinstance(hashes, dict) or set(hashes) != set(paths)
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in hashes.values())):
                continue
            contents = {name: _blob(checkout, published, name) for name in paths}
            if any(_hash(contents[name]) != hashes[name] for name in paths):
                continue
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # Invalid/unrelated state cannot authorize an overwrite. Without a
            # verified receipt the normal local-HEAD conflict check still applies.
            continue
        for name in paths:
            candidate = {"sha": published, "hash": hashes[name], "data": contents[name],
                         "receipt_path": str(path)}
            current = baselines.get(name)
            if current is None or _ancestor(checkout, current["sha"], published):
                baselines[name] = candidate
            elif not _ancestor(checkout, published, current["sha"]) and current["hash"] != candidate["hash"]:
                raise RuntimeError("已驗證發布基準有不可比較的版本，停止且不覆寫")
    return baselines


def _audit(checkout: Path, receipt: dict, *, staged=False) -> None:
    paths = receipt["paths"]
    args = ("diff", "--cached", "--name-status", "-z", "--no-renames") if staged else (
        "diff", "--name-status", "-z", "--no-renames", receipt["base"], receipt["sha"])
    entries = _git(checkout, *args).decode("utf-8").rstrip("\0").split("\0")
    if entries == [""]:
        entries = []
    changed = []
    for index in range(0, len(entries), 2):
        status, path = entries[index:index + 2]
        if status not in {"A", "M"} or path not in paths:
            raise RuntimeError("隔離提交含未授權路徑或刪除")
        changed.append(path)
    if changed != receipt["changed_paths"]:
        raise RuntimeError("隔離提交清單與收據不符")
    # Inspect the entire staged diff, not only names or the final tree.
    diff_args = ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv") if staged else (
        "diff", "--binary", "--no-ext-diff", "--no-textconv", receipt["base"], receipt["sha"])
    _safe_blob(_git(checkout, *diff_args), "staged-diff")
    ref = ":" if staged else receipt["sha"]
    for path in paths:
        data = _git(checkout, "show", ":" + path) if staged else _blob(checkout, ref, path)
        if _hash(data) != receipt["hashes"][path]:
            raise RuntimeError("隔離檔案 hash 與收據不符")
        _safe_blob(data, path)
    if not staged:
        commits = _text(checkout, "rev-list", receipt["base"] + ".." + receipt["sha"]).splitlines()
        expected = [receipt["sha"]] if receipt["changed_paths"] else []
        if commits != expected:
            raise RuntimeError("隔離提交含未授權的其他 outgoing commit")
        if commits and _text(checkout, "rev-parse", receipt["sha"] + "^") != receipt["base"]:
            raise RuntimeError("隔離提交並非以遠端 main 為唯一父提交")
        # Every outgoing commit and newly reachable blob receives the same checks.
        for commit in commits:
            _safe_blob(_git(checkout, "show", "--format=fuller", "--binary", "--no-ext-diff",
                                "--no-textconv", commit), "outgoing-commit")
        scan_history(checkout)


def prepare_publication(repo: Path, paths: list[str], *, dry_run=False,
                        private_confirmed=False, strict_paths=None,
                        _test_remote: str | None = None) -> dict:
    """Validate/copy explicit bytes onto remote main without touching local Git state.

    Caller must obtain a live private-repository confirmation and pass its exact
    HTTPS remote as private_confirmed. Bare offline fixtures use _test_remote. ``strict_paths`` is
    accepted for CLI compatibility but must equal the exact publication allowlist.
    Tests may use an existing local bare repository via the private test argument.
    """
    repo = Path(repo).resolve()
    paths = _paths(paths)
    if strict_paths is not None and _paths(strict_paths) != paths:
        raise RuntimeError("驗證清單必須與發布清單完全相同")
    remote = _remote(repo, private_confirmed, _test_remote)
    author_name, author_email = _identity(repo)
    contents = {}
    for path in paths:
        source = _safe_path(repo, path)
        if not source.is_file():
            raise RuntimeError("拒絕發布刪除或不存在的知識卡")
        contents[path] = source.read_bytes()
        _safe_blob(contents[path], path)
    _validate(repo, paths)
    if any(_safe_path(repo, p).read_bytes() != contents[p] for p in paths):
        raise RuntimeError("驗證期間知識內容已變動，請重新 prepare")
    # A user workspace need not be a Git repo. Never trust a code repo's
    # HEAD as authorization to overwrite remote knowledge. Prior verified own
    # publication receipts supply production baselines; local bare test fixtures
    # additionally exercise the legacy conflict baseline contract.
    local_head = _text(repo, "rev-parse", "HEAD") if _test_remote is not None else None
    root = _safe_path(repo, "state/publish")
    root.mkdir(parents=True, exist_ok=True)
    publication = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    publication.mkdir()
    checkout = publication / "checkout"
    _git(publication, "clone", "--single-branch", "--branch", "main", "--no-tags", "--no-local",
         "--config", "core.autocrlf=false", "--", remote, str(checkout))
    _remote(checkout, private_confirmed, _test_remote, remote)
    base = _text(checkout, "rev-parse", "HEAD")
    published_baselines = _published_baselines(repo, checkout, base, remote)
    receipt = {"version": 2, "publication_mode": "direct_main", "target_ref": "refs/heads/main",
               "status": "preparing", "repo": str(repo), "remote": remote,
               "workspace": str(checkout), "receipt_path": str(publication / "receipt.json"),
               "local_head": local_head, "base": base, "branch": "codex/knowledge-sync-" + publication.name,
               "paths": paths, "hashes": {p: _hash(contents[p]) for p in paths},
               "baseline_hashes": {}, "baseline_sources": {}, "local_head_hashes": {},
               "remote_hashes": {}, "changed_paths": [], "sha": None,
               "attempts": 0, "dry_run": bool(dry_run)}
    _save(receipt)
    for path in paths:
        baseline = _blob(repo, local_head, path) if local_head else None
        receipt["local_head_hashes"][path] = _hash(baseline)
        published = published_baselines.get(path)
        if published is not None:
            baseline = published["data"]
            receipt["baseline_sources"][path] = {"kind": "published_receipt", "sha": published["sha"],
                                                  "receipt_path": published["receipt_path"]}
        else:
            receipt["baseline_sources"][path] = {"kind": "local_head" if local_head else "unestablished", "sha": local_head}
        previous = _blob(checkout, base, path)
        receipt["baseline_hashes"][path] = _hash(baseline)
        receipt["remote_hashes"][path] = _hash(previous)
        if previous != contents[path] and previous != baseline:
            receipt["status"] = "conflict"
            _save(receipt)
            raise RuntimeError("遠端與已驗證發布／本機 HEAD 的知識版本分歧；停止且不覆寫")
        if previous != contents[path]:
            receipt["changed_paths"].append(path)
        target = _safe_path(checkout, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[path])
    # Validate the exact assembled remote tree too: a local index may reference a
    # different unselected card that exists only in the development worktree.
    _validate(checkout, paths)
    _git(checkout, "switch", "-c", receipt["branch"])
    _git(checkout, "add", "--", *paths)
    _audit(checkout, receipt, staged=True)
    scan_history(checkout)
    if dry_run:
        receipt["status"] = "dry_run"
    elif receipt["changed_paths"]:
        _git(checkout, "-c", "user.name=" + author_name, "-c", "user.email=" + author_email,
             "commit", "-m", "Update reviewed knowledge cards")
        receipt["sha"] = _text(checkout, "rev-parse", "HEAD")
        _audit(checkout, receipt)
        receipt["status"] = "prepared"
    else:
        receipt["sha"] = base
        _audit(checkout, receipt)
        receipt["status"] = "unchanged"
    return _save(receipt)


def _load(repo: Path, receipt_path: Path, private_confirmed: str | bool, test_remote: str | None) -> tuple[dict, Path]:
    repo = Path(repo).resolve()
    path = Path(receipt_path).absolute()
    if not path.is_relative_to(repo / "state" / "publish"):
        raise RuntimeError("收據必須位於本機 state/publish")
    _safe_path(repo, path.relative_to(repo).as_posix())
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (receipt.get("version") not in {1, 2} or receipt.get("repo") != str(repo)
            or receipt.get("receipt_path") != str(path)):
        raise RuntimeError("收據與本機專案不符")
    if receipt["version"] == 2 and (receipt.get("publication_mode") != "direct_main"
                                    or receipt.get("target_ref") != "refs/heads/main"):
        raise RuntimeError("收據發布模式不符；請重新 prepare")
    checkout = path.parent / "checkout"
    _safe_path(repo, checkout.relative_to(repo).as_posix())
    git_dir = _safe_path(repo, (checkout / ".git").relative_to(repo).as_posix())
    if not git_dir.is_dir() or (git_dir / "objects" / "info" / "alternates").exists():
        raise RuntimeError("發布工作樹必須有獨立 Git objects，不可借用原始 repo")
    if receipt["workspace"] != str(checkout) or _paths(receipt["paths"]) != receipt["paths"]:
        raise RuntimeError("收據發布清單／工作樹不符")
    if (not re.fullmatch(r"codex/knowledge-sync-[A-Za-z0-9-]+", receipt["branch"])
            or not re.fullmatch(r"[0-9a-f]{40}", receipt.get("sha") or "")
            or not re.fullmatch(r"[0-9a-f]{40}", receipt.get("base") or "")):
        raise RuntimeError("收據並非可推送版本；dry-run 請重新 prepare")
    remote = _remote(repo, private_confirmed, test_remote)
    if _remote(checkout, private_confirmed, test_remote, remote) != remote or receipt["remote"] != remote:
        raise RuntimeError("收據 remote 不符")
    if (_text(checkout, "rev-parse", "HEAD") != receipt["sha"]
            or _text(checkout, "branch", "--show-current") != receipt["branch"]
            or _text(checkout, "status", "--porcelain", "--untracked-files=all")):
        raise RuntimeError("隔離工作樹／分支／SHA 已變動，停止重試")
    _audit(checkout, receipt)
    return receipt, checkout


def _check_current_main(checkout: Path, receipt: dict) -> tuple[str, bool]:
    current = _fetch(checkout)
    matches = True
    for path in receipt["paths"]:
        actual = _hash(_blob(checkout, current, path))
        if actual != receipt["hashes"][path]:
            matches = False
        if actual not in {receipt["remote_hashes"][path], receipt["hashes"][path]}:
            receipt["status"] = "conflict"
            _save(receipt)
            raise RuntimeError("prepare 後遠端同檔已變更；停止重試，不覆寫")
    scan_history(checkout)
    return current, matches


def push_publication(repo: Path, receipt_path: Path, *, private_confirmed=False,
                     _test_remote: str | None = None) -> dict:
    """Fast-forward main once; retry reads back an already-present commit first."""
    receipt, checkout = _load(repo, receipt_path, private_confirmed, _test_remote)
    if receipt["version"] != 2 or receipt.get("publication_mode") != "direct_main":
        raise RuntimeError("舊 PR 收據僅能 verify；直接發布 main 請重新 prepare")
    current, matches = _check_current_main(checkout, receipt)
    if matches and _ancestor(checkout, receipt["sha"], current):
        receipt.update(status="published", published_sha=current, verified_main_sha=current)
        return _save(receipt)
    if (current != receipt["base"] or receipt["status"] in {"main_pushed", "published"}):
        receipt["status"] = "conflict"
        _save(receipt)
        raise RuntimeError("遠端 main 已變動；停止推送，請重新 prepare")
    receipt["attempts"] += 1
    receipt["status"] = "push_pending"
    _save(receipt)
    try:
        # No force, '+' refspec, mirror, tags, or deletion. Git's server-side
        # fast-forward check also rejects a divergent advance after the fetch.
        _git(checkout, "-c", "push.followTags=false", "push", "origin",
             receipt["sha"] + ":refs/heads/main")
    except RuntimeError:
        receipt["status"] = "push_failed"
        _save(receipt)
        raise
    receipt["status"] = "main_pushed"
    _save(receipt)
    current, matches = _check_current_main(checkout, receipt)
    if not matches or not _ancestor(checkout, receipt["sha"], current):
        raise RuntimeError("Push 後 main 尚未核對成功；保留 main_pushed 收據，請重試 verify")
    receipt.update(status="published", published_sha=current, verified_main_sha=current)
    return _save(receipt)


def verify_publication(repo: Path, receipt_path: Path, *, private_confirmed=False,
                       _test_remote: str | None = None) -> dict:
    """Read current remote main and every selected byte before marking published."""
    receipt, checkout = _load(repo, receipt_path, private_confirmed, _test_remote)
    current, matches = _check_current_main(checkout, receipt)
    receipt["verified_main_sha"] = current
    direct = receipt["version"] == 2
    if matches and (not direct or _ancestor(checkout, receipt["sha"], current)):
        receipt.update(status="published", published_sha=current)
    elif direct and (current != receipt["base"] or receipt["status"] in {"main_pushed", "published"}):
        receipt["status"] = "conflict"
        _save(receipt)
        raise RuntimeError("遠端 main 與直接發布收據不符；請重新 prepare")
    elif receipt["status"] == "published":
        receipt["status"] = "not_published"
    return _save(receipt)
