"""Small user entry point: existing Agent or explicitly configured model API."""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import kb_knowledge as knowledge
import kb_workflow as flow

CODE = Path(__file__).resolve().parent
INDEX = "# Auto Knowledge Base\n\n依內容維護主題；每篇知識卡只列一次。\n\n<!-- akb:index:start -->\n<!-- akb:index:end -->\n"


def workspace_path(value=None):
    root = flow._plain(Path(value or os.environ.get("AKB_WORKSPACE") or CODE / "workspace").absolute()).resolve()
    if root == CODE or CODE.is_relative_to(root):
        raise ValueError("workspace_must_not_contain_source_code")
    if root.is_relative_to(CODE) and root != CODE / "workspace":
        raise ValueError("use_default_workspace_or_external_directory")
    if root.name.lower() in {".git", ".env", "engine", "tests"}:
        raise ValueError("unsafe_workspace")
    return root


def default_settings():
    return {"version": 1, "mode": "agent", "max_batch": 20,
            "engine": {},
            "api": {"endpoint": "", "model": "", "api_key_env": "AKB_API_KEY",
                    "allow_remote": False, "timeout": 90, "max_input_chars": 60000,
                    "max_output_tokens": 6000},
            "publish": {"enabled": False, "repository": "", "branch": "main"}}


def init_workspace(root):
    root = workspace_path(root)
    root.mkdir(parents=True, exist_ok=True)
    for relative in ("knowledge/cards", "markdown/processed", "state", "outputs", "videos/douyin"):
        flow._plain(root / relative).mkdir(parents=True, exist_ok=True)
    created = []
    initial = {"akb.json": json.dumps(default_settings(), ensure_ascii=False, indent=2) + "\n",
               "knowledge/README.md": INDEX,
               ".gitignore": "*\n!knowledge/\n!knowledge/**\n!.gitignore\n"}
    for name, text in initial.items():
        path = flow._plain(root / name)
        if not path.exists():
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            created.append(name)
    return {"workspace": str(root), "created": created, "mode": settings_for(root)["mode"]}


def settings_for(root):
    path = flow._plain(root / "akb.json")
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict) or settings.get("version") != 1:
        raise ValueError("unsupported_workspace_config")
    if settings.get("mode") not in {"agent", "api"}:
        raise ValueError("mode_must_be_agent_or_api")
    for section in ("api", "engine", "publish"):
        if not isinstance(settings.get(section), dict):
            raise ValueError("invalid_config_section")
    if type(settings.get("max_batch")) is not int or not 1 <= settings["max_batch"] <= 100:
        raise ValueError("max_batch_must_be_1_to_100")
    return settings


def _runtime_path(value, root):
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _package_available(name, vendor):
    # Match the engine's vendor-first lookup without importing runtime packages.
    try:
        return bool((vendor and importlib.machinery.PathFinder.find_spec(name, [str(vendor)]))
                    or importlib.util.find_spec(name))
    except (ImportError, ValueError, OSError):
        return False


def _binary_available(value, root, env):
    if Path(value).is_absolute() or os.path.dirname(value):
        return _runtime_path(value, root).is_file()
    return bool(shutil.which(value, path=env.get("PATH")))


