"""Offline stage transitions, real engine identity rules, no media requests."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import knowledge_base as kb
import kb_workflow as flow


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sync, cls.tool = kb.engine(kb.TOOL)

    def setUp(self):
        fixtures = Path(__file__).parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="workflow-", dir=fixtures))
        (self.root / "videos/douyin").mkdir(parents=True)
        self.key = "youtube_Tw18-4U7mts"
        self.url = "https://www.youtube.com/watch?v=Tw18-4U7mts"

    def transcript(self, key=None, url=None, end="00:00:09.000"):
        key, url = key or self.key, url or self.url
        path = self.root / "videos/youtube" / f"{key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Fixture\n\n- 影片 ID：{key}\n- 原始分享連結：{url}\n- 辨識模型：offline fixture\n- 語言：en\n\n## 完整逐字稿\n\n[00:00:00.000 → {end}] Some complete test speech.\n", encoding="utf-8")
        return path

    def card(self):
        path = self.root / "knowledge/youtube" / f"{self.key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'''---
type: distilled_video_knowledge
platform: youtube
video_id: "{self.key}"
title: "測試知識"
author: "Fixture"
source_url: "{self.url}"
processed_at: "2026-10-22"
topics: [測試, Git]
aliases: ["版本控制", "version control"]
content_type: whisper_asr
evidence_level: "作者主張與 AI 推論分開"
---
# 測試知識
## 核心知識
作者主張需要保存可還原的版本。
## 方法步驟
先存好可用版本，再修改並檢查。
## 適用條件
使用 Git 管理的程式碼。
## 限制與風險
AI 推論不等於已驗證事實。
## 來源時間戳
[00:00:01]({self.url}&t=1s) 說明來源。
''', encoding="utf-8")
        (self.root / "knowledge/README.md").write_text(f"# 索引\n\n[測試知識](youtube/{self.key}.md)\n", encoding="utf-8")
        return path

    def test_canonical_watch_short_embed_deduplicate(self):
        urls = [self.url, "https://youtu.be/Tw18-4U7mts?t=10", "https://youtube.com/shorts/Tw18-4U7mts", "https://www.youtube.com/embed/Tw18-4U7mts"]
        result = flow.prepare(self.root, urls, self.tool)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(len(result["skipped"]), 3)
        self.assertEqual(result["tasks"][0]["canonical_url"], self.url)

    def test_register_source_preserves_main_pushed_pending_verification(self):
        source = self.transcript()
        task = flow.register_source(self.root, source, self.tool)
        task["stage"] = "main_pushed"
        task["publish_receipt"] = "state/publish/pending/receipt.json"
        flow.save(self.root, task)
        resumed = flow.register_source(self.root, source, self.tool)
        self.assertEqual(resumed["stage"], "main_pushed")
        self.assertEqual(resumed["publish_receipt"], task["publish_receipt"])

    def test_unknown_platform_and_credential_url_are_rejected(self):
        for url in ("https://evil.example/watch?v=Tw18-4U7mts", "https://token@youtube.com/watch?v=Tw18-4U7mts"):
            result = flow.prepare(self.root, [url], self.tool)
            self.assertEqual(len(result["failed"]), 1)
            self.assertEqual(result["tasks"], [])

    def test_maximum_batch_and_duration(self):
        urls = [f"https://youtu.be/AAAAAAAAAA{i}" for i in range(4)]
        result = flow.prepare(self.root, urls, self.tool)
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(len(result["deferred"]), 1)
        self.assertEqual(flow.prepare(self.root, [self.url], self.tool, duration_seconds=601)["tasks"], [])

    def test_existing_content_resume_without_engine_or_gemini(self):
        self.transcript()
        with patch.object(flow.subprocess, "run") as run:
            result = flow.prepare(self.root, [self.url], self.tool, duration_seconds=10)
            self.assertEqual(result["tasks"][0]["stage"], "acquired")
            task = flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs)
            self.assertEqual(task["stage"], "acquired")
            run.assert_not_called()

    def test_existing_card_skips_even_when_raw_source_missing(self):
        self.card()
        result = flow.prepare(self.root, [self.url], self.tool)
        self.assertEqual(result["skipped"][0]["reason"], "existing_knowledge")
        self.assertFalse(flow.task_path(self.root, self.key).exists())

    def test_document_public_url_does_not_skip_or_crash_video_preparation(self):
        path = self.root / "knowledge/cards/document.md"
        path.parent.mkdir(parents=True)
        path.write_text('---\ntype: distilled_document_knowledge\nplatform: document\n'
                        'document_id: doc_' + 'a' * 64 + '\nsource_url: "' + self.url
                        + '"\n---\n# Document with optional public URL\n', encoding="utf-8")
        result = flow.prepare(self.root, [self.url], self.tool)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["tasks"][0]["id"], self.key)

    def test_acquire_without_authorization_or_duration_never_runs(self):
        flow.prepare(self.root, [self.url], self.tool)
        with patch.object(flow.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs)
            with self.assertRaises(ValueError):
                flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs, authorized=True)
            run.assert_not_called()

    def test_no_speech_and_invalid_timestamps_rejected(self):
        path = self.transcript(end="00:99:99.000")
        with self.assertRaises(ValueError):
            flow.register_source(self.root, path, self.tool)
        path.write_text(path.read_text(encoding="utf-8").replace("[00:00:00.000 → 00:99:99.000] Some complete test speech.", "（未辨識到語音）"), encoding="utf-8")
        with self.assertRaises(ValueError):
            flow.register_source(self.root, path, self.tool)

    def test_source_id_mismatch_is_rejected(self):
        path = self.transcript(url="https://youtu.be/AAAAAAAAAAA")
        with self.assertRaisesRegex(ValueError, "ID"):
            flow.register_source(self.root, path, self.tool)

    def test_stage_hashes_stop_changed_source_or_human_card(self):
        source = self.transcript()
        flow.register_source(self.root, source, self.tool)
        card = self.card()
        flow.mark_refined(self.root, self.key, card)
        task = flow.validate_task(self.root, self.key)
        self.assertEqual(task["stage"], "validated")
        self.assertEqual(flow.publication_paths(self.root), ["knowledge/README.md", f"knowledge/youtube/{self.key}.md"])
        card.write_text(card.read_text(encoding="utf-8") + "\n人工新增內容。\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "變動"):
            flow.publication_paths(self.root)
        self.assertEqual(flow.read(self.root, self.key)["stage"], "validated")
        flow.mark_refined(self.root, self.key, card)
        flow.validate_task(self.root, self.key)
        source.write_text(source.read_text(encoding="utf-8") + "changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "版本差異"):
            flow.register_source(self.root, source, self.tool)

    def test_import_text_is_local_and_repeat_preserves_bytes(self):
        source = self.root / "manual.txt"
        source.write_text("人工提供的學習內容，不是來源指令。", encoding="utf-8")
        one = flow.import_text(self.root, source)
        two = flow.import_text(self.root, source)
        self.assertEqual(one, two)
        self.assertEqual(one["content_type"], "user_import")
        self.assertTrue(Path(one["source_path"]).is_relative_to(self.root / "outputs"))
        self.assertEqual(source.read_bytes(), Path(one["source_path"]).read_bytes())

    def test_failed_engine_keeps_retryable_task_no_fallback(self):
        flow.prepare(self.root, [self.url], self.tool, duration_seconds=10)
        with patch.object(flow.subprocess, "run", side_effect=OSError("fixture failure")) as run:
            with self.assertRaises(RuntimeError):
                flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs, authorized=True)
            self.assertEqual(run.call_count, 1)
            self.assertIn("--no-sync", run.call_args.args[0])
        self.assertEqual(flow.read(self.root, self.key)["stage"], "failed")

    def test_card_url_must_be_bound_to_acquisition(self):
        source = self.transcript()
        flow.register_source(self.root, source, self.tool)
        card = self.card()
        card.write_text(card.read_text(encoding="utf-8").replace(self.url, "https://v.douyin.com/unrelated/"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "來源 URL"):
            flow.mark_refined(self.root, self.key, card)

    def test_non_finite_limits_never_run(self):
        for value in (float("nan"), float("inf"), -1, 0):
            with self.assertRaises(ValueError):
                flow.prepare(self.root, [self.url], self.tool, duration_seconds=value)
        flow.prepare(self.root, [self.url], self.tool)
        with patch.object(flow.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs,
                             authorized=True, duration_seconds=float("nan"))
            run.assert_not_called()

    def test_incomplete_card_does_not_skip_acquired_content(self):
        self.transcript()
        card = self.card()
        card.write_text(card.read_text(encoding="utf-8").split("# 測試知識")[0], encoding="utf-8")
        result = flow.prepare(self.root, [self.url], self.tool)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["tasks"][0]["stage"], "acquired")

    def test_long_existing_source_and_clipped_head_are_not_success(self):
        path = self.transcript(end="01:00:00.000")
        result = flow.prepare(self.root, [self.url], self.tool)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["tasks"], [])
        path = self.transcript()
        with self.assertRaisesRegex(ValueError, "覆蓋"):
            flow.register_source(self.root, path, self.tool, duration_seconds=60)

    def test_registered_external_evidence_is_resumed_without_download(self):
        path = self.transcript()
        evidence = self.root / "prior-evidence.md"
        evidence.write_bytes(path.read_bytes())
        # Register a source that is outside normal outputs/videos, as real evidence reuse does.
        flow.register_source(self.root, evidence, self.tool, duration_seconds=10)
        with patch.object(flow.subprocess, "run") as run:
            task = flow.acquire(self.root, kb.TOOL, self.key, self.sync, self.tool, kb.import_outputs)
            self.assertEqual(task["source_path"], str(evidence.resolve()))
            run.assert_not_called()

    def test_short_link_skipped_existing_resolves_from_current_engine_receipt(self):
        url = "https://v.douyin.com/shortABC/"
        result = flow.prepare(self.root, [url], self.tool, duration_seconds=10)
        key = result["tasks"][0]["id"]
        target = self.root / "videos/douyin/1234567890123456789.md"
        fixture = self.transcript(key="1234567890123456789", url="https://www.douyin.com/video/1234567890123456789")
        target.write_bytes(fixture.read_bytes())
        def engine_result(*args, **kwargs):
            log = self.root / "state/results.jsonl"
            log.write_text(json.dumps({"status": "skipped_existing", "video_id": "1234567890123456789"}) + "\n", encoding="utf-8")
            return type("Result", (), {"returncode": 0})()
        with patch.object(flow.subprocess, "run", side_effect=engine_result):
            task = flow.acquire(self.root, kb.TOOL, key, self.sync, self.tool, kb.import_outputs, authorized=True)
        self.assertEqual(task["id"], "1234567890123456789")
        self.assertEqual(flow.read(self.root, key)["stage"], "resolved")


class DocumentWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="markdown-", dir=fixtures))
        self.inbox = self.root / "markdown"
        self.inbox.mkdir()
        self.source = self.inbox / "lesson.md"

    def source_text(self, text="# 原文\n\n## 方法\n記錄原文證據。\n"):
        self.source.write_text(text, encoding="utf-8")
        return self.source.read_bytes()

    def scan(self):
        return flow.scan_markdown(self.root)

    def refine(self, task, title="文件知識"):
        from kb_knowledge import card_filename
        card = self.root / "knowledge" / "cards" / card_filename(title, "document", task["id"])
        card.parent.mkdir(parents=True, exist_ok=True)
        meta = {"type": "distilled_document_knowledge", "platform": "document", "document_id": task["id"],
                "title": title, "author": "Fixture", "processed_at": "2026-10-22", "topics": ["方法"],
                "aliases": ["文件方法"], "content_type": task["content_type"], "evidence_level": "原文主張，未獨立驗證",
                "source_document": "local:" + task["source_filename"], "source_sha256": task["source_sha256"]}
        frontmatter = "\n".join(name + ": " + json.dumps(value, ensure_ascii=False) for name, value in meta.items())
        card.write_text("---\n" + frontmatter + "\n---\n# " + title + "\n"
                        "## 核心知識\n原文強調保存證據。\n## 方法\n先記錄，再檢查。\n"
                        "## 適用條件\n可取得完整原文。\n## 限制\n尚未獨立驗證。\n"
                        "## 來源依據\n" + task["source_filename"] + "；SHA256 " + task["source_sha256"]
                        + "；原文『方法』章節。\n", encoding="utf-8")
        cards = sorted((self.root / "knowledge" / "cards").glob("*.md"))
        (self.root / "knowledge" / "README.md").write_text("# 唯一索引\n" + "\n".join(
            "[卡片](cards/" + path.name + ")" for path in cards) + "\n", encoding="utf-8")
        flow.mark_refined(self.root, task["id"], card)
        return flow.validate_task(self.root, task["id"])

    def validated(self):
        self.source_text()
        scan = self.scan()
        return scan, self.refine(scan["tasks"][0])

    def archive(self, scan, task):
        return flow.archive_markdown(self.root, [task["id"]], scan_id=scan["scan_id"])

    def test_scan_preserves_full_hash_snapshot_and_original_trace(self):
        data = self.source_text()
        result = self.scan()
        task = result["tasks"][0]
        self.assertEqual(task["id"], "doc_" + flow.digest(self.source))
        self.assertEqual(task["document_id"], task["id"])
        self.assertEqual(task["source_sha256"], task["id"][4:])
        self.assertEqual(task["original_path"], str(self.source))
        self.assertEqual(task["source_filename"], "lesson.md")
        self.assertEqual(Path(task["source_path"]).read_bytes(), data)
        self.assertTrue(self.source.exists())
        self.assertEqual(task["stage"], "acquired")

    def test_empty_scan_and_processed_nested_files_are_not_intake(self):
        processed = self.inbox / "processed"
        processed.mkdir()
        (processed / "old.md").write_text("old", encoding="utf-8")
        nested = self.inbox / "nested"
        nested.mkdir()
        (nested / "child.md").write_text("child", encoding="utf-8")
        (self.inbox / "notes.txt").write_text("text", encoding="utf-8")
        result = self.scan()
        self.assertIsNone(result["scan_id"])
        self.assertEqual(result["tasks"], [])
        self.assertEqual(flow.tasks(self.root), [])

    def test_same_contents_different_names_deduplicate_without_losing_origins(self):
        data = self.source_text()
        (self.inbox / "second.md").write_bytes(data)
        result = self.scan()
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(result["tasks"][0]["originals"]), 2)
        repeated = self.scan()
        self.assertEqual(repeated["tasks"], [])
        self.assertEqual(len(repeated["skipped"]), 2)

    def test_repeat_scan_preserves_refined_validated_and_published_stages(self):
        scan, task = self.validated()
        for stage in ("refined", "validated", "main_pushed", "published"):
            task["stage"] = stage
            flow.save(self.root, task)
            result = self.scan()
            self.assertEqual(result["tasks"], [])
            self.assertEqual(result["skipped"][0]["stage"], stage)
            self.assertEqual(flow.read(self.root, task["id"])["stage"], stage)
        self.assertNotEqual(scan["scan_id"], result["scan_id"])

    def test_changed_original_is_new_document_and_old_card_stays_untouched(self):
        scan, task = self.validated()
        card = self.root / task["card_path"]
        previous = card.read_bytes()
        self.source.write_text("# Changed original\n", encoding="utf-8")
        result = self.scan()
        self.assertNotEqual(result["tasks"][0]["id"], task["id"])
        self.assertEqual(result["tasks"][0]["stage"], "acquired")
        self.assertEqual(flow.read(self.root, task["id"])["stage"], "needs_review")
        self.assertEqual(card.read_bytes(), previous)
        with self.assertRaises(ValueError):
            self.archive(scan, task)

    def test_changed_snapshot_is_never_silently_repaired_or_skipped(self):
        scan, task = self.validated()
        snapshot = Path(task["source_path"])
        snapshot.write_text("human edited snapshot", encoding="utf-8")
        result = self.scan()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "human edited snapshot")
        with self.assertRaises(ValueError):
            flow.publication_paths(self.root, [task["id"]])

    def test_document_without_video_url_validates_and_has_publish_paths(self):
        scan, task = self.validated()
        self.assertEqual(task["platform"], "document")
        self.assertEqual(flow.publication_paths(self.root), ["knowledge/README.md", task["card_path"]])
        self.assertTrue(task["card_path"].startswith("knowledge/cards/"))
        # A repeated explicit refinement/validation does not reset published work.
        task["stage"] = "published"
        flow.save(self.root, task)
        flow.mark_refined(self.root, task["id"], self.root / task["card_path"])
        self.assertEqual(flow.validate_task(self.root, task["id"])["stage"], "published")

    def test_archive_preserves_bytes_snapshot_hash_stage_and_replay(self):
        scan, task = self.validated()
        data, snapshot = self.source.read_bytes(), Path(task["source_path"])
        result = self.archive(scan, task)
        destination = self.inbox / "processed" / "lesson.md"
        self.assertFalse(self.source.exists())
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(snapshot.read_bytes(), data)
        saved = flow.read(self.root, task["id"])
        self.assertEqual(saved["stage"], "validated")
        self.assertEqual(saved["originals"][0]["archive_sha256"], task["source_sha256"])
        self.assertEqual(len(result["archived"]), 1)
        self.assertEqual(len(self.archive(scan, task)["skipped"]), 1)
        self.assertEqual(flow.publication_paths(self.root), ["knowledge/README.md", task["card_path"]])
        self.assertEqual(self.scan()["tasks"], [])

    def test_archive_requires_validation_and_exact_scan_membership(self):
        self.source_text()
        scan = self.scan()
        task = scan["tasks"][0]
        with self.assertRaisesRegex(ValueError, "驗證"):
            self.archive(scan, task)
        external = self.root / "external.md"
        external.write_text("# External\n", encoding="utf-8")
        imported = self.refine(flow.import_text(self.root, external))
        with self.assertRaisesRegex(ValueError, "此次 scan"):
            self.archive(scan, imported)
        self.assertTrue(external.exists())
        self.assertTrue(self.source.exists())

    def test_archive_collision_different_hash_preserves_both_files(self):
        scan, task = self.validated()
        destination = self.inbox / "processed" / "lesson.md"
        destination.parent.mkdir()
        destination.write_text("existing different content", encoding="utf-8")
        data = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "版本差異"):
            self.archive(scan, task)
        self.assertEqual(self.source.read_bytes(), data)
        self.assertEqual(destination.read_text(encoding="utf-8"), "existing different content")

    def test_archive_can_reuse_identical_destination_without_overwriting_it(self):
        scan, task = self.validated()
        destination = self.inbox / "processed" / "lesson.md"
        destination.parent.mkdir()
        destination.write_bytes(self.source.read_bytes())
        identity = flow._file_id(destination)
        self.archive(scan, task)
        self.assertFalse(self.source.exists())
        self.assertEqual(flow._file_id(destination), identity)

    def test_interruption_after_copy_before_handle_delete_can_resume(self):
        scan, task = self.validated()
        with patch.object(flow, "_delete_locked", side_effect=OSError("offline interruption before handle deletion")), self.assertRaises(OSError):
            self.archive(scan, task)
        self.assertTrue(self.source.exists())
        self.assertTrue((self.inbox / "processed" / "lesson.md").exists())
        self.assertEqual(flow.read(self.root, task["id"])["originals"][0]["archive_state"], "pending")
        self.assertEqual(len(self.archive(scan, task)["archived"]), 1)

    def test_interruption_after_handle_delete_before_final_save_can_resume(self):
        scan, task = self.validated()
        real_save = flow.save
        def interrupt(repo, value):
            if value.get("archive_state") == "archived":
                raise OSError("offline interruption before final save")
            return real_save(repo, value)
        with patch.object(flow, "save", side_effect=interrupt), self.assertRaises(OSError):
            self.archive(scan, task)
        self.assertFalse(self.source.exists())
        self.assertEqual(flow.read(self.root, task["id"])["originals"][0]["archive_state"], "pending")
        self.assertEqual(len(self.archive(scan, task)["archived"]), 1)

    def test_replacement_after_last_verification_is_blocked_by_native_handle(self):
        scan, task = self.validated()
        data = self.source.read_bytes()
        replacement = self.root / "replacement.md"
        replacement.write_bytes(b"new unverified file\n")
        def replace_after_final_check(descriptor):
            # This callback runs after all content/identity checks, at the exact
            # old check/unlink gap. Native sharing must reject the real replace.
            replacement.replace(self.source)
            self.fail("Replacing the locked original must be rejected by Windows")
        with patch.object(flow, "_delete_locked", side_effect=replace_after_final_check):
            with self.assertRaises(PermissionError) as failure:
                self.archive(scan, task)
        # MoveFileEx may report ERROR_ACCESS_DENIED (5), while CRT writes map
        # sharing failures to errno.EACCES without retaining a Win32 error code.
        self.assertEqual(failure.exception.errno, 13)
        self.assertEqual(self.source.read_bytes(), data)
        self.assertEqual(replacement.read_bytes(), b"new unverified file\n")
        self.assertEqual(flow.read(self.root, task["id"])["originals"][0]["archive_state"], "pending")
        self.assertEqual(len(self.archive(scan, task)["archived"]), 1)
        self.assertTrue(replacement.exists())

    def test_in_place_edit_after_last_verification_is_blocked_by_native_handle(self):
        scan, task = self.validated()
        snapshot = Path(task["source_path"]).read_bytes()
        def edit_after_final_check(descriptor):
            self.source.write_text("human edit during archive", encoding="utf-8")
            self.fail("Writing the locked original must be rejected by Windows")
        with patch.object(flow, "_delete_locked", side_effect=edit_after_final_check):
            with self.assertRaises(PermissionError) as failure:
                self.archive(scan, task)
        self.assertEqual(failure.exception.errno, 13)
        self.assertEqual(self.source.read_bytes(), snapshot)
        self.assertEqual(Path(task["source_path"]).read_bytes(), snapshot)
        saved = flow.read(self.root, task["id"])
        self.assertEqual(saved["originals"][0]["archive_state"], "pending")
        self.assertNotEqual(saved.get("archive_state"), "archived")

    def test_scan_change_between_first_digest_and_import_has_no_mismatched_token(self):
        self.source_text()
        real_digest = flow.digest
        changed = False
        def mutate_after_probe(path):
            nonlocal changed
            value = real_digest(path)
            if path == self.source and not changed:
                changed = True
                self.source.write_bytes(b"# Stable content B after scan probe\n")
            return value
        with patch.object(flow, "digest", side_effect=mutate_after_probe):
            result = self.scan()
        self.assertEqual(len(result["failed"]), 1)
        self.assertIsNone(result["scan_id"])
        self.assertEqual(result["tasks"], [])
        actual_hash = real_digest(self.source)
        imported = flow.read(self.root, "doc_" + actual_hash)
        self.assertEqual(Path(imported["source_path"]).read_bytes(), self.source.read_bytes())
        resumed = self.scan()
        manifest = json.loads(flow._scan_path(self.root, resumed["scan_id"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["entries"][0]["id"], imported["id"])
        self.assertEqual(manifest["entries"][0]["source_sha256"], actual_hash)
        self.assertEqual(len(self.archive(resumed, self.refine(imported))["archived"]), 1)

    def test_handle_identity_matches_full_python_stat_and_does_not_truncate(self):
        self.source_text()
        expected = flow._file_id(self.source)
        descriptor = flow._open_locked(self.source, delete=True)
        try:
            self.assertEqual(flow._stat_id(flow.os.fstat(descriptor)), expected)
        finally:
            flow.os.close(descriptor)
        from types import SimpleNamespace
        large = SimpleNamespace(st_dev=123, st_ino=(1 << 110) + 77, st_birthtime_ns=456)
        self.assertEqual(flow._stat_id(large), [123, (1 << 110) + 77, 456])

    def test_unsupported_platform_keeps_original_and_pending(self):
        scan, task = self.validated()
        with patch.object(flow.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(ValueError, "沒有已驗證"):
                self.archive(scan, task)
        self.assertTrue(self.source.exists())
        self.assertEqual(flow.read(self.root, task["id"])["originals"][0]["archive_state"], "pending")

    def test_changed_human_card_blocks_archive_without_moving_original(self):
        scan, task = self.validated()
        card = self.root / task["card_path"]
        card.write_text(card.read_text(encoding="utf-8") + "\n人工補充。\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "知識卡已改變"):
            self.archive(scan, task)
        self.assertTrue(self.source.exists())
        self.assertFalse((self.inbox / "processed").exists())

    def test_newer_scan_does_not_invalidate_prior_valid_token(self):
        first, task = self.validated()
        second = self.scan()
        self.assertNotEqual(first["scan_id"], second["scan_id"])
        self.assertEqual(len(self.archive(first, task)["archived"]), 1)

    def test_archived_old_token_cannot_remove_reintroduced_same_content(self):
        first, task = self.validated()
        data = self.source.read_bytes()
        self.archive(first, task)
        self.source.write_bytes(data)
        next_scan = self.scan()
        self.assertEqual(next_scan["tasks"], [])
        self.assertEqual(len(self.archive(first, task)["skipped"]), 1)
        self.assertTrue(self.source.exists())
        self.assertEqual(len(self.archive(next_scan, task)["archived"]), 1)

    def test_source_symlink_and_junction_are_rejected_without_reading(self):
        self.source_text()
        original = Path.is_symlink
        def fake_link(path):
            return path == self.source or original(path)
        with patch.object(Path, "is_symlink", fake_link):
            self.assertEqual(len(self.scan()["failed"]), 1)
            with self.assertRaisesRegex(ValueError, "符號連結"):
                flow.import_text(self.root, self.source)
        original_junction = getattr(Path, "is_junction", lambda _: False)
        def fake_junction(path):
            return path == self.inbox or original_junction(path)
        with patch.object(Path, "is_junction", fake_junction, create=True):
            with self.assertRaisesRegex(ValueError, "junction"):
                self.scan()


if __name__ == "__main__":
    unittest.main()
