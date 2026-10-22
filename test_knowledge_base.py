"""Entry-point contracts; real engine integration is checked locally, offline."""
from pathlib import Path
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import knowledge_base as kb
import kb_engine


class EntryTests(unittest.TestCase):
    def setUp(self):
        # Retain tiny fixtures; do not recursively delete directories.
        fixtures = Path(__file__).parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="kb_entry_test_", dir=fixtures))
        (self.root / "akb.json").write_text(json.dumps({"publish": {"enabled": True, "repository": "fixture/knowledge", "branch": "main"}}), encoding="utf-8")
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.source = self.outputs / "example.md"
        self.source.write_bytes(b"complete fixture\n")
        self.sync = SimpleNamespace(collect_sources=lambda _: {"12345": self.source})
        self.tool = SimpleNamespace(is_complete=lambda _: True)
        self.target = self.root / "videos" / "douyin" / kb.card_filename("影片", "douyin", "12345")

    def test_copy_then_repeat_is_byte_identical(self):
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 1)
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 0)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())

    def test_legacy_transcript_stays_in_place(self):
        legacy = self.root / "videos/douyin/12345.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(self.source.read_bytes())
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 0)
        self.assertTrue(legacy.is_file())
        self.assertFalse(self.target.exists())

    def test_title_change_in_stored_filename_does_not_duplicate_id(self):
        kb.import_outputs(self.outputs, self.root, self.sync, self.tool)
        renamed = self.target.with_name(kb.card_filename("另一個標題", "douyin", "12345"))
        self.target.rename(renamed)
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 0)
        self.assertEqual(len(list(renamed.parent.glob("*.md"))), 1)

    def test_new_transcript_uses_sanitized_heading_and_stable_id(self):
        self.source.write_text('# 測試影片 / 標題: A\ncomplete fixture\n', encoding="utf-8")
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 1)
        expected = self.target.with_name(kb.card_filename("測試影片 / 標題: A", "douyin", "12345"))
        self.assertEqual(expected.read_bytes(), self.source.read_bytes())

    def test_different_version_stops_without_overwrite(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"user version\n")
        with self.assertRaisesRegex(RuntimeError, "版本差異"):
            kb.import_outputs(self.outputs, self.root, self.sync, self.tool)
        self.assertEqual(self.target.read_bytes(), b"user version\n")

    def test_entire_batch_validated_before_first_copy(self):
        second = self.outputs / "bad.md"
        second.write_bytes(b"incomplete\n")
        self.sync.collect_sources = lambda _: {"12345": self.source, "67890": second}
        self.tool.is_complete = lambda path: path != second
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            kb.import_outputs(self.outputs, self.root, self.sync, self.tool)
        self.assertFalse(self.target.exists())

    def test_missing_source_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            kb.import_outputs(self.root / "missing", self.root, self.sync, self.tool)

    def test_forwarding_uses_kb_workspace_without_automatic_push(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(kb.main(["transcribe", "--url", "https://www.douyin.com/video/12345"]), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--workspace") + 1], str(self.root))
            self.assertIn("--no-sync", command)
            self.assertEqual(command[-2:], ["--url", "https://www.douyin.com/video/12345"])

    def test_engine_failure_exit_code_is_preserved(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.subprocess, "run", return_value=SimpleNamespace(returncode=7)):
            self.assertEqual(kb.main(["transcribe", "--batch"]), 7)

    def test_workspace_override_is_refused(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.subprocess, "run") as run:
            self.assertEqual(kb.main(["transcribe", "--workspace=elsewhere"]), 1)
            run.assert_not_called()

    def receipt(self, status="prepared"):
        folder = self.root / "state" / "publish" / "fixture"
        folder.mkdir(parents=True, exist_ok=True)
        result = {"version": 2, "publication_mode": "direct_main", "target_ref": "refs/heads/main",
                  "status": status, "receipt_path": str(folder / "receipt.json"),
                  "sha": "a" * 40, "branch": "codex/knowledge-sync-fixture",
                  "hashes": {}, "paths": ["knowledge/README.md"]}
        (folder / "receipt.json").write_text(json.dumps(result), encoding="utf-8")
        return result

    def test_prepare_only_delegates_exact_allowlist_without_push(self):
        receipt = self.receipt()
        paths = ["knowledge/README.md", "knowledge/douyin/12345.md"]
        with patch.object(kb.workflow, "publication_paths", return_value=paths), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication", return_value=receipt) as prepare, patch.object(kb.publisher, "push_publication") as push:
            result = kb.publish(self.root, ids=["12345"], prepare_only=True)
            self.assertEqual(result, receipt)
            prepare.assert_called_once_with(self.root, paths, dry_run=False, private_confirmed=True)
            push.assert_not_called()
        self.assertEqual(kb._last_receipt(self.root), Path(receipt["receipt_path"]))

    def test_dry_run_never_pushes(self):
        receipt = self.receipt("dry_run")
        with patch.object(kb.workflow, "publication_paths", return_value=["knowledge/README.md"]), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication", return_value=receipt), patch.object(kb.publisher, "push_publication") as push:
            self.assertEqual(kb.publish(self.root, dry_run=True)["status"], "dry_run")
            push.assert_not_called()

    def test_push_failure_preserves_last_receipt_for_retry(self):
        receipt = self.receipt()
        with patch.object(kb.workflow, "publication_paths", return_value=["knowledge/README.md"]), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication", return_value=receipt), patch.object(kb.publisher, "push_publication", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                kb.publish(self.root)
        self.assertEqual(kb._last_receipt(self.root), Path(receipt["receipt_path"]))

    def test_retry_reuses_receipt_without_preparing_a_new_commit(self):
        receipt = self.receipt("published")
        kb._remember(self.root, receipt)
        with patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication") as prepare, patch.object(kb.publisher, "push_publication", return_value=receipt) as push, patch.object(kb.workflow, "tasks", return_value=[]):
            result = kb.publish(self.root, retry=True)
            self.assertEqual(result["status"], "published")
            self.assertNotIn("pr_url", result)
            prepare.assert_not_called()
            push.assert_called_once_with(self.root, Path(receipt["receipt_path"]), private_confirmed=True)

    def test_verify_delegates_main_readback_without_push_or_pr(self):
        receipt = self.receipt("published")
        with patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "verify_publication", return_value=receipt) as verify, patch.object(kb.publisher, "push_publication") as push, patch.object(kb.workflow, "tasks", return_value=[]):
            kb.publish(self.root, receipt_path=Path(receipt["receipt_path"]), verify=True)
            verify.assert_called_once()
            push.assert_not_called()

    def test_task_stage_advancement_requires_same_card_and_source_hashes(self):
        note = self.root / "knowledge" / "douyin" / "12345.md"
        note.parent.mkdir(parents=True)
        note.write_text("reviewed card", encoding="utf-8")
        card_hash = kb.workflow.digest(note)
        task = {"id": "12345", "stage": "validated", "card_path": "knowledge/douyin/12345.md",
                "card_sha256": card_hash, "source_path": str(self.source), "source_sha256": kb.workflow.digest(self.source)}
        receipt = self.receipt("main_pushed")
        receipt["hashes"] = {task["card_path"]: "wrong"}
        with patch.object(kb.workflow, "tasks", return_value=[task]), patch.object(kb.workflow, "save") as save:
            kb._advance_tasks(self.root, receipt)
            save.assert_not_called()
            receipt["hashes"][task["card_path"]] = card_hash
            kb._advance_tasks(self.root, receipt)
            self.assertEqual(task["stage"], "main_pushed")
            self.assertEqual(task["next_step"], "verify_remote_main")
            self.assertNotIn("published_main_sha", task)

    def test_reverify_not_published_marks_only_matching_publication_for_review(self):
        receipt = self.receipt("not_published")
        card = "knowledge/douyin/12345.md"
        receipt["hashes"] = {card: "original-card-hash"}
        task = {"id": "12345", "stage": "published", "card_path": card,
                "card_sha256": "original-card-hash", "source_sha256": "original-source-hash",
                "validated_at": "original-validation", "publication_receipt": receipt["receipt_path"]}
        other = dict(task, id="other", publication_receipt="different-receipt")
        changed = dict(task, id="changed", card_sha256="new-card-hash")
        with patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "verify_publication", return_value=receipt), patch.object(kb.workflow, "tasks", return_value=[task, other, changed]), patch.object(kb.workflow, "save") as save, patch.object(kb.publisher, "push_publication") as push:
            kb.publish(self.root, receipt_path=Path(receipt["receipt_path"]), verify=True)
            self.assertEqual(task["stage"], "needs_review")
            self.assertEqual(task["remote_publication_status"], "not_published")
            self.assertEqual(task["publication_previous_stage"], "published")
            self.assertEqual(task["next_step"], "inspect_remote_conflict")
            self.assertEqual(task["card_sha256"], "original-card-hash")
            self.assertEqual(task["source_sha256"], "original-source-hash")
            self.assertEqual(task["validated_at"], "original-validation")
            self.assertEqual(other["stage"], "published")
            self.assertEqual(changed["stage"], "published")
            save.assert_called_once_with(self.root, task)
            push.assert_not_called()

    def test_current_main_conflict_raise_uses_audited_receipt_but_network_error_does_not(self):
        receipt = self.receipt("conflict")
        card = "knowledge/douyin/12345.md"
        receipt["hashes"] = {card: "original-card-hash"}
        for audited_status in ("conflict", "published", None):
            task = {"id": "12345", "stage": "published", "card_path": card,
                    "card_sha256": "original-card-hash", "source_sha256": "original-source-hash",
                    "publication_receipt": receipt["receipt_path"]}
            audited = dict(receipt, status=audited_status)
            with self.subTest(audited_status=audited_status), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "verify_publication", side_effect=RuntimeError("remote operation stopped")), patch.object(kb.publisher, "_load", return_value=(audited, self.root), side_effect=RuntimeError("invalid receipt") if audited_status is None else None) as load, patch.object(kb.workflow, "tasks", return_value=[task]), patch.object(kb.workflow, "save") as save, patch.object(kb.publisher, "push_publication") as push:
                with self.assertRaisesRegex(RuntimeError, "remote operation stopped"):
                    kb.publish(self.root, receipt_path=Path(receipt["receipt_path"]), verify=True)
                load.assert_called_once_with(self.root, Path(receipt["receipt_path"]), True, None)
                if audited_status == "conflict":
                    self.assertEqual(task["stage"], "needs_review")
                    self.assertEqual(task["remote_publication_status"], "conflict")
                    self.assertEqual(task["next_step"], "inspect_remote_conflict")
                    save.assert_called_once_with(self.root, task)
                else:
                    self.assertEqual(task["stage"], "published")
                    save.assert_not_called()
                self.assertEqual(task["source_sha256"], "original-source-hash")
                push.assert_not_called()

    def test_private_check_rejects_public_or_read_only_repo(self):
        for data in ({"private": False, "push": True}, {"private": True, "push": False}):
            with patch.object(kb, "_gh", return_value=json.dumps(data)), self.assertRaisesRegex(RuntimeError, "private"):
                kb.confirm_private(self.root)

    def test_gh_errors_do_not_expose_stderr_credentials(self):
        with patch.object(kb.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="secret-api-key")):
            with self.assertRaises(RuntimeError) as caught:
                kb._gh(self.root, "api", "repos/test")
            self.assertNotIn("secret-api-key", str(caught.exception))

    def test_daily_sync_publishes_only_knowledge_without_gh_pr_or_engine(self):
        receipt = self.receipt("published")
        receipt["published_sha"] = "a" * 40
        paths = ["knowledge/README.md", "knowledge/douyin/12345.md"]
        receipt["paths"] = paths
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", side_effect=AssertionError("no engine")), patch.object(kb.workflow, "publication_paths", return_value=paths), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication", return_value=dict(receipt, status="prepared")) as prepare, patch.object(kb.publisher, "push_publication", return_value=receipt) as push, patch.object(kb.workflow, "tasks", return_value=[]), patch.object(kb, "_gh", side_effect=AssertionError("no PR command")):
            self.assertEqual(kb.main(["sync", "--id", "12345"]), 0)
            prepare.assert_called_once_with(self.root, paths, dry_run=False, private_confirmed=True)
            push.assert_called_once_with(self.root, Path(receipt["receipt_path"]), private_confirmed=True)
            self.assertTrue(all(path.startswith("knowledge/") for path in prepare.call_args.args[1]))
            self.assertNotIn("pr_url", receipt)

    def test_post_push_network_failure_retries_original_receipt_without_reacquire(self):
        note = self.root / "knowledge" / "douyin" / "12345.md"
        note.parent.mkdir(parents=True)
        note.write_text("reviewed card", encoding="utf-8")
        task = {"id": "12345", "stage": "validated", "card_path": "knowledge/douyin/12345.md",
                "card_sha256": kb.workflow.digest(note), "source_path": str(self.source),
                "source_sha256": kb.workflow.digest(self.source)}
        receipt = self.receipt("prepared")
        receipt["hashes"] = {task["card_path"]: task["card_sha256"]}
        pending = dict(receipt, status="main_pushed")
        with patch.object(kb.workflow, "publication_paths", return_value=receipt["paths"]), patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "prepare_publication", return_value=receipt) as prepare, patch.object(kb.publisher, "push_publication", side_effect=RuntimeError("readback network failure")), patch.object(kb.publisher, "_load", return_value=(pending, self.root)), patch.object(kb.workflow, "tasks", return_value=[task]), patch.object(kb.workflow, "save"), patch.object(kb.workflow, "acquire") as acquire:
            with self.assertRaisesRegex(RuntimeError, "readback network failure"):
                kb.publish(self.root)
            self.assertEqual(task["stage"], "main_pushed")
            self.assertNotIn("published_main_sha", task)
            self.assertEqual(task["next_step"], "verify_remote_main")
            self.assertEqual(kb._last_receipt(self.root), Path(receipt["receipt_path"]))
            published = dict(receipt, status="published", published_sha="a" * 40)
            with patch.object(kb.publisher, "push_publication", return_value=published):
                kb.publish(self.root, retry=True)
            self.assertEqual(task["stage"], "published")
            self.assertNotIn("next_step", task)
            self.assertEqual(prepare.call_count, 1)
            acquire.assert_not_called()

    def test_legacy_pr_receipt_cannot_push_directly_but_can_verify(self):
        legacy = self.receipt("branch_pushed")
        legacy.update(version=1)
        legacy.pop("publication_mode")
        legacy.pop("target_ref")
        with patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "push_publication", side_effect=RuntimeError("legacy receipt requires new prepare")) as push, patch.object(kb.publisher, "_load", return_value=(legacy, self.root)), patch.object(kb.workflow, "tasks", return_value=[]), patch.object(kb.publisher, "prepare_publication") as prepare, patch.object(kb, "_gh", side_effect=AssertionError("no PR command")):
            with self.assertRaisesRegex(RuntimeError, "legacy receipt"):
                kb.publish(self.root, receipt_path=Path(legacy["receipt_path"]))
            verified = dict(legacy, status="published", published_sha="a" * 40)
            with patch.object(kb.publisher, "verify_publication", return_value=verified) as verify:
                result = kb.publish(self.root, receipt_path=Path(legacy["receipt_path"]), verify=True)
                self.assertEqual(result["status"], "published")
                verify.assert_called_once()
            self.assertEqual(push.call_count, 1)
            prepare.assert_not_called()

    def test_conflicting_retry_marks_review_without_automatic_reprepare_or_acquire(self):
        receipt = self.receipt("conflict")
        card = "knowledge/douyin/12345.md"
        receipt["hashes"] = {card: "original-card-hash"}
        task = {"id": "12345", "stage": "main_pushed", "card_path": card,
                "card_sha256": "original-card-hash", "source_sha256": "original-source-hash",
                "publication_receipt": receipt["receipt_path"]}
        kb._remember(self.root, receipt)
        with patch.object(kb, "confirm_private", return_value=True), patch.object(kb.publisher, "push_publication", side_effect=RuntimeError("remote main changed")), patch.object(kb.publisher, "_load", return_value=(receipt, self.root)), patch.object(kb.workflow, "tasks", return_value=[task]), patch.object(kb.workflow, "save"), patch.object(kb.publisher, "prepare_publication") as prepare, patch.object(kb.workflow, "acquire") as acquire:
            with self.assertRaisesRegex(RuntimeError, "remote main changed"):
                kb.publish(self.root, retry=True)
            self.assertEqual(task["stage"], "needs_review")
            self.assertEqual(task["remote_publication_status"], "conflict")
            self.assertEqual(task["next_step"], "inspect_remote_conflict")
            self.assertEqual(task["source_sha256"], "original-source-hash")
            prepare.assert_not_called()
            acquire.assert_not_called()

    def test_local_commands_do_not_load_transcription_engine(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", side_effect=AssertionError("engine not needed")), patch.object(kb.workflow, "tasks", return_value=[]), patch.object(kb.workflow, "import_text", return_value={"stage": "acquired"}), patch.object(kb, "validate_knowledge", return_value={"index": "knowledge/README.md", "summary": "ok", "cards": []}), patch.object(kb, "search_knowledge", return_value={"index_content": "# index", "no_results": True}):
            for command in (["status"], ["import-text", str(self.source)], ["validate"], ["search", "Docker"]):
                with self.subTest(command=command):
                    self.assertEqual(kb.main(command), 0)

    def test_prepare_duration_is_only_for_one_url(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.workflow, "prepare") as prepare:
            self.assertEqual(kb.main(["prepare", "--url", "one", "--url", "two", "--duration-seconds", "60"]), 1)
            prepare.assert_not_called()

    def test_prepare_defaults_and_acquire_authorization_are_forwarded(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.workflow, "prepare", return_value={}) as prepare, patch.object(kb.workflow, "acquire", return_value={}) as acquire:
            self.assertEqual(kb.main(["prepare", "--url", "https://example.invalid"]), 0)
            self.assertEqual(prepare.call_args.kwargs["max_items"], 3)
            self.assertEqual(prepare.call_args.kwargs["max_seconds"], 600)
            self.assertEqual(kb.main(["acquire", "12345", "--authorized", "--duration-seconds", "60"]), 0)
            self.assertTrue(acquire.call_args.kwargs["authorized"])

    def test_gemini_status_is_disabled_and_does_not_load_engine(self):
        import kb_gemini
        with patch.object(kb, "ROOT", self.root), patch.object(kb, "engine", side_effect=AssertionError("not needed")), patch.object(kb_gemini, "get_status", return_value={"enabled": False}) as status:
            self.assertEqual(kb.main(["gemini-status"]), 0)
            status.assert_called_once_with(self.root, enabled=False)


class PlatformEntryTests(unittest.TestCase):
    setUp = EntryTests.setUp
    def test_new_platform_import_uses_its_own_directory(self):
        key = "youtube_J_GCvfto07c"
        self.sync.collect_sources = lambda _: {key: self.source}
        self.sync.transcript_relative_path = lambda value: Path("videos") / "youtube" / f"{value}.md"
        self.assertEqual(kb.import_outputs(self.outputs, self.root, self.sync, self.tool), 1)
        self.assertEqual((self.root / "videos" / "youtube" / kb.card_filename("影片", "youtube", key)).read_bytes(), self.source.read_bytes())
        self.assertFalse((self.root / "videos" / "douyin" / f"{key}.md").exists())

    def test_explicit_markdown_scan_needs_no_video_engine(self):
        result = {"scan_id": "scan-fixture", "tasks": [], "skipped": [], "failed": []}
        with patch.object(kb, "ROOT", self.root), patch.object(kb.workflow, "scan_markdown", return_value=result) as scan, patch.object(kb, "engine", side_effect=AssertionError("no engine")):
            self.assertEqual(kb.main(["scan-markdown"]), 0)
            scan.assert_called_once_with(self.root)

    def test_prepare_also_discovers_manual_documents(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb.workflow, "scan_markdown", return_value={"scan_id": "fixture", "tasks": [{"id": "doc_fixture"}]}) as scan, patch.object(kb, "engine", return_value=(self.sync, self.tool)), patch.object(kb.workflow, "prepare", return_value={"tasks": []}) as prepare:
            self.assertEqual(kb.main(["prepare", "--url", "https://youtu.be/Tw18-4U7mts"]), 0)
            scan.assert_called_once_with(self.root)
            prepare.assert_called_once()

    def test_status_does_not_scan_or_move_documents(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb.workflow, "scan_markdown", side_effect=AssertionError("read-only status")), patch.object(kb.workflow, "tasks", return_value=[]):
            self.assertEqual(kb.main(["status"]), 0)

    def test_archive_cli_requires_exact_scan_and_selected_ids(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb.workflow, "archive_markdown", return_value={"archived": []}) as archive, patch.object(kb, "engine", side_effect=AssertionError("no engine")):
            self.assertEqual(kb.main(["archive-markdown", "--id", "doc_fixture", "--scan-id", "scan-fixture"]), 0)
            archive.assert_called_once_with(self.root, ["doc_fixture"], scan_id="scan-fixture")

    def test_card_path_uses_task_identity_and_needs_no_engine(self):
        with patch.object(kb, "ROOT", self.root), patch.object(kb.workflow, "read", return_value={"id": "youtube_Tw18-4U7mts", "platform": "youtube"}), patch.object(kb, "engine", side_effect=AssertionError("no engine")), patch.object(kb.workflow, "scan_markdown", side_effect=AssertionError("read-only name")):
            self.assertEqual(kb.main(["card-path", "youtube_Tw18-4U7mts", "--title", "測試標題"]), 0)

    def test_engine_adapter_skips_titled_external_import_without_outputs_cache(self):
        sync, tool = kb.engine(kb.TOOL)
        key = "youtube_Tw18-4U7mts"
        destination = self.root / "videos/youtube" / kb.card_filename("標題不同也同一影片", "youtube", key)
        destination.parent.mkdir(parents=True)
        destination.write_text(f"# Fixture\n\n- 影片 ID：{key}\n- 原始分享連結：https://www.youtube.com/watch?v=Tw18-4U7mts\n- 辨識模型：offline fixture\n- 語言：en\n\n## 完整逐字稿\n\n[00:00:00.000 → 00:00:09.000] Complete test speech.\n", encoding="utf-8")
        (self.root / "videos/douyin").mkdir()
        original_known, original_completed = tool.knowledge_transcript, tool.completed_source_id
        with patch.object(kb_engine, "engine", return_value=(sync, tool)), patch.object(tool, "ROOT", self.root), patch.object(tool, "OUTPUTS", self.outputs), patch.object(tool, "LOG", self.root / "state/results.jsonl"), patch.object(tool, "KNOWLEDGE_TRANSCRIPTS", self.root / "videos/douyin"), patch.object(tool, "download_for_transcription", side_effect=AssertionError("must not download or load Whisper")) as download:
            self.assertEqual(kb_engine.run(kb.TOOL, ["--workspace", str(self.root), "--no-sync", "--url", "https://youtu.be/Tw18-4U7mts"]), 0)
            (self.root / "urls.txt").write_text("https://www.youtube.com/watch?v=Tw18-4U7mts\nhttps://youtube.com/shorts/Tw18-4U7mts\n", encoding="utf-8")
            self.assertEqual(kb_engine.run(kb.TOOL, ["--workspace", str(self.root), "--no-sync", "--batch"]), 0)
            download.assert_not_called()
        self.assertIs(tool.knowledge_transcript, original_known)
        self.assertIs(tool.completed_source_id, original_completed)
        self.assertEqual(list(self.outputs.glob("*.md")), [self.source])

    def test_escaping_engine_destination_is_refused_before_writing(self):
        self.sync.transcript_relative_path = lambda _: Path("..") / "outside.md"
        with self.assertRaisesRegex(RuntimeError, "目的地"):
            kb.import_outputs(self.outputs, self.root, self.sync, self.tool)

    def test_only_knowledge_is_publishable(self):
        for platform in ("youtube", "instagram", "tiktok", "bilibili"):
            self.assertFalse(kb.managed(f"videos/{platform}/id.md"))
            self.assertTrue(kb.managed(f"knowledge/{platform}/id.md"))
        self.assertFalse(kb.managed("videos/unknown/id.md"))
        self.assertFalse(kb.managed("videos/youtube/token.json"))


class PortableWorkspaceTests(unittest.TestCase):
    setUp = EntryTests.setUp

    def test_explicit_workspace_does_not_use_code_directory(self):
        with patch.object(kb.workflow, "tasks", return_value=[]) as tasks:
            self.assertEqual(kb.main(["--workspace", str(self.root), "status"]), 0)
            tasks.assert_called_once_with(self.root)

    def test_relative_workspace_is_rejected_before_state_access(self):
        with patch.object(kb.workflow, "tasks") as tasks:
            self.assertEqual(kb.main(["--workspace", "relative-workspace", "status"]), 1)
            tasks.assert_not_called()

    def test_relative_refined_card_uses_selected_workspace(self):
        with patch.object(kb.workflow, "mark_refined", return_value={}) as refined:
            self.assertEqual(kb.main(["--workspace", str(self.root), "refined", "doc_fixture", "knowledge/cards/card.md"]), 0)
            refined.assert_called_once_with(self.root, "doc_fixture", self.root / "knowledge/cards/card.md")

    def test_live_private_check_returns_exact_confirmed_target(self):
        with patch.object(kb, "_gh", return_value=json.dumps({"private": True, "push": True})) as gh:
            self.assertEqual(kb.confirm_private(self.root), "https://github.com/fixture/knowledge.git")
            self.assertIn("repos/fixture/knowledge", gh.call_args.args)

    def test_publish_requires_opt_in_even_when_no_validated_tasks(self):
        (self.root / "akb.json").write_text(json.dumps({"publish": {"enabled": False}}), encoding="utf-8")
        with patch.object(kb, "confirm_private") as private, patch.object(kb.workflow, "publication_paths") as paths:
            with self.assertRaisesRegex(RuntimeError, "停用"):
                kb.publish(self.root)
            private.assert_not_called(); paths.assert_not_called()


if __name__ == "__main__":
    unittest.main()
