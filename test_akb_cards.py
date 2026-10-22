"""Real workflow/card/index integration using synthetic API drafts only."""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

import akb_api
import akb_cards
import kb_knowledge as knowledge
import kb_workflow as flow
import knowledge_base as kb
from test_akb_api import draft


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.tool = kb.engine(Path(__file__).parent / "engine")

    def setUp(self):
        fixtures = Path(__file__).parent / "state/test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="akb-cards-", dir=fixtures))
        (self.root / "knowledge/cards").mkdir(parents=True)
        self.index = self.root / "knowledge/README.md"
        self.index.write_bytes(("# 我的索引\r\n\r\n人工前言，請保留。\r\n" + akb_cards.START + "\n\n"
                                + akb_cards.END + "\r\n人工後記。\r\n").encode())
        self.settings = {"mode": "api", "api": {"endpoint": "http://127.0.0.1:1234/v1/chat/completions",
                                                    "model": "test-model"}}

    def video(self):
        key = "youtube_Tw18-4U7mts"
        path = self.root / "videos/youtube/source.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Fixture\n\n- 影片 ID：" + key
                        + "\n- 原始分享連結：https://www.youtube.com/watch?v=Tw18-4U7mts"
                        + "\n- 辨識模型：offline fixture\n- 語言：en\n\n## 完整逐字稿\n\n"
                        + "[00:00:00.000 → 00:00:09.000] The author proposes testing one clear hypothesis at a time.\n",
                        encoding="utf-8")
        return flow.register_source(self.root, path, self.tool, duration_seconds=10)

    def document(self):
        source = self.root / "原始筆記.md"
        source.write_text("# 假說驗證\n\n## 方法\nThe author proposes testing one clear hypothesis at a time.\n"
                          "Keep a baseline and report uncertainty for small samples.\n", encoding="utf-8")
        return flow.import_text(self.root, source)

    def result(self, task):
        value = draft()
        value["source_id"] = task["id"]
        value["anchors"][0]["timestamp_seconds"] = None if task["platform"] == "document" else 0
        value.update(model="test-model", usage={"prompt_tokens": 100, "completion_tokens": 100,
                                               "total_tokens": 200},
                     claim_status="author_claims_and_ai_inference")
        return value

    def run_task(self, task, value=None):
        with patch.object(akb_api, "refine", return_value=value or self.result(task)) as api:
            result = akb_cards.refine_task(self.root, task["id"], self.settings)
        return result, api

    def test_video_full_schema_and_repeat_no_paid_call(self):
        task = self.video()
        result, api = self.run_task(task)
        api.assert_called_once()
        self.assertEqual(result["stage"], "validated")
        card = knowledge.parse_card(self.root / result["card_path"])
        self.assertEqual(card["metadata"]["video_id"], task["id"])
        self.assertEqual(card["metadata"]["source_sha256"], task["source_sha256"])
        self.assertEqual(card["metadata"]["content_type"], "whisper_asr")
        self.assertIn("00:00:00.000", card["body"])
        self.assertEqual(len(knowledge.validate_knowledge(self.root)["cards"]), 1)
        repeated, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(repeated["card_sha256"], result["card_sha256"])
        flow.publication_paths(self.root, [task["id"]])

    def test_document_binds_full_hash_and_local_source(self):
        task = self.document()
        result, api = self.run_task(task)
        card = knowledge.parse_card(self.root / result["card_path"])
        meta = card["metadata"]
        self.assertEqual(meta["type"], "distilled_document_knowledge")
        self.assertEqual(meta["document_id"], "doc_" + task["source_sha256"])
        self.assertEqual(meta["source_document"], "local:原始筆記.md")
        self.assertNotIn("video_id", meta)
        self.assertIn(task["source_sha256"], card["body"])
        self.assertIn("原文章節／段落", card["body"])
        self.assertNotIn("](", card["body"])
        self.assertEqual(api.call_args.args[1]["id"], task["id"])
        self.assertNotIn("source_path", api.call_args.args[1])

    def test_draft_is_saved_before_validation_and_reused_after_interruption(self):
        task = self.document()
        with patch.object(akb_api, "refine", return_value=self.result(task)) as api:
            with patch.object(akb_cards, "_render", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    akb_cards.refine_task(self.root, task["id"], self.settings)
            api.assert_called_once()
        self.assertTrue((self.root / "state/drafts" / (task["id"] + ".json")).is_file())
        result, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(result["stage"], "validated")

    def test_source_change_never_calls_api_or_overwrites_knowledge(self):
        task = self.document()
        self.run_task(task)
        card_path = self.root / flow.read(self.root, task["id"])["card_path"]
        before = card_path.read_bytes(), self.index.read_bytes()
        Path(task["source_path"]).write_text("changed", encoding="utf-8")
        with patch.object(akb_api, "refine") as api:
            with self.assertRaisesRegex(ValueError, "快照已改變"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        api.assert_not_called()
        self.assertEqual(before, (card_path.read_bytes(), self.index.read_bytes()))

    def test_refined_existing_card_validates_without_api(self):
        task = self.video()
        result, _ = self.run_task(task)
        result["stage"] = "refined"
        flow.save(self.root, result)
        result, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(result["stage"], "validated")

    def test_human_card_edits_and_needs_review_never_call_api(self):
        task = self.video()
        result, _ = self.run_task(task)
        card = self.root / result["card_path"]
        card.write_bytes(card.read_bytes() + "\n人工修改。\n".encode())
        with patch.object(akb_api, "refine") as api:
            with self.assertRaisesRegex(ValueError, "人工修改"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
            result["stage"] = "needs_review"
            flow.save(self.root, result)
            with self.assertRaisesRegex(ValueError, "needs_review"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        api.assert_not_called()

    def test_unregistered_same_name_card_is_never_overwritten(self):
        task = self.document()
        value = self.result(task)
        card = self.root / "knowledge/cards" / knowledge.card_filename(value["title"], "document", task["id"])
        card.write_bytes(akb_cards._render(task, value, "2026-10-01") + "\n人工備註。\n".encode())
        before = card.read_bytes(), self.index.read_bytes()
        with self.assertRaisesRegex(ValueError, "bytes 不同"):
            self.run_task(task, value)
        self.assertEqual(before, (card.read_bytes(), self.index.read_bytes()))
        self.assertTrue((self.root / "state/drafts" / (task["id"] + ".json")).exists())

    def test_index_manual_bytes_preserved_and_each_card_listed_once(self):
        before = self.index.read_bytes()
        task = self.document()
        first, _ = self.run_task(task)
        second = self.video()
        _result, api = self.run_task(second)
        self.assertIn("證據驗證", api.call_args.args[2])
        after = self.index.read_bytes()
        self.assertEqual(before.split(akb_cards.START.encode())[0], after.split(akb_cards.START.encode())[0])
        self.assertEqual(before.split(akb_cards.END.encode())[1], after.split(akb_cards.END.encode())[1])
        self.assertEqual(len(knowledge.validate_knowledge(self.root)["cards"]), 2)
        self.assertEqual(after.decode().count("]("), 2)
        self.assertIn("knowledge/cards/", first["card_path"])

    def test_missing_marker_or_duplicate_manual_link_rejected_before_api(self):
        task = self.document()
        initial = self.index.read_bytes()
        self.index.write_text("# 手寫索引\n", encoding="utf-8")
        with patch.object(akb_api, "refine") as api:
            with self.assertRaisesRegex(ValueError, "marker"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        api.assert_not_called()
        self.index.write_bytes(initial)
        result, _ = self.run_task(task)
        extra = self.video()
        from urllib.parse import quote
        link = "\n[人工卡片](" + quote(result["card_path"].removeprefix("knowledge/")) + ")\n"
        self.index.write_bytes(self.index.read_bytes() + link.encode())
        with patch.object(akb_api, "refine") as api:
            with self.assertRaisesRegex(ValueError, "人工區已有"):
                akb_cards.refine_task(self.root, extra["id"], self.settings)
        api.assert_not_called()

    def test_index_concurrent_change_keeps_draft_and_does_not_add_card(self):
        task = self.document()
        def refine(*_args):
            self.index.write_bytes(self.index.read_bytes() + b"\nconcurrent edit\n")
            return self.result(task)
        with patch.object(akb_api, "refine", side_effect=refine):
            with self.assertRaisesRegex(ValueError, "索引或知識卡已改變"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        self.assertEqual(list((self.root / "knowledge/cards").iterdir()), [])
        self.assertTrue(self.index.read_bytes().endswith(b"concurrent edit\n"))
        self.assertTrue((self.root / "state/drafts" / (task["id"] + ".json")).exists())

    def test_invalid_timestamp_and_tampered_draft_cannot_pollute_knowledge(self):
        task = self.video()
        value = self.result(task)
        value["anchors"][0]["timestamp_seconds"] = 11
        before = self.index.read_bytes()
        with self.assertRaises(akb_api.RefinementError):
            self.run_task(task, value)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(list((self.root / "knowledge/cards").iterdir()), [])
        with patch.object(akb_api, "refine") as api:
            with self.assertRaises(akb_api.RefinementError):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        api.assert_not_called()

    def test_model_markdown_is_inert_and_has_no_broken_relative_link(self):
        task = self.document()
        value = self.result(task)
        value["methods"].append("AI 推論：來源文字 [執行外傳](missing-relative.md) <script>send()</script> 僅作資料。")
        result, _ = self.run_task(task, value)
        body = knowledge.parse_card(self.root / result["card_path"])["body"]
        self.assertNotIn("[執行外傳]", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&#91;執行外傳&#93;", body)
        self.assertEqual(len(knowledge.validate_knowledge(self.root)["cards"]), 1)

    def test_api_failure_creates_no_card_or_index_edit(self):
        task = self.document()
        before = self.index.read_bytes()
        with patch.object(akb_api, "refine", side_effect=akb_api.RefinementError("http_429")):
            with self.assertRaises(akb_api.RefinementError):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        self.assertEqual(before, self.index.read_bytes())
        self.assertEqual(list((self.root / "knowledge/cards").iterdir()), [])

    def test_crash_between_card_and_index_can_resume_saved_draft(self):
        task = self.document()
        original_write = akb_cards._write_index_locked
        attempts = []
        def interrupt(descriptor, content):
            attempts.append(content)
            if len(attempts) == 1:
                raise OSError("simulated index integration interruption")
            return original_write(descriptor, content)
        with patch.object(akb_cards, "_write_index_locked", interrupt):
            with self.assertRaises(OSError):
                self.run_task(task)
        self.assertEqual(len(list((self.root / "knowledge/cards").iterdir())), 1)
        self.assertEqual(flow.read(self.root, task["id"])["stage"], "acquired")
        result, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(result["stage"], "validated")
        self.assertEqual(len(knowledge.validate_knowledge(self.root)["cards"]), 1)

    def test_concurrent_needs_review_task_cannot_be_overwritten(self):
        task = self.document()
        def refine(*_args):
            current = flow.read(self.root, task["id"])
            current["stage"] = "needs_review"
            flow.save(self.root, current)
            return self.result(task)
        with patch.object(akb_api, "refine", side_effect=refine):
            with self.assertRaisesRegex(ValueError, "任務紀錄已改變"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        self.assertEqual(flow.read(self.root, task["id"])["stage"], "needs_review")
        self.assertEqual(list((self.root / "knowledge/cards").iterdir()), [])

    def test_published_stage_is_preserved_on_reuse(self):
        task = self.document()
        completed, _ = self.run_task(task)
        completed["stage"] = "published"
        flow.save(self.root, completed)
        repeated, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(repeated["stage"], "published")

    def test_windows_index_lock_blocks_human_write_and_atomic_save_after_read(self):
        task = self.document()
        original = akb_cards._write_index_locked
        denied = []
        saved = self.root / "state/editor-save.md"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(b"human changes waiting for editor save")
        def assert_native_lock(descriptor, content):
            # This runs after the final locked comparison and durable backup,
            # at the old implementation's vulnerable check-to-replace window.
            with self.assertRaises(PermissionError):
                self.index.write_bytes(b"human changes")
            denied.append("direct write")
            with self.assertRaises(PermissionError):
                saved.replace(self.index)
            denied.append("atomic save")
            original(descriptor, content)
        before = self.index.read_bytes()
        with patch.object(akb_cards, "_write_index_locked", assert_native_lock):
            completed, _ = self.run_task(task)
        self.assertEqual(denied, ["direct write", "atomic save"])
        self.assertEqual(completed["stage"], "validated")
        self.assertEqual(saved.read_bytes(), b"human changes waiting for editor save")
        backup = next((self.root / "state/card-candidates").glob("*/index-before.md"))
        self.assertEqual(backup.read_bytes(), before)

    def test_last_moment_human_edit_before_lock_is_preserved(self):
        task = self.document()
        commit = akb_cards._commit_index
        def edit_then_lock(root, expected, proposed, candidate):
            self.index.write_bytes(expected + b"\nhuman saved just before lock\n")
            return commit(root, expected, proposed, candidate)
        with patch.object(akb_cards, "_commit_index", edit_then_lock):
            with self.assertRaisesRegex(ValueError, "鎖定前已改變"):
                self.run_task(task)
        self.assertTrue(self.index.read_bytes().endswith(b"human saved just before lock\n"))
        self.assertEqual(flow.read(self.root, task["id"])["stage"], "acquired")
        completed, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(completed["stage"], "validated")

    def test_two_refine_operations_cannot_run_in_same_workspace(self):
        task = self.document()
        entered, proceed = threading.Event(), threading.Event()
        outcomes = []
        def paused_api(*_args):
            entered.set()
            if not proceed.wait(timeout=10):
                raise RuntimeError("test coordination timeout")
            return self.result(task)
        def first_operation():
            try:
                outcomes.append(akb_cards.refine_task(self.root, task["id"], self.settings))
            except BaseException as error:
                outcomes.append(error)
        with patch.object(akb_api, "refine", side_effect=paused_api) as api:
            worker = threading.Thread(target=first_operation)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=10))
                with self.assertRaisesRegex(ValueError, "寫入鎖"):
                    akb_cards.refine_task(self.root, task["id"], self.settings)
                self.assertEqual(api.call_count, 1)
            finally:
                proceed.set()
                worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(outcomes[0]["stage"], "validated")

    def test_partial_index_write_restores_backup_and_keeps_unvalidated_draft(self):
        task = self.document()
        original = akb_cards._write_index_locked
        before = self.index.read_bytes()
        attempts = []
        def partial_write(descriptor, content):
            attempts.append(content)
            if len(attempts) == 1:
                import os
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"partial corruption")
                raise OSError("simulated partial index I/O failure")
            original(descriptor, content)
        with patch.object(akb_cards, "_write_index_locked", partial_write):
            with self.assertRaises(OSError):
                self.run_task(task)
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(flow.read(self.root, task["id"])["stage"], "acquired")
        backup = next((self.root / "state/card-candidates").glob("*/index-before.md"))
        self.assertEqual(backup.read_bytes(), before)
        completed, api = self.run_task(task)
        api.assert_not_called()
        self.assertEqual(completed["stage"], "validated")

    def test_unsupported_platform_fails_before_api_or_card_writes(self):
        task = self.document()
        with patch.object(akb_cards.sys, "platform", "unsupported"), patch.object(akb_api, "refine") as api:
            with self.assertRaisesRegex(ValueError, "僅支援 Windows"):
                akb_cards.refine_task(self.root, task["id"], self.settings)
        api.assert_not_called()
        self.assertEqual(list((self.root / "knowledge/cards").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