def doctor(root):
    initialized = flow._plain(root / "akb.json").is_file()
    settings = settings_for(root) if initialized else default_settings()
    from akb_intake import _settings
    _limit, engine, _timeout = _settings(settings)
    env = dict(os.environ)
    env.update(engine.get("env", {}))
    selected = engine.get("python", sys.executable)
    selected_path = (str(_runtime_path(selected, root)) if os.path.dirname(str(selected))
                     else shutil.which(str(selected), path=env.get("PATH")))
    python_matches = bool(selected_path and Path(selected_path).resolve() == Path(sys.executable).resolve())
    vendor = _runtime_path(engine["vendor_dir"], root) if engine.get("vendor_dir") else None
    packages = {name: _package_available(name, vendor) if python_matches else None
                for name in ("faster_whisper", "av", "ctranslate2", "yt_dlp", "yt_dlp_ejs")}
    model_value = engine.get("model_path") or env.get("AKB_MODEL_PATH")
    model = _runtime_path(model_value, root) if model_value else None
    required_model = ("model.bin", "config.json", "tokenizer.json")
    missing_model = []
    for name in required_model:
        try:
            if not model or not (model / name).is_file() or (model / name).stat().st_size == 0:
                missing_model.append(name)
        except OSError:
            missing_model.append(name)
    browser_value = env.get("AKB_BROWSER_EXECUTABLE")
    if browser_value:
        browser = _runtime_path(browser_value, root).is_file()
    else:
        channel = env.get("AKB_BROWSER_CHANNEL")
        relatives = {"msedge": "Microsoft/Edge/Application/msedge.exe",
                     "chrome": "Google/Chrome/Application/chrome.exe"}
        browser = any((Path(base) / relative).is_file()
                      for key, relative in relatives.items() if not channel or key == channel
                      for base in (env.get("PROGRAMFILES") or env.get("ProgramFiles"),
                                   env.get("PROGRAMFILES(X86)") or env.get("ProgramFiles(x86)")) if base)
    module_value = env.get("AKB_PLAYWRIGHT_MODULE")
    modules = ([_runtime_path(module_value, root)] if module_value else
               [CODE / "node_modules/playwright", *[Path(base) / "playwright"
                 for base in env.get("NODE_PATH", "").split(os.pathsep) if base]])
    ffmpeg = env.get("AKB_FFMPEG", "ffmpeg")
    ffmpeg_path = _runtime_path(ffmpeg, root)
    if env.get("AKB_FFMPEG") and ffmpeg_path.is_dir():
        ffmpeg = str(ffmpeg_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    checks = {"engine_python_matches": python_matches,
              "node": _binary_available(env.get("AKB_NODE", "node"), root, env),
              "ffmpeg": _binary_available(ffmpeg, root, env),
              "browser_executable": browser,
              "playwright_module": any((path / "package.json").is_file() for path in modules),
              "model_files": not missing_model}
    missing = [name for name, found in checks.items() if not found]
    missing.extend(name for name, found in packages.items() if found is not True)
    key_name = settings["api"].get("api_key_env")
    return {"workspace": str(root), "python": sys.version.split()[0],
            "workspace_initialized": initialized,
            "python_tested_version": sys.version_info[:2] == (3, 12),
            "platform_tested": sys.platform == "win32",
            "commands": {name: bool(shutil.which(name)) for name in ("git", "gh", "rg", "node", "ffmpeg")},
            "python_packages": packages,
            "runtime_checks": checks,
            "model_path_configured": bool(model_value),
            "model_missing_files": missing_model,
            "video_missing": missing,
            "capabilities": {"markdown": "available", "search": "detected" if shutil.which("rg") else "missing_ripgrep",
                             "video": "needs_setup" if missing else "detected_not_verified"},
            "setup_guide": str(CODE / "docs/AGENT_SETUP.md"),
            "api_configured": bool(settings["api"].get("endpoint") and settings["api"].get("model")),
            "api_key_set": bool(isinstance(key_name, str) and key_name and os.environ.get(key_name)),
            "publish_enabled": settings["publish"].get("enabled") is True,
            "gemini_video_backup": "disabled",
            "note": "唯讀檢查不安裝、不下載、不啟動瀏覽器或載入模型。找到檔案不等於可用；請依首次設定指引完成實際驗證。"}


def safe_error(exc):
    # Never echo provider response bodies, environment values or raw subprocess errors.
    return {"error": getattr(exc, "code", type(exc).__name__),
            "message": "此階段未完成；原件與已取得內容保留，可修正設定後續跑。"}


def finish(root, ids, scan_id=None):
    settings = settings_for(root)
    ids = list(dict.fromkeys(ids))
    result = {"validated": [], "failed": [], "archive": None, "publication": "disabled"}
    for key in ids:
        try:
            flow.validate_task(root, key)
            result["validated"].append(key)
        except (ValueError, OSError, RuntimeError) as exc:
            result["failed"].append({"id": key, **safe_error(exc)})
    if scan_id and result["validated"]:
        try:
            manifest = json.loads(flow._scan_path(root, scan_id).read_text(encoding="utf-8"))
            scanned = {entry["id"] for entry in manifest["entries"]}
            archive_ids = [key for key in result["validated"] if key in scanned]
            if archive_ids:
                result["archive"] = flow.archive_markdown(root, archive_ids, scan_id=scan_id)
        except (ValueError, OSError, RuntimeError) as exc:
            result["archive"] = {"status": "originals_preserved", **safe_error(exc)}
    if settings["publish"].get("enabled") is True and result["validated"]:
        import knowledge_base
        current = [flow.read(root, key) for key in result["validated"]]
        receipts = {}
        fresh = []
        results = []
        for task in current:
            if task["stage"] == "published":
                continue
            if task.get("publication_receipt"):
                receipts[task["publication_receipt"]] = task["stage"]
            elif task["stage"] == "validated":
                fresh.append(task["id"])
            else:
                results.append({"id": task["id"], "status": "not_published", "error": "missing_publication_receipt"})
        for receipt, stage in receipts.items():
            try:
                results.append(knowledge_base.publish(root, receipt_path=Path(receipt),
                                                      verify=stage in {"main_pushed", "branch_pushed"},
                                                      retry=stage == "validated"))
            except (ValueError, OSError, RuntimeError) as exc:
                results.append({"status": "not_published", **safe_error(exc)})
        if fresh:
            try:
                results.append(knowledge_base.publish(root, ids=fresh))
            except (ValueError, OSError, RuntimeError) as exc:
                results.append({"status": "not_published", **safe_error(exc)})
        status = ("already_published" if not results else "published"
                  if all(value.get("status") == "published" for value in results) else "not_published")
        result["publication"] = {"status": status, "results": results}
    return result


def run(root, texts, mode=None):
    from akb_intake import intake
    settings = settings_for(root)
    if mode:
        settings["mode"] = mode
    received = intake(root, texts, settings)
    ids = list(dict.fromkeys(received["tasks"]))
    result = {"workspace": str(root), "mode": settings["mode"], "intake": received, "pending": []}
    completed = []
    for key in ids:
        task = flow.read(root, key)
        if settings["mode"] == "api" and task["stage"] not in {"main_pushed", "branch_pushed"}:
            from akb_cards import refine_task
            try:
                task = refine_task(root, key, settings)
            except (ValueError, OSError, RuntimeError) as exc:
                result["pending"].append({"id": key, **safe_error(exc)})
                continue
        if task["stage"] in {"validated", "published", "main_pushed", "branch_pushed"}:
            completed.append(key)
        else:
            result["pending"].append({"id": key, "stage": task["stage"],
                                      "source_path": task.get("source_path"),
                                      "next_step": "Agent 閱讀全文、精煉、維護索引，再 refined / finish。"})
    result["completion"] = finish(root, completed, received.get("scan_id")) if completed else None
    if result["pending"] and settings["mode"] == "agent":
        result["agent_guide"] = str(CODE / "docs" / "AGENT_GUIDE.md")
        result["notice"] = "收件完成不等於精煉完成；請目前 Agent 依指引接續，Python 不提供 Agent 推理服務。"
    return result


def completed_without_errors(result):
    if result.get("intake", {}).get("failed") or result.get("failed"):
        return False
    if any("error" in item for item in result.get("pending", [])):
        return False
    completion = result.get("completion") or result
    if completion.get("failed"):
        return False
    if isinstance(completion.get("archive"), dict) and "error" in completion["archive"]:
        return False
    publication = completion.get("publication")
    return not isinstance(publication, dict) or publication.get("status") in {"published", "already_published"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto Knowledge Base：貼連結／Markdown → 知識庫")
    parser.add_argument("--workspace", help="知識庫路徑；預設 AKB_WORKSPACE 或程式目錄下 workspace")
    commands = parser.add_subparsers(dest="command")
    for name in ("init", "doctor", "status"):
        commands.add_parser(name)
    runner = commands.add_parser("run", help="收件；Agent 接續精煉，或使用已設定 API")
    runner.add_argument("--mode", choices=("agent", "api"))
    runner.add_argument("--text", action="append", default=[], help="單個／多個連結或完整分享文字，可重複")
    finalizer = commands.add_parser("finish", help="Agent 完成 refined 後驗證、歸檔及選配發布")
    finalizer.add_argument("--id", action="append", required=True)
    finalizer.add_argument("--scan-id")
    search = commands.add_parser("search")
    search.add_argument("query")
    args = parser.parse_args(argv)
    try:
        root = workspace_path(args.workspace)
        if args.command == "init":
            result = init_workspace(root)
        elif args.command == "doctor":
            result = doctor(root)
        elif args.command == "status":
            settings_for(root)
            result = {"workspace": str(root), "tasks": flow.tasks(root)}
        elif args.command == "search":
            settings_for(root)
            result = knowledge.search_knowledge(root, args.query)
        elif args.command == "finish":
            result = finish(root, args.id, args.scan_id)
        elif args.command == "run":
            result = run(root, args.text, args.mode)
        else:
            if not sys.stdin.isatty():
                parser.print_help()
                return 2
            init_workspace(root)
            print("Auto Knowledge Base：貼入分享文字或影片連結，按 Enter。空白則只掃描 markdown。")
            result = run(root, [input("> ")])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if completed_without_errors(result) else 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps(safe_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
