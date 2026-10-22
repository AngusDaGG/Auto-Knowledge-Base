"""Local Agent workflow; transcription stays in the existing Video2Markdown engine."""
from __future__ import annotations

import argparse
import importlib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import kb_workflow as workflow
import kb_publish as publisher
from kb_knowledge import card_filename, search_knowledge, validate_knowledge

ROOT = Path.cwd()
TOOL = Path(__file__).resolve().parent / "engine"
PLATFORMS = ("douyin", "youtube", "instagram", "tiktok", "bilibili")


def transcript_path(video_id: str, sync) -> Path:
    from engine.platforms import transcript_relative_path as default_resolver
    resolver = getattr(sync, "transcript_relative_path", None)
    relative = Path((resolver or default_resolver)(video_id))
    if (len(relative.parts) != 3 or relative.parts[0] != "videos" or relative.parts[1] not in PLATFORMS
            or relative.name != f"{video_id}.md" or relative.is_absolute()):
        raise RuntimeError("引擎提供了不支援的逐字稿目的地")
    return relative


def engine(tool_dir: Path):
    if not (tool_dir / "transcript_tool.py").is_file():
        raise RuntimeError(f"找不到既有轉錄工具：{tool_dir}")
    sys.path.insert(0, str(tool_dir))
    return importlib.import_module("knowledge_sync"), importlib.import_module("transcript_tool")


def transcript_suffix(video_id: str, sync) -> str:
    platform = transcript_path(video_id, sync).parent.name
    native_id = video_id.removeprefix(platform + "_")
    return f"__{platform}__{native_id}.md"


def import_outputs(source: Path, repo: Path, sync, tool) -> int:
    """Validate the entire batch before adding files; never replace existing bytes."""
    if not source.is_dir():
        raise RuntimeError(f"輸出資料夾不存在：{source}")
    sources = sync.collect_sources(source)
    pending = []
    for video_id, path in sources.items():
        if not tool.is_complete(path):
            raise RuntimeError(f"逐字稿未完成：{path.name}")
        legacy = transcript_path(video_id, sync)
        folder = publisher._safe_path(repo, legacy.parent.as_posix())
        source_hash = workflow.digest(path)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source_hash:
            raise RuntimeError("讀取期間逐字稿已變動，請重新匯入")
        candidates = ([repo / legacy] if (repo / legacy).exists() else [])
        candidates.extend(folder.glob("*" + transcript_suffix(video_id, sync)))
        if len(candidates) > 1:
            raise RuntimeError("同 ID 有多份逐字稿，請先核對差異")
        if candidates:
            target = candidates[0]
            if workflow.digest(target) != source_hash:
                raise RuntimeError(f"同 ID 有版本差異，拒絕覆寫：{target}")
        else:
            heading = re.search(r"(?m)^#\s+(.+)$", data.decode("utf-8-sig"))
            filename = card_filename(heading[1].strip() if heading else "影片", legacy.parent.name, video_id)
            target = publisher._safe_path(repo, (legacy.parent / filename).as_posix())
            pending.append((target, data))
    for target, data in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
    print(f"匯入 {len(pending)} 份；已有相同內容 {len(sources) - len(pending)} 份")
    return len(pending)


def managed(path: str) -> bool:
    try:
        publisher._paths([path])
        return True
    except (RuntimeError, TypeError):
        return False


