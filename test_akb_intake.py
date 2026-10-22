"""Offline URL intake contracts; engine calls write synthetic receipts only."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import akb_intake as intake_module
import knowledge_base as kb
import kb_workflow as flow


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sync, cls.tool = kb.engine(kb.TOOL)

    def setUp(self):
        fixtures = Path(__file__).parent / "state" / "intake-test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=fixtures))
        (self.root / "videos" / "douyin").mkdir(parents=True)
        self.url = "https://www.youtube.com/watch?v=abcdefghijk"
        self.key = "youtube_abcdefghijk"
        self.calls = []
        self.receipt_override = {}
        self.process_status = 0
        self.run_patch = patch.object(intake_module.subprocess, "run", side_effect=self.engine)
        self.run = self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def transcript(self, key, source, duration=900):
        out = self.root / "outputs"
        out.mkdir(exist_ok=True)
        p = out / (key + ".md")
        minutes, seconds = divmod(duration - 1, 60)
        hours, minutes = divmod(minutes, 60)
        p.write_text(f"# Fixture\n- 影片 ID：{key}\n- 原始分享連結：{source}\n- 原作者：Fixture\n"
                     "- 辨識模型：offline fixture\n- 語言：en\n\n## 完整逐字稿\n\n"
                     f"[00:00:00.000 → {hours:02}:{minutes:02}:{seconds:02}.000] Complete fixture speech.\n", encoding="utf-8")
        return p

    def engine(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.process_status:
            return subprocess.CompletedProcess(argv, self.process_status, "", "SECRET failure payload")
        url = argv[argv.index("--url") + 1]
        if "v.douyin.com" in url:
            key, platform, canonical = "12345", "douyin", "https://www.douyin.com/video/12345"
        else:
            key, platform, canonical = flow.identity(url, self.tool)
        p = self.transcript(key, url)
        record = {"version": 1, "video_id": key, "platform": platform, "canonical_url": canonical,
                  "source_url": canonical, "duration_seconds": 900, "duration_evidence": "extractor_metadata",
                  "content_type": "whisper_asr", "method": "yt-dlp", "transcript_sha256": flow.digest(p)}
        record.update(self.receipt_override)
        folder = self.root / "state" / "acquisitions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (key + ".json")).write_text(json.dumps(record), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def call(self, texts, settings=None):
        return intake_module.intake(self.root, texts, settings or {})

    def test_multi_url_share_text_escaped_scheme_and_aliases_deduplicate(self):
        result = self.call(["分享 https\\://youtu.be/abcdefghijk?si=tracking 然後 " + self.url,
                            "https://www.youtube.com/shorts/abcdefghijk"])
        self.assertEqual(result["tasks"], [self.key])
        self.assertEqual(result["pending"], [self.key])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(result["skipped"]), 2)
        self.assertFalse(result["failed"])
        task = flow.read(self.root, self.key)
        self.assertEqual(task["duration_seconds"], 900)
        self.assertEqual(task["max_seconds"], 900)
        self.assertEqual(task["stage"], "acquired")
        self.assertEqual(Path(task["source_path"]).parent, self.root / "videos" / "youtube")

    def test_resume_acquired_validated_and_published_never_calls_engine(self):
        self.call([self.url])
        for stage in ("acquired", "validated", "published"):
            task = flow.read(self.root, self.key)
            task["stage"] = stage
            flow.save(self.root, task)
            with self.subTest(stage=stage):
                result = self.call([self.url])
                self.assertFalse(result["failed"])
                self.assertEqual(result["tasks"], [self.key])
        self.assertEqual(len(self.calls), 1)

    def test_short_link_resolution_is_persisted_and_canonical_rerun_does_not_fetch(self):
        short = "https://v.douyin.com/offline-fixture/"
        result = self.call([short, "https://www.douyin.com/video/12345"])
        self.assertFalse(result["failed"])
        alias_key = flow.identity(short, self.tool)[0]
        self.assertEqual(flow.read(self.root, alias_key)["resolved_id"], "12345")
        self.assertEqual(result["tasks"], ["12345"])
        self.call([short])
        self.assertEqual(len(self.calls), 1)

    def test_invalid_receipt_hash_does_not_import_or_redownload_on_resume(self):
        self.receipt_override = {"transcript_sha256": "0" * 64}
        result = self.call([self.url])
        self.assertTrue(result["failed"])
        self.assertEqual(flow.read(self.root, self.key)["stage"], "failed")
        self.assertFalse((self.root / "videos" / "youtube").exists())
        self.call([self.url])
        self.assertEqual(len(self.calls), 1)

    def test_invalid_receipt_duration_and_source_identity_stop(self):
        overrides = ({"duration_seconds": None}, {"duration_seconds": True},
                     {"duration_seconds": -1}, {"canonical_url": "https://youtu.be/zzzzzzzzzzz"},
                     {"method": []}, {"canonical_url": None})
        for number, override in enumerate(overrides):
            self.receipt_override = override
            with self.subTest(override=override):
                result = self.call([f"https://youtu.be/abcdefghij{number}"])
                self.assertTrue(result["failed"])
        self.assertEqual(len(self.calls), len(overrides))

    def test_unsupported_and_missing_urls_are_safe_independent_failures(self):
        result = self.call(["https://evil.example/?token=SECRET_PRIVATE", "no link", self.url])
        self.assertEqual(len(result["failed"]), 2)
        self.assertEqual(result["tasks"], [self.key])
        self.assertNotIn("SECRET_PRIVATE", json.dumps(result))
        self.assertEqual(len([t for t in flow.tasks(self.root) if t["stage"] == "failed"]), 2)

    def test_engine_failure_is_recorded_once_and_next_video_continues(self):
        self.process_status = 1
        result = self.call([self.url, "https://youtu.be/zzzzzzzzzzz"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(result["failed"]), 2)
        self.assertTrue(all(item["reason"] == "engine_failed" for item in result["failed"]))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_timeout_is_safe_and_has_no_outer_retry(self):
        self.run.side_effect = subprocess.TimeoutExpired("SECRET", 1)
        result = self.call([self.url])
        self.assertEqual(result["failed"][0]["reason"], "engine_timeout")
        self.assertEqual(self.run.call_count, 1)

    def test_explicit_settings_use_argv_and_only_approved_runtime_environment(self):
        settings = {"engine": {"model_path": "fixture model", "vendor_dir": "fixture vendor",
                               "python": "fixture-python", "env": {"AKB_NODE": "fixture node"},
                               "timeout_seconds": 10000}}
        self.call([self.url], settings)
        argv, kwargs = self.calls[0]
        self.assertEqual(argv[0], "fixture-python")
        self.assertIn("--no-sync", argv)
        self.assertEqual(argv[argv.index("--model-path") + 1], "fixture model")
        self.assertEqual(kwargs["env"]["AKB_NODE"], "fixture node")
        self.assertEqual(kwargs["timeout"], 10000)
        self.assertNotIn("shell", kwargs)
        with self.assertRaises(intake_module.IntakeError):
            self.call([], {"engine": {"env": {"COOKIE": "do-not-read"}}})

    def test_batch_limit_defers_with_no_extra_engine_invocation(self):
        result = self.call([self.url, "https://youtu.be/zzzzzzzzzzz"], {"max_batch": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["skipped"][0]["reason"], "batch_limit")

    def test_every_call_scans_markdown_and_retains_prior_unfinished_documents(self):
        inbox = self.root / "markdown"
        inbox.mkdir()
        (inbox / "fixture.md").write_text("# Fixture\n\nLong enough original source discussion with conditions and limitations.\n", encoding="utf-8")
        first = self.call([])
        self.assertIsNotNone(first["scan_id"])
        self.assertEqual(len(first["pending"]), 1)
        second = self.call([])
        self.assertEqual(first["pending"], second["pending"])
        self.assertEqual(first["tasks"], second["tasks"])
        self.run.assert_not_called()

    def test_import_failure_recovers_receipt_without_fetching_again(self):
        short = "https://v.douyin.com/offline-fixture/"
        with patch.object(kb, "import_outputs", side_effect=RuntimeError("fixture conflict")):
            first = self.call([short])
        self.assertTrue(first["failed"])
        second = self.call([short])
        self.assertFalse(second["failed"])
        self.assertEqual(second["tasks"], ["12345"])
        self.assertEqual(len(self.calls), 1)

    def test_changed_existing_source_is_retained_without_engine_and_reports_failure(self):
        self.call([self.url])
        task = flow.read(self.root, self.key)
        source = Path(task["source_path"])
        source.write_text(source.read_text(encoding="utf-8") + "Changed source", encoding="utf-8")
        result = self.call([self.url])
        self.assertTrue(result["failed"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("last_intake_error", flow.read(self.root, self.key))

    def test_existing_output_without_receipt_never_reloads_or_guesses_duration(self):
        self.transcript(self.key, self.url)
        result = self.call([self.url])
        self.assertTrue(result["failed"])
        self.run.assert_not_called()
        self.assertNotIn("duration_seconds", flow.read(self.root, self.key))


if __name__ == "__main__":
    unittest.main()
