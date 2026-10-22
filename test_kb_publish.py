"""Real, offline Git integration tests; all retained fixtures live in state/."""
import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import kb_publish as publish
from kb_knowledge import card_filename


CARD = "knowledge/douyin/12345.md"
INDEX = "knowledge/README.md"


def reviewed_card(video_id="12345"):
    return f'''---
type: distilled_video_knowledge
platform: douyin
video_id: "{video_id}"
title: "測試知識卡"
author: "測試作者"
source_url: "https://www.douyin.com/video/{video_id}"
processed_at: "2026-10-22"
topics: [測試]
evidence_level: "作者主張，未獨立驗證"
content_type: tutorial
aliases: [測試別名]
---
# 測試知識卡
## 核心知識
先核對來源。
## 方法
記錄來源證據。
## 適用條件
公開影片。
## 限制
尚未獨立驗證。
## 來源時間戳
作者於 01:23 提及此方法。
'''


def reviewed_document(title="文件研究方法"):
    digest = hashlib.sha256(b"original local markdown document").hexdigest()
    return f'''---
type: distilled_document_knowledge
platform: document
document_id: "doc_{digest}"
source_sha256: "{digest}"
source_document: "local:research.md"
title: "{title}"
author: "原文作者未明"
processed_at: "2026-10-22"
topics: [文件研究]
aliases: [人工整理, research]
content_type: user_imported_markdown
evidence_level: "來源原文主張；未外部驗證"
status: completed
---
# {title}
## 核心知識
保留觀點與證據的區別。
## 方法
列出可追溯的觀點，再逐項核驗。
## 適用條件
人工 Markdown 文件整理。
## 限制
未獨立驗證原文；本卡是提煉知識，不是全文或工程驗收報告。
## 來源依據
原始檔名 research.md；SHA256：{digest}；原文章節「研究方法」。
'''


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.longpaths=true", "-c", "core.quotePath=false", "-c", "core.autocrlf=false", "-c", "core.hooksPath=",
                             "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8").strip()