def _gh(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("GitHub CLI 無法使用或逾時；本機收據保留") from None
    if result.returncode:
        raise RuntimeError(f"GitHub CLI 操作失敗（exit {result.returncode}）；請檢查本機授權／網路")
    return result.stdout.strip()


def confirm_private(repo: Path) -> str:
    target = publisher.publish_target(repo)
    try:
        info = json.loads(_gh(repo, "api", "repos/" + target["repository"],
                              "--jq", "{private: .private, push: .permissions.push}"))
    except (ValueError, TypeError):
        raise RuntimeError("無法確認 GitHub repo 隱私與 Push 權限") from None
    if not isinstance(info, dict) or info.get("private") is not True or info.get("push") is not True:
        raise RuntimeError("GitHub repo 必須為 private，且目前帳號須有 Push 權限")
    return target["remote"]


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _last_receipt(repo: Path) -> Path:
    pointer = publisher._safe_path(repo.resolve(), "state/publish/last.json")
    if not pointer.is_file():
        raise ValueError("沒有可重試的發布收據；請先執行 sync --prepare-only")
    return Path(json.loads(pointer.read_text(encoding="utf-8"))["receipt_path"])


def _remember(repo: Path, receipt: dict) -> None:
    pointer = publisher._safe_path(repo.resolve(), "state/publish/last.json")
    _write_json(pointer, {"receipt_path": receipt["receipt_path"]})


def _advance_tasks(repo: Path, receipt: dict) -> None:
    stage = receipt.get("status")
    if stage in {"not_published", "conflict"}:
        for task in workflow.tasks(repo):
            card = task.get("card_path")
            expected = receipt.get("hashes", {}).get(card)
            if (task.get("publication_receipt") != receipt.get("receipt_path")
                    or not expected or expected != task.get("card_sha256")
                    or task.get("stage") not in {"validated", "branch_pushed", "main_pushed", "published", "needs_review"}):
                continue
            if task["stage"] != "needs_review":
                task["publication_previous_stage"] = task["stage"]
            task.update(stage="needs_review", remote_publication_status=stage,
                        next_step="inspect_remote_conflict")
            workflow.save(repo, task)
        return
    if stage not in {"prepared", "unchanged", "branch_pushed", "main_pushed", "published"}:
        return
    for task in workflow.tasks(repo):
        card = task.get("card_path")
        expected = receipt.get("hashes", {}).get(card)
        same_receipt = task.get("publication_receipt") == receipt.get("receipt_path")
        allowed = task.get("stage") in {"validated", "branch_pushed", "main_pushed", "published"}
        if task.get("stage") == "needs_review" and same_receipt and stage == "published":
            allowed = True
        if not card or not expected or expected != task.get("card_sha256") or not allowed:
            continue
        if (workflow.digest(repo / card) != expected
                or workflow.digest(Path(task["source_path"])) != task["source_sha256"]):
            continue
        if task.get("stage") == "published" and stage != "published":
            continue
        task.update(publication_receipt=receipt["receipt_path"], publication_sha=receipt["sha"],
                    remote_publication_status=stage)
        if stage not in {"prepared", "unchanged"}:
            task["stage"] = stage
        if stage == "main_pushed":
            task["next_step"] = "verify_remote_main"
        elif stage == "published":
            task["published_main_sha"] = receipt.get("published_sha")
            if task.get("next_step") in {"verify_remote_main", "inspect_remote_conflict"}:
                task.pop("next_step")
        workflow.save(repo, task)


def _run_publication(repo: Path, receipt_path: Path, *, verify=False, private_confirmed=False) -> dict:
    operation = publisher.verify_publication if verify else publisher.push_publication
    try:
        return operation(repo, receipt_path, private_confirmed=private_confirmed)
    except RuntimeError:
        # Trust saved statuses only after auditing the original receipt/checkout.
        # A network failure alone is never publication or conflict evidence.
        try:
            failed, _ = publisher._load(repo, receipt_path, private_confirmed, None)
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
        else:
            _remember(repo, failed)
            if failed.get("status") in {"conflict", "not_published", "main_pushed"}:
                _advance_tasks(repo, failed)
        raise


def publish(repo: Path, *, ids=None, dry_run=False, prepare_only=False,
            receipt_path: Path | None = None, retry=False, verify=False) -> dict:
    publisher.publish_target(repo)  # Opt-in is required even for prepare, retry and verify.
    if dry_run and (receipt_path or retry or verify or prepare_only):
        raise ValueError("--dry-run 不可與收據重試、verify 或 prepare-only 混用")
    if ids and (receipt_path or retry or verify):
        raise ValueError("收據重試／verify 不可更換 --id")
    if prepare_only and (receipt_path or retry or verify):
        raise ValueError("--prepare-only 僅用於準備新發布")
    existing = receipt_path or (_last_receipt(repo) if retry or verify else None)
    if existing is not None:
        existing = Path(existing)
        if not existing.is_absolute():
            existing = repo / existing
        confirmed = confirm_private(repo)
        result = _run_publication(repo, existing, verify=verify, private_confirmed=confirmed)
    else:
        paths = workflow.publication_paths(repo, ids)
        if not paths:
            return {"status": "no_validated_tasks", "message": "沒有可發布的已驗證任務"}
        confirmed = confirm_private(repo)
        result = publisher.prepare_publication(repo, paths, dry_run=dry_run, private_confirmed=confirmed)
        _remember(repo, result)
        _advance_tasks(repo, result)
        if dry_run or prepare_only:
            return result
        result = _run_publication(repo, Path(result["receipt_path"]), private_confirmed=confirmed)
    _remember(repo, result)
    _advance_tasks(repo, result)
    return result


def _show(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本機 Agent 影片與 Markdown 知識流程；只發布完成知識")
    parser.add_argument("--workspace", type=Path, default=None, help="資料工作區絕對路徑；預設目前目錄")
    parser.add_argument("--tool-dir", type=Path, default=TOOL, help="轉錄引擎目錄；預設內附 engine/")
    commands = parser.add_subparsers(dest="command", required=True)
    transcribe = commands.add_parser("transcribe", add_help=False, help="呼叫既有轉錄引擎，保留 --no-sync")
    transcribe.add_argument("args", nargs=argparse.REMAINDER)
    ingest = commands.add_parser("import-outputs", help="匯入既有本機轉錄，永不發布原文")
    ingest.add_argument("--from", dest="source", type=Path, default=None)
    commands.add_parser("check", help="唯讀確認引擎與完整逐字稿")
    prepare = commands.add_parser("prepare", help="建立小批次候選與去重，不下載")
    prepare.add_argument("--url", action="append", default=[])
    prepare.add_argument("--file", type=Path)
    prepare.add_argument("--topic", default="")
    prepare.add_argument("--discovered-via", default="")
    prepare.add_argument("--max-items", type=int, default=3)
    prepare.add_argument("--max-seconds", type=int, default=600)
    prepare.add_argument("--duration-seconds", type=float)
    acquire = commands.add_parser("acquire", help="取得指定候選；需要來源與片長確認")
    acquire.add_argument("id")
    acquire.add_argument("--authorized", action="store_true")
    acquire.add_argument("--duration-seconds", type=float)
    register = commands.add_parser("register-source", help="登記既有完整逐字稿")
    register.add_argument("path", type=Path)
    register.add_argument("--duration-seconds", type=float)
    imported = commands.add_parser("import-text", help="匯入明確指定的本機文字，不呼叫引擎")
    imported.add_argument("path", type=Path)
    imported.add_argument("--source-url", default="")
    commands.add_parser("scan-markdown", help="登記 markdown/ 頂層新文件；不精煉、不搬原件")
    archived = commands.add_parser("archive-markdown", help="歸檔本輪已完成驗證的原始 Markdown")
    archived.add_argument("--id", action="append", required=True)
    archived.add_argument("--scan-id", required=True)
    named = commands.add_parser("card-path", help="依任務穩定 ID 與標題建議新知識卡檔名，不寫入")
    named.add_argument("id")
    named.add_argument("--title", required=True)
    refined = commands.add_parser("refined", help="記錄 Agent 已完成的知識卡，不生成內容")
    refined.add_argument("id")
    refined.add_argument("card", type=Path)
    validate = commands.add_parser("validate", help="驗證任務或整份知識索引")
    validate.add_argument("--id", action="append", default=[])
    commands.add_parser("status", help="顯示本機任務狀態")
    search = commands.add_parser("search", help="先讀索引，再以字面文字與 aliases 搜尋")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    commands.add_parser("gemini-status", help="顯示預設停用狀態；不呼叫雲端")
    sync_parser = commands.add_parser("sync", help="驗證後隔離發布 knowledge 到 main；原文與程式留本機")
    sync_parser.add_argument("--id", action="append", default=[])
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--prepare-only", action="store_true")
    sync_parser.add_argument("--receipt", type=Path)
    sync_parser.add_argument("--retry", action="store_true")
    sync_parser.add_argument("--verify", action="store_true")
    args, remaining = parser.parse_known_args(argv)
    try:
        workspace = args.workspace if args.workspace is not None else ROOT
        if not workspace.is_absolute():
            raise ValueError("--workspace 必須是絕對路徑")
        workspace = workflow._plain(workspace)
        if not workspace.is_dir():
            raise ValueError("workspace 不存在；請先執行初始化")
        if args.command != "transcribe" and remaining:
            parser.error("未知參數：" + " ".join(remaining))
        markdown_scan = None
        if args.command in {"prepare", "acquire", "register-source", "import-text", "import-outputs"} or (
                args.command == "transcribe" and "--help" not in args.args):
            markdown_scan = workflow.scan_markdown(workspace)
            print("Markdown 收件掃描：")
            _show(markdown_scan)
        if args.command == "status":
            fields = ("id", "stage", "platform", "content_type", "card_path", "error", "next_step", "publication_receipt",
                      "remote_publication_status", "publication_previous_stage")
            _show({"tasks": [{key: task[key] for key in fields if key in task} for task in workflow.tasks(workspace)]})
        elif args.command == "search":
            result = search_knowledge(workspace, args.query, args.limit)
            print(result.pop("index_content"))
            _show(result)
        elif args.command == "import-text":
            _show(workflow.import_text(workspace, args.path.absolute(), source_url=args.source_url))
        elif args.command == "scan-markdown":
            _show(workflow.scan_markdown(workspace))
        elif args.command == "archive-markdown":
            _show(workflow.archive_markdown(workspace, args.id, scan_id=args.scan_id))
        elif args.command == "card-path":
            task = workflow.read(workspace, args.id)
            _show({"id": task["id"], "card_path": "knowledge/cards/" + card_filename(args.title, task["platform"], task["id"])})
        elif args.command == "refined":
            _show(workflow.mark_refined(workspace, args.id, (args.card if args.card.is_absolute() else workspace / args.card).absolute()))
        elif args.command == "validate":
            if args.id:
                _show({"tasks": [workflow.validate_task(workspace, key) for key in args.id]})
            else:
                result = validate_knowledge(workspace)
                _show({"index": result["index"], "summary": result["summary"],
                       "cards": [card["relative_path"] for card in result["cards"]]})
        elif args.command == "gemini-status":
            import kb_gemini
            _show(kb_gemini.get_status(workspace, enabled=False))
        elif args.command == "sync":
            _show(publish(workspace, ids=args.id, dry_run=args.dry_run, prepare_only=args.prepare_only,
                          receipt_path=args.receipt, retry=args.retry, verify=args.verify))
        else:
            sync, tool = engine(args.tool_dir.resolve())
            if args.command == "transcribe":
                actual = list(sys.argv[1:] if argv is None else argv)
                forwarded = actual[actual.index("transcribe") + 1:]
                if any(item.split("=", 1)[0] in {"--workspace", "--vendor-dir"} for item in forwarded):
                    raise RuntimeError("工作資料夾由統一入口管理，請勿覆寫 --workspace／--vendor-dir")
                command = [sys.executable, "-B", str(Path(__file__).with_name("kb_engine.py")), str(args.tool_dir.resolve()),
                           "--workspace", str(workspace), "--no-sync"]
                vendor = args.tool_dir / "vendor"
                if vendor.is_dir():
                    command.extend(["--vendor-dir", str(vendor.resolve())])
                result = subprocess.run(command + forwarded, cwd=workspace)
                if "--help" not in forwarded and (workspace / "outputs").is_dir():
                    import_outputs(workspace / "outputs", workspace, sync, tool)
                return result.returncode
            if args.command == "import-outputs":
                import_outputs((args.source or workspace / "outputs").resolve(), workspace, sync, tool)
            elif args.command == "prepare":
                urls = list(args.url)
                if args.file:
                    urls.extend(line.strip() for line in args.file.read_text(encoding="utf-8-sig").splitlines()
                                if line.strip() and not line.lstrip().startswith("#"))
                if not urls:
                    raise ValueError("請提供 --url 或 --file")
                if args.duration_seconds is not None and len(urls) != 1:
                    raise ValueError("--duration-seconds 只適用單一 URL")
                _show(workflow.prepare(workspace, urls, tool, max_items=args.max_items, max_seconds=args.max_seconds,
                                       duration_seconds=args.duration_seconds, topic=args.topic, discovered_via=args.discovered_via))
            elif args.command == "acquire":
                _show(workflow.acquire(workspace, args.tool_dir.resolve(), args.id, sync, tool, import_outputs,
                                       authorized=args.authorized, duration_seconds=args.duration_seconds))
            elif args.command == "register-source":
                _show(workflow.register_source(workspace, args.path.absolute(), tool, duration_seconds=args.duration_seconds))
            elif args.command == "check":
                transcripts = {}
                for platform in PLATFORMS:
                    transcripts.update(sync.collect_sources(workspace / "videos" / platform))
                if any(not tool.is_complete(path)
                       or path.parent != workspace / transcript_path(video_id, sync).parent
                       or not (path.name == f"{video_id}.md" or path.name.endswith(transcript_suffix(video_id, sync)))
                       for video_id, path in transcripts.items()):
                    raise RuntimeError("既有逐字稿格式或 ID 不符")
                print(f"引擎：{args.tool_dir.resolve()}；完整逐字稿 {len(transcripts)} 份")
        return 0
    except subprocess.CalledProcessError:
        print("停止：外部命令失敗；診斷與本機資料保留。", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"停止：{error}；本機資料保留。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
