"""Check the explicit source-only export, without inspecting user workspaces."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess


def check_text(text, name):
    patterns = (r"AIza[\w-]{30,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
                r"\bgithub_pat_[A-Za-z0-9_]{40,}",
                r"\bsk-proj-[A-Za-z0-9_-]{40,}",
                r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
    if any(re.search(pattern, text) for pattern in patterns):
        raise ValueError("possible_secret:" + name)


def check(root, source_tree=False):
    manifest = json.loads((root / "release-files.json").read_text(encoding="utf-8"))
    allowed = manifest["files"]
    if len(allowed) != len(set(allowed)) or not allowed:
        raise ValueError("invalid_release_manifest")
    for name in allowed:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("unsafe_release_path")
        if path.parts[0] in {"workspace", "state", "outputs", "videos", "knowledge", "markdown", "models", "vendor", "node_modules"}:
            raise ValueError("user_data_in_release")
        actual = root / path
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (actual, *actual.parents)):
            raise ValueError("linked_release_file:" + name)
        if not actual.is_file() or actual.stat().st_size > 750_000:
            raise ValueError("missing_or_large_source_file:" + name)
        check_text(actual.read_text(encoding="utf-8-sig"), name)
    if not source_tree:
        if not (root / ".git").exists():
            raise ValueError("run_from_export_repository_or_use_source_tree")
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
        tracked = set(result.stdout.decode("utf-8").rstrip("\0").split("\0"))
        if tracked != set(allowed):
            raise ValueError("tracked_files_differ_from_release_manifest")
        refs = subprocess.run(["git", "-C", str(root), "rev-list", "--all"], capture_output=True, check=True)
        for revision in refs.stdout.decode().splitlines():
            names = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", revision],
                                   capture_output=True, check=True).stdout.decode("utf-8").rstrip("\0").split("\0")
            if not set(names).issubset(set(allowed)):
                raise ValueError("unapproved_paths_in_reachable_history")
            for name in names:
                data = subprocess.run(["git", "-C", str(root), "show", revision + ":" + name],
                                      capture_output=True, check=True).stdout
                if len(data) > 750_000:
                    raise ValueError("large_blob_in_history")
                check_text(data.decode("utf-8-sig"), name)
    return {"status": "passed", "source_files": len(allowed), "history_checked": not source_tree}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(Path(__file__).resolve().parent, args.source_tree)))