def write(repo, path, data):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="publish_", dir=fixtures))
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.local = self.root / "local"
        self.origin.mkdir()
        self.seed.mkdir()
        git(self.origin, "init", "--bare", "--initial-branch=main")
        git(self.seed, "init", "--initial-branch=main")
        self.identity(self.seed)
        write(self.seed, "app.py", "print('remote version')\n")
        write(self.seed, CARD, "# Old reviewed card\n")
        write(self.seed, INDEX, "# Old reviewed index\n")
        write(self.seed, "videos/douyin/12345.md", "# Local source is not a publication candidate\n")
        git(self.seed, "add", "--", "app.py", CARD, INDEX, "videos/douyin/12345.md")
        git(self.seed, "commit", "-m", "Remote baseline")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "origin", "main")
        git(self.root, "clone", str(self.origin), str(self.local))
        self.identity(self.local)
        self.validator = patch.object(publish, "_validate")
        self.validate = self.validator.start()
        self.addCleanup(self.validator.stop)

    @staticmethod
    def identity(repo):
        git(repo, "config", "user.name", "Offline fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")

    def prepare(self, paths=None, **kwargs):
        return publish.prepare_publication(self.local, paths or [CARD, INDEX],
                                           _test_remote=str(self.origin), **kwargs)

    def push(self, receipt):
        return publish.push_publication(self.local, Path(receipt["receipt_path"]),
                                        _test_remote=str(self.origin))

    def verify(self, receipt):
        return publish.verify_publication(self.local, Path(receipt["receipt_path"]),
                                          _test_remote=str(self.origin))

    def update_cards(self):
        write(self.local, CARD, "# New reviewed card\n")
        write(self.local, INDEX, "# New reviewed index\n")

    def integrate_fixture(self, receipt):
        # Refresh the simulated other editor after a direct main publication.
        git(self.seed, "fetch", "origin", "main")
        git(self.seed, "merge", "--ff-only", "FETCH_HEAD")
        return self.verify(receipt)

    def first_published_fixture(self):
        self.update_cards()
        return self.integrate_fixture(self.push(self.prepare()))

    def assert_remote_only_knowledge(self, receipt):
        actual = git(self.origin, "diff", "--name-only", receipt["base"], "main").splitlines()
        self.assertEqual(actual, receipt["changed_paths"])
        self.assertEqual(git(self.origin, "rev-list", receipt["base"] + "..main"), receipt["sha"])
        self.assertEqual(git(self.origin, "show", "main:app.py"), "print('remote version')")
        self.assertEqual(git(self.origin, "rev-parse", "main"), receipt["sha"])
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_dirty_code_staged_code_and_local_commits_never_leave_local(self):
        write(self.local, "app.py", "print('local committed development')\n")
        git(self.local, "add", "--", "app.py")
        git(self.local, "commit", "-m", "Local development must not be published")
        write(self.local, "app.py", "print('user staged development')\n")
        git(self.local, "add", "--", "app.py")
        write(self.local, "private.txt", "password=fixture_sensitive_value\n")
        self.update_cards()
        before = (git(self.local, "rev-parse", "HEAD"), git(self.local, "branch", "--show-current"),
                  git(self.local, "diff", "--cached"), git(self.local, "status", "--porcelain"))
        receipt = self.push(self.prepare())
        after = (git(self.local, "rev-parse", "HEAD"), git(self.local, "branch", "--show-current"),
                 git(self.local, "diff", "--cached"), git(self.local, "status", "--porcelain"))
        # state is untracked in the small fixture, so ignore its single status line.
        self.assertEqual(before[:3], after[:3])
        self.assertEqual(before[3], "\n".join(line for line in after[3].splitlines() if "state/" not in line))
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(receipt["publication_mode"], "direct_main")
        self.assert_remote_only_knowledge(receipt)
        self.assertEqual(git(self.origin, "show", "-s", "--format=%an|%ae", receipt["sha"]),
                         "Offline fixture|fixture@example.invalid")
        self.assertNotIn("private.txt", git(self.origin, "ls-tree", "-r", "--name-only", "main"))

    def test_dry_run_stages_and_scans_but_does_not_commit_or_push(self):
        self.update_cards()
        receipt = self.prepare(dry_run=True)
        checkout = Path(receipt["workspace"])
        self.assertEqual(receipt["status"], "dry_run")
        self.assertIsNone(receipt["sha"])
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), receipt["base"])
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")
        with self.assertRaisesRegex(RuntimeError, "dry-run"):
            self.push(receipt)

    def test_different_remote_card_stops_without_overwrite(self):
        self.update_cards()
        write(self.seed, CARD, "# Different remote author version\n")
        git(self.seed, "add", "--", CARD)
        git(self.seed, "commit", "-m", "Another editor")
        git(self.seed, "push", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "分歧"):
            self.prepare()
        self.assertEqual(git(self.origin, "show", "main:" + CARD), "# Different remote author version")
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_remote_index_conflict_is_not_silently_regenerated(self):
        self.update_cards()
        write(self.seed, INDEX, "# Other editor index\n")
        git(self.seed, "add", "--", INDEX)
        git(self.seed, "commit", "-m", "Another index")
        git(self.seed, "push", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "分歧"):
            self.prepare()

    def test_remote_already_has_identical_bytes_is_idempotent(self):
        self.update_cards()
        write(self.seed, CARD, "# New reviewed card\n")
        write(self.seed, INDEX, "# New reviewed index\n")
        git(self.seed, "add", "--", CARD, INDEX)
        git(self.seed, "commit", "-m", "Equivalent prior publication")
        git(self.seed, "push", "origin", "main")
        receipt = self.prepare()
        self.assertEqual(receipt["status"], "unchanged")
        self.assertEqual(receipt["changed_paths"], [])
        self.assertEqual(self.verify(receipt)["status"], "published")

    def test_retry_reuses_sha_and_published_requires_main_readback(self):
        self.update_cards()
        receipt = self.prepare()
        real_git = publish._git
        def fail_push(repo, *args, **kwargs):
            if "push" in args:
                raise RuntimeError("Git push 失敗（exit 1）；本機收據保留")
            return real_git(repo, *args, **kwargs)
        with patch.object(publish, "_git", side_effect=fail_push):
            with self.assertRaisesRegex(RuntimeError, "push"):
                self.push(receipt)
        failed = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "push_failed")
        self.assertEqual(failed["sha"], receipt["sha"])
        pushed = self.push(receipt)
        self.assertEqual(pushed["attempts"], 2)
        self.assertEqual(pushed["status"], "published")
        with patch.object(publish, "_git", side_effect=fail_push):
            again = self.push(receipt)
        self.assertEqual(again["sha"], receipt["sha"])
        self.assertEqual(again["attempts"], 2)
        self.assert_remote_only_knowledge(again)
        verified = self.verify(receipt)
        self.assertEqual(verified["status"], "published")
        self.assertEqual(verified["published_sha"], git(self.origin, "rev-parse", "main"))

    def test_post_push_readback_failure_keeps_main_pushed_and_retry_does_not_push(self):
        self.update_cards()
        receipt = self.prepare()
        real_git = publish._git
        fetches = 0
        pushes = []
        def fail_readback(repo, *args, **kwargs):
            nonlocal fetches
            if args[0] == "fetch":
                fetches += 1
                if fetches == 2:
                    raise RuntimeError("Git fetch 失敗；本機收據保留")
            if "push" in args:
                pushes.append(args)
            return real_git(repo, *args, **kwargs)
        with patch.object(publish, "_git", side_effect=fail_readback):
            with self.assertRaisesRegex(RuntimeError, "fetch"):
                self.push(receipt)
        saved = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "main_pushed")
        self.assertEqual(git(self.origin, "rev-parse", "main"), receipt["sha"])
        self.assertEqual(len(pushes), 1)
        def forbid_push(repo, *args, **kwargs):
            if "push" in args:
                self.fail("Retry must only read back the already-pushed main")
            return real_git(repo, *args, **kwargs)
        with patch.object(publish, "_git", side_effect=forbid_push):
            retried = self.push(receipt)
        self.assertEqual(retried["status"], "published")
        self.assertEqual(retried["attempts"], 1)
        self.assertEqual(retried["sha"], receipt["sha"])
        self.assert_remote_only_knowledge(retried)

    def test_push_response_lost_after_server_acceptance_is_recovered_without_repush(self):
        self.update_cards()
        receipt = self.prepare()
        real_git = publish._git
        def lose_response(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if "push" in args:
                raise RuntimeError("Git push 回應遺失；本機收據保留")
            return result
        with patch.object(publish, "_git", side_effect=lose_response):
            with self.assertRaisesRegex(RuntimeError, "回應遺失"):
                self.push(receipt)
        saved = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "push_failed")
        self.assertEqual(git(self.origin, "rev-parse", "main"), receipt["sha"])
        def forbid_push(repo, *args, **kwargs):
            if "push" in args:
                self.fail("An accepted commit cannot be pushed again on retry")
            return real_git(repo, *args, **kwargs)
        with patch.object(publish, "_git", side_effect=forbid_push):
            result = self.push(receipt)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["attempts"], 1)
        self.assert_remote_only_knowledge(result)

    def test_race_after_fetch_is_rejected_by_non_fast_forward_push(self):
        self.update_cards()
        receipt = self.prepare()
        real_git = publish._git
        raced_sha = None
        def advance_before_push(repo, *args, **kwargs):
            nonlocal raced_sha
            if "push" in args:
                self.assertEqual(args[-1], receipt["sha"] + ":refs/heads/main")
                self.assertFalse(any(arg.startswith("--force") or arg.startswith("+") for arg in args))
                write(self.seed, "remote-note.md", "# Concurrent remote work\n")
                git(self.seed, "add", "--", "remote-note.md")
                git(self.seed, "commit", "-m", "Remote advances after preflight")
                git(self.seed, "push", "origin", "main")
                raced_sha = git(self.seed, "rev-parse", "HEAD")
            return real_git(repo, *args, **kwargs)
        with patch.object(publish, "_git", side_effect=advance_before_push):
            with self.assertRaises(RuntimeError):
                self.push(receipt)
        self.assertEqual(git(self.origin, "rev-parse", "main"), raced_sha)
        self.assertEqual(git(self.origin, "show", "main:" + INDEX), "# Old reviewed index")
        with self.assertRaisesRegex(RuntimeError, "main 已變動.*重新 prepare"):
            self.push(receipt)
        saved = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "conflict")
        self.assertEqual(saved["sha"], receipt["sha"])

    def test_same_file_remote_change_after_prepare_stops_retry(self):
        self.update_cards()
        receipt = self.prepare()
        write(self.seed, CARD, "# Concurrent remote edit\n")
        git(self.seed, "add", "--", CARD)
        git(self.seed, "commit", "-m", "Concurrent editor")
        git(self.seed, "push", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "同檔已變更"):
            self.push(receipt)
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_unrelated_remote_advance_after_prepare_requires_reprepare(self):
        self.update_cards()
        receipt = self.prepare()
        write(self.seed, "remote-note.md", "# Another remote file\n")
        git(self.seed, "add", "--", "remote-note.md")
        git(self.seed, "commit", "-m", "Independent change")
        git(self.seed, "push", "origin", "main")
        with self.assertRaisesRegex(RuntimeError, "main 已變動.*重新 prepare"):
            self.push(receipt)
        self.assertEqual(git(self.origin, "rev-parse", "main"), git(self.seed, "rev-parse", "HEAD"))
        self.assertEqual(git(self.origin, "show", "main:" + INDEX), "# Old reviewed index")

    def test_receipt_checkout_tampering_prevents_push(self):
        self.update_cards()
        receipt = self.prepare()
        write(Path(receipt["workspace"]), "app.py", "print('tampered')\n")
        with self.assertRaisesRegex(RuntimeError, "已變動"):
            self.push(receipt)

    def test_legacy_pr_receipt_cannot_authorize_direct_main_push(self):
        self.update_cards()
        receipt = self.prepare()
        receipt.update(version=1, status="prepared")
        receipt.pop("publication_mode")
        receipt.pop("target_ref")
        Path(receipt["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "舊 PR.*重新 prepare"):
            self.push(receipt)
        self.assertEqual(git(self.origin, "rev-parse", "main"), receipt["base"])
        self.assertEqual(self.verify(receipt)["status"], "prepared")

    def test_legacy_pr_receipt_can_still_verify_a_manual_merge(self):
        self.update_cards()
        receipt = self.prepare()
        receipt.update(version=1, status="branch_pushed")
        receipt.pop("publication_mode")
        receipt.pop("target_ref")
        Path(receipt["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
        checkout = Path(receipt["workspace"])
        git(checkout, "push", "origin", receipt["sha"] + ":refs/heads/" + receipt["branch"])
        self.assertEqual(self.verify(receipt)["status"], "branch_pushed")
        git(self.seed, "fetch", "origin", receipt["branch"])
        git(self.seed, "merge", "--ff-only", "FETCH_HEAD")
        git(self.seed, "push", "origin", "main")
        self.assertEqual(self.verify(receipt)["status"], "published")
        with self.assertRaisesRegex(RuntimeError, "舊 PR.*重新 prepare"):
            self.push(receipt)

    def test_direct_receipt_cannot_change_its_target_ref(self):
        self.update_cards()
        receipt = self.prepare()
        receipt["target_ref"] = "refs/heads/other"
        Path(receipt["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "發布模式不符"):
            self.push(receipt)
        self.assertEqual(git(self.origin, "rev-parse", "main"), receipt["base"])

    def test_rejects_sources_reports_duplicates_and_path_escape(self):
        for paths in (["videos/douyin/12345.md"], ["PROGRESS.md"], ["knowledge/../app.md"],
                      ["knowledge/report.md"], [CARD, CARD], ["knowledge/douyin/../../app.md"],
                      ["knowledge/douyin/12345.md:extra"], ["knowledge\\douyin\\12345.md"]):
            with self.subTest(paths=paths), self.assertRaises(RuntimeError):
                self.prepare(paths)

    def test_secrets_in_selected_bytes_are_rejected(self):
        write(self.local, CARD, "# Card\npassword=fixture_sensitive_value\n")
        with self.assertRaisesRegex(RuntimeError, "憑證"):
            self.prepare()

    def test_bare_google_api_key_in_selected_bytes_is_rejected(self):
        synthetic = "AIza" + "A" * 35
        write(self.local, CARD, "# Card\n" + synthetic + "\n")
        with self.assertRaisesRegex(RuntimeError, "憑證"):
            self.prepare()
        self.assertFalse((self.local / "state" / "publish").exists())

    def test_unsafe_existing_remote_history_blocks_new_branch(self):
        write(self.seed, "outputs/raw.md", "# Disallowed raw output\n")
        git(self.seed, "add", "--", "outputs/raw.md")
        git(self.seed, "commit", "-m", "Unsafe legacy data")
        git(self.seed, "push", "origin", "main")
        self.update_cards()
        with self.assertRaisesRegex(RuntimeError, "可達歷史含本機資料"):
            self.prepare()

    def test_validator_failure_prevents_checkout_and_commit(self):
        self.validate.side_effect = ValueError("知識卡未完成")
        self.update_cards()
        with self.assertRaisesRegex(ValueError, "未完成"):
            self.prepare()
        self.assertFalse((self.local / "state" / "publish").exists())

    def test_private_confirmation_and_exact_remote_are_required(self):
        with self.assertRaisesRegex(RuntimeError, "停用"):
            publish.prepare_publication(self.local, [CARD])
        write(self.local, "akb.json", json.dumps({"publish": {"enabled": True, "repository": "fixture/private-knowledge"}}))
        with self.assertRaisesRegex(RuntimeError, "private"):
            publish.prepare_publication(self.local, [CARD])
        with self.assertRaisesRegex(RuntimeError, "目的地"):
            publish.prepare_publication(self.local, [CARD], private_confirmed="https://github.com/other/repo.git")

    def test_new_card_untracked_locally_can_be_added_without_other_files(self):
        new = "knowledge/youtube/youtube_example123.md"
        write(self.local, new, "# Completed reviewed new card\n")
        receipt = self.push(self.prepare([new]))
        self.assertEqual(receipt["changed_paths"], [new])
        self.assertEqual(git(self.origin, "show", "main:" + new), "# Completed reviewed new card")

    def test_reverted_development_commit_still_cannot_be_hidden_in_history(self):
        self.update_cards()
        receipt = self.prepare()
        checkout = Path(receipt["workspace"])
        self.identity(checkout)
        write(checkout, "app.py", "print('outgoing code')\n")
        git(checkout, "add", "--", "app.py")
        git(checkout, "commit", "-m", "Unapproved development")
        write(checkout, "app.py", "print('remote version')\n")
        git(checkout, "add", "--", "app.py")
        git(checkout, "commit", "-m", "Revert final bytes only")
        receipt["sha"] = git(checkout, "rev-parse", "HEAD")
        Path(receipt["receipt_path"]).write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "outgoing commit"):
            self.push(receipt)

    def test_secret_in_old_remote_blob_blocks_even_after_clean_final_bytes(self):
        write(self.seed, "legacy.md", "password=fixture_sensitive_value\n")
        git(self.seed, "add", "--", "legacy.md")
        git(self.seed, "commit", "-m", "Unsafe ancestor")
        write(self.seed, "legacy.md", "# Current file has no secret\n")
        git(self.seed, "add", "--", "legacy.md")
        git(self.seed, "commit", "-m", "Clean final bytes")
        git(self.seed, "push", "origin", "main")
        self.update_cards()
        with self.assertRaisesRegex(RuntimeError, "可達歷史疑似含憑證"):
            self.prepare()

    def test_bare_google_api_key_in_old_remote_blob_is_rejected(self):
        synthetic = "AIza" + "A" * 35
        write(self.seed, "legacy.md", synthetic + "\n")
        git(self.seed, "add", "--", "legacy.md")
        git(self.seed, "commit", "-m", "Synthetic unsafe ancestor")
        write(self.seed, "legacy.md", "# Current file has no key\n")
        git(self.seed, "add", "--", "legacy.md")
        git(self.seed, "commit", "-m", "Clean current bytes")
        git(self.seed, "push", "origin", "main")
        self.update_cards()
        with self.assertRaisesRegex(RuntimeError, "可達歷史疑似含憑證"):
            self.prepare()
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_git_symlink_mode_in_baseline_is_rejected(self):
        oid = git(self.local, "rev-parse", "HEAD:" + CARD)
        git(self.local, "update-index", "--cacheinfo", "120000," + oid + "," + CARD)
        git(self.local, "commit", "-m", "Local symlink entry")
        self.update_cards()
        with self.assertRaisesRegex(RuntimeError, "一般 Markdown"):
            self.prepare()

    def test_git_failure_does_not_expose_credential_url(self):
        from types import SimpleNamespace
        secret_url = b"https://credential-placeholder@example.invalid/repo.git"
        with patch.object(publish.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=secret_url, stderr=secret_url)):
            with self.assertRaises(RuntimeError) as raised:
                publish._git(self.local, "fetch", "origin")
        self.assertNotIn("credential-placeholder", str(raised.exception))

    def test_real_validator_and_publisher_preserve_exact_reviewed_bytes(self):
        self.validator.stop()
        write(self.local, CARD, reviewed_card())
        write(self.local, INDEX, "# 索引\n[卡片](douyin/12345.md)\n")
        receipt = self.push(self.prepare())
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(subprocess.run(["git", "-C", str(self.origin), "show",
                                         "main:" + CARD], capture_output=True, check=True).stdout,
                         (self.local / CARD).read_bytes())

    def valid_remote_baseline(self):
        self.validator.stop()
        write(self.seed, CARD, reviewed_card())
        write(self.seed, INDEX, "# 索引\n[既有卡片](douyin/12345.md)\n")
        git(self.seed, "add", "--", CARD, INDEX)
        git(self.seed, "commit", "-m", "Valid legacy knowledge baseline")
        git(self.seed, "push", "origin", "main")
        git(self.local, "fetch", "origin", "main")
        git(self.local, "merge", "--ff-only", "FETCH_HEAD")

    def test_document_card_and_index_publish_without_original_or_development_code(self):
        self.valid_remote_baseline()
        digest = hashlib.sha256(b"original local markdown document").hexdigest()
        name = card_filename("文件研究方法", "document", "doc_" + digest)
        target = "knowledge/cards/" + name
        write(self.local, target, reviewed_document())
        write(self.local, INDEX, "# 索引\n[既有卡片](douyin/12345.md)\n" + f"[文件](cards/{name})\n")
        write(self.local, "markdown/research.md", "# Original full document\nNever publish raw text.\n")
        write(self.local, "app.py", "print('local code must stay local')\n")
        git(self.local, "add", "--", "app.py")
        before = (git(self.local, "rev-parse", "HEAD"), git(self.local, "diff", "--cached"))
        receipt = self.push(self.prepare([target, INDEX]))
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(receipt["publication_mode"], "direct_main")
        self.assertEqual(set(receipt["changed_paths"]), {target, INDEX})
        self.assert_remote_only_knowledge(receipt)
        self.assertEqual(before, (git(self.local, "rev-parse", "HEAD"), git(self.local, "diff", "--cached")))
        tree = git(self.origin, "ls-tree", "-r", "--name-only", "main")
        self.assertNotIn("markdown/research.md", tree)
        self.assertEqual(subprocess.run(["git", "-C", str(self.origin), "show", "main:" + target],
                                       capture_output=True, check=True).stdout, (self.local / target).read_bytes())

    def test_named_video_card_and_legacy_card_coexist(self):
        self.valid_remote_baseline()
        name = card_filename("測試知識卡", "douyin", "67890")
        target = "knowledge/cards/" + name
        write(self.local, target, reviewed_card("67890"))
        write(self.local, INDEX, "# 索引\n[既有卡片](douyin/12345.md)\n" + f"[影片](cards/{name})\n")
        receipt = self.push(self.prepare([target, INDEX]))
        self.assertEqual(receipt["status"], "published")
        self.assertEqual(set(receipt["changed_paths"]), {target, INDEX})
        self.assert_remote_only_knowledge(receipt)
        self.assertIn(CARD, git(self.origin, "ls-tree", "-r", "--name-only", "main"))

    def test_named_document_path_does_not_allow_raw_text_or_engineering_reports(self):
        self.valid_remote_baseline()
        digest = hashlib.sha256(b"original local markdown document").hexdigest()
        name = card_filename("文件研究方法", "document", "doc_" + digest)
        target = "knowledge/cards/" + name
        write(self.local, INDEX, "# 索引\n[既有卡片](douyin/12345.md)\n" + f"[文件](cards/{name})\n")
        original_sha = git(self.origin, "rev-parse", "main")
        for body in ("# Raw original\nNot distilled.\n", "# Engineering report\nPASS\n",
                     reviewed_document().replace("content_type: user_imported_markdown", "content_type: engineering_report")):
            write(self.local, target, body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.prepare([target, INDEX])
            self.assertEqual(git(self.origin, "rev-parse", "main"), original_sha)

    def test_assembled_tree_rejects_index_link_to_an_unselected_local_card(self):
        self.validator.stop()
        write(self.local, CARD, reviewed_card())
        write(self.local, "knowledge/douyin/67890.md", reviewed_card("67890"))
        write(self.local, INDEX, "# 索引\n[A](douyin/12345.md)\n[B](douyin/67890.md)\n")
        with self.assertRaises(ValueError):
            self.prepare([CARD, INDEX])
        self.assertEqual(git(self.origin, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_second_publication_reuses_verified_index_without_touching_local_git(self):
        write(self.local, "app.py", "print('unrelated local commit')\n")
        git(self.local, "add", "--", "app.py")
        git(self.local, "commit", "-m", "Unrelated local development")
        write(self.local, "app.py", "print('staged local work')\n")
        git(self.local, "add", "--", "app.py")
        original = (git(self.local, "rev-parse", "HEAD"), git(self.local, "branch", "--show-current"),
                    git(self.local, "diff", "--cached"))
        first = self.first_published_fixture()
        self.assertEqual(first["status"], "published")
        first_bytes = Path(first["receipt_path"]).read_bytes()
        new_card = "knowledge/youtube/youtube_second123.md"
        write(self.local, new_card, "# Second reviewed card\n")
        write(self.local, INDEX, "# Second reviewed index\n")
        second = self.push(self.prepare([new_card, INDEX]))
        self.assertEqual(second["baseline_sources"][INDEX]["kind"], "published_receipt")
        self.assertEqual(second["baseline_sources"][INDEX]["sha"], first["published_sha"])
        self.assertEqual(second["baseline_hashes"][INDEX], first["hashes"][INDEX])
        self.assertNotEqual(second["local_head_hashes"][INDEX], second["baseline_hashes"][INDEX])
        self.assertEqual(second["base"], first["published_sha"])
        self.assert_remote_only_knowledge(second)
        self.assertEqual(self.integrate_fixture(second)["status"], "published")
        self.assertEqual(Path(first["receipt_path"]).read_bytes(), first_bytes)
        self.assertEqual(original, (git(self.local, "rev-parse", "HEAD"),
                                    git(self.local, "branch", "--show-current"),
                                    git(self.local, "diff", "--cached")))

    def test_other_editor_index_after_published_baseline_still_conflicts(self):
        self.first_published_fixture()
        write(self.seed, INDEX, "# Concurrent editor's index\n")
        git(self.seed, "add", "--", INDEX)
        git(self.seed, "commit", "-m", "Concurrent index edit")
        git(self.seed, "push", "origin", "main")
        write(self.local, INDEX, "# Next reviewed index\n")
        with self.assertRaisesRegex(RuntimeError, "分歧"):
            self.prepare()
        self.assertEqual(git(self.origin, "show", "main:" + INDEX), "# Concurrent editor's index")

    def test_external_revert_to_old_local_head_does_not_override_published_baseline(self):
        self.first_published_fixture()
        write(self.seed, INDEX, "# Old reviewed index\n")
        git(self.seed, "add", "--", INDEX)
        git(self.seed, "commit", "-m", "Other editor reverts index")
        git(self.seed, "push", "origin", "main")
        write(self.local, INDEX, "# Next reviewed index\n")
        with self.assertRaisesRegex(RuntimeError, "分歧"):
            self.prepare()

    def test_invalid_published_receipt_cannot_authorize_new_index(self):
        first = self.first_published_fixture()
        write(self.local, INDEX, "# Next reviewed index\n")
        for kind in ("hash", "ancestor", "status"):
            invalid = json.loads(json.dumps(first))
            if kind == "hash":
                invalid["hashes"][INDEX] = "0" * 64
            elif kind == "ancestor":
                invalid["published_sha"] = "f" * 40
            else:
                invalid["status"] = "branch_pushed"
            Path(first["receipt_path"]).write_text(json.dumps(invalid), encoding="utf-8")
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "分歧"):
                self.prepare()

    def test_latest_verified_publication_wins_over_older_receipt(self):
        first = self.first_published_fixture()
        write(self.local, INDEX, "# Second reviewed index\n")
        second = self.integrate_fixture(self.push(self.prepare()))
        write(self.local, INDEX, "# Third reviewed index\n")
        third = self.prepare()
        self.assertNotEqual(first["published_sha"], second["published_sha"])
        self.assertEqual(third["baseline_sources"][INDEX]["sha"], second["published_sha"])
        self.assertEqual(third["baseline_hashes"][INDEX], second["hashes"][INDEX])


    def test_plain_workspace_never_reads_or_publishes_code_repo_head(self):
        target = self.root / "fresh-target.git"
        seed = self.root / "fresh-seed"
        workspace = self.root / "plain-workspace"
        target.mkdir(); seed.mkdir(); workspace.mkdir()
        git(target, "init", "--bare", "--initial-branch=main")
        git(seed, "init", "--initial-branch=main")
        self.identity(seed)
        write(seed, "README.md", "# Existing private knowledge target\n")
        git(seed, "add", "--", "README.md")
        git(seed, "commit", "-m", "Existing target")
        git(seed, "remote", "add", "origin", str(target))
        git(seed, "push", "origin", "main")
        write(workspace, CARD, "# Reviewed new card\n")
        write(workspace, INDEX, "# Reviewed new index\n")
        def authorized_remote(repo, confirmed, test_remote, expected_remote=None):
            if expected_remote is not None:
                self.assertEqual(git(repo, "remote", "get-url", "origin"), expected_remote)
            return str(target)
        with patch.object(publish, "_remote", side_effect=authorized_remote), patch.object(
                publish, "_identity", return_value=("Offline fixture", "fixture@example.invalid")):
            receipt = publish.prepare_publication(workspace, [CARD, INDEX], private_confirmed=True)
            self.assertIsNone(receipt["local_head"])
            self.assertEqual(receipt["baseline_sources"][INDEX]["kind"], "unestablished")
            result = publish.push_publication(workspace, Path(receipt["receipt_path"]), private_confirmed=True)
        self.assertEqual(result["status"], "published")
        self.assertFalse((workspace / ".git").exists())
        self.assertEqual(git(target, "show", "main:README.md"), "# Existing private knowledge target")
        self.assertEqual(git(target, "diff", "--name-only", receipt["base"], "main").splitlines(), sorted([CARD, INDEX]))

    def test_unestablished_plain_workspace_cannot_overwrite_remote_index(self):
        workspace = self.root / "plain-conflict"
        workspace.mkdir()
        write(workspace, CARD, "# New card\n")
        write(workspace, INDEX, "# Unknown replacement index\n")
        with patch.object(publish, "_remote", return_value=str(self.origin)), patch.object(
                publish, "_identity", return_value=("Offline fixture", "fixture@example.invalid")):
            with self.assertRaisesRegex(RuntimeError, "分歧"):
                publish.prepare_publication(workspace, [CARD, INDEX], private_confirmed=True)
        self.assertEqual(git(self.origin, "show", "main:" + INDEX), "# Old reviewed index")


class IdentityTests(unittest.TestCase):
    def test_named_cards_allowlist_stays_narrow(self):
        valid = "knowledge/cards/" + card_filename("人工知識", "document", "doc_" + "a" * 64)
        self.assertEqual(publish._paths([valid, INDEX]), sorted([valid, INDEX]))
        for invalid in ("knowledge/cards/README.md", "knowledge/cards/AGENTS.md", "knowledge/cards/report.md",
                        "knowledge/douyin/README.md", "knowledge/youtube/AGENTS.md",
                        "knowledge/another/report.md", "knowledge/cards/A__unknown__id.md",
                        "knowledge/cards/sub/A__document__doc_" + "a" * 64 + ".md",
                        "markdown/research.md", "outputs/imported-text/doc.md", "kb_knowledge.py"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                publish._paths([invalid])

    def test_missing_original_git_identity_is_rejected(self):
        for values in ((b"", b"fixture@example.invalid\n"), (b"Offline fixture\n", b"")):
            with self.subTest(values=values), patch.object(publish, "_git", side_effect=values):
                with self.assertRaisesRegex(RuntimeError, "user.name.*user.email"):
                    publish._identity(Path("unused-offline-fixture"))


class TargetConfigurationTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).resolve().parent / "state/test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="target-config-", dir=fixtures))

    def write_config(self, target):
        write(self.workspace, "akb.json", json.dumps({"publish": target}))

    def test_missing_or_disabled_target_never_runs_git(self):
        with patch.object(publish, "_git", side_effect=AssertionError("no Git without opt-in")):
            with self.assertRaisesRegex(RuntimeError, "停用"):
                publish._remote(self.workspace, True, None)
            self.write_config({"enabled": False, "repository": "fixture/knowledge"})
            with self.assertRaisesRegex(RuntimeError, "停用"):
                publish._remote(self.workspace, True, None)

    def test_target_is_not_inferred_from_workspace_origin(self):
        self.write_config({"enabled": True, "repository": "example/private-knowledge", "branch": "main"})
        with patch.object(publish, "_git", side_effect=AssertionError("workspace need not be Git")):
            self.assertEqual(publish._remote(self.workspace, "https://github.com/example/private-knowledge.git", None), "https://github.com/example/private-knowledge.git")
            with self.assertRaisesRegex(RuntimeError, "private"):
                publish._remote(self.workspace, False, None)

    def test_private_confirmation_is_bound_to_exact_configured_repository(self):
        self.write_config({"enabled": True, "repository": "example/first"})
        confirmed = "https://github.com/example/first.git"
        self.assertEqual(publish._remote(self.workspace, confirmed, None), confirmed)
        self.write_config({"enabled": True, "repository": "example/second"})
        with patch.object(publish, "_git", side_effect=AssertionError("no Git on changed target")):
            with self.assertRaisesRegex(RuntimeError, "目的地"):
                publish._remote(self.workspace, confirmed, None)

    def test_url_credentials_invalid_owner_and_non_main_branch_are_rejected(self):
        for repository in ("https://github.com/example/repo", "owner/token@github.com", "../repo", "owner/..", "owner/repo.git", "owner/repo\n"):
            self.write_config({"enabled": True, "repository": repository})
            with self.subTest(repository=repository), self.assertRaisesRegex(RuntimeError, "repository"):
                publish.publish_target(self.workspace)
        self.write_config({"enabled": True, "repository": "owner/repo", "branch": "other"})
        with self.assertRaisesRegex(RuntimeError, "main"):
            publish.publish_target(self.workspace)


if __name__ == "__main__":
    unittest.main()
