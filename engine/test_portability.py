"""Offline export contracts: metadata provenance, no publishing, portable runtime."""
import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import douyin_download as downloads
import transcript_tool as tool
import knowledge_sync


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="akb-portability-"))

    def info(self, **overrides):
        return dict(id="youtube_abcdefghijk", title="Fixture", uploader="Author", duration=120,
                    download_method="yt-dlp", **overrides)

    def test_receipt_uses_canonical_source_observed_duration_and_transcript_hash(self):
        record = tool.acquisition_metadata(self.info(), "https://youtu.be/abcdefghijk?si=PRIVATE_TRACKING")
        transcript = self.root / "transcript.md"
        transcript.write_text("offline fixture", encoding="utf-8")
        with patch.object(tool, "ROOT", self.root):
            tool.save_acquisition(record, transcript)
        path = self.root / "state" / "acquisitions" / "youtube_abcdefghijk.json"
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["duration_seconds"], 120)
        self.assertEqual(result["duration_evidence"], "extractor_metadata")
        self.assertEqual(result["source_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(result["transcript_sha256"], hashlib.sha256(transcript.read_bytes()).hexdigest())
        self.assertNotIn("PRIVATE_TRACKING", path.read_text(encoding="utf-8"))

    def test_unknown_duration_live_drm_and_source_mismatch_are_terminal(self):
        for overrides in ({"duration": None}, {"duration": float("inf")}, {"duration": True},
                          {"duration": 0}, {"is_live": True}, {"has_drm": True}, {"live_status": "is_upcoming"}):
            info = self.info()
            info.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(downloads.FallbackError):
                tool.acquisition_metadata(info, "https://youtu.be/abcdefghijk")
        with self.assertRaises(downloads.FallbackError):
            tool.acquisition_metadata(self.info(), "https://youtu.be/zzzzzzzzzzz")

    def test_player_and_douyin_public_metadata_durations_keep_units(self):
        info = self.info()
        info["expected_duration"] = 120.25
        self.assertEqual(tool.acquisition_metadata(info, "https://youtu.be/abcdefghijk")["duration_seconds"], 120.25)
        info = {"id": "12345", "video": {"duration": 12500}}
        record = tool.acquisition_metadata(info, "https://v.douyin.com/fixture/")
        self.assertEqual(record["duration_seconds"], 12.5)
        self.assertEqual(record["canonical_url"], "https://www.douyin.com/video/12345")

    def test_explicit_node_and_playwright_module_avoid_bundled_runtime(self):
        node = self.root / "node.exe"
        node.write_bytes(b"fixture")
        module = self.root / "playwright"
        module.mkdir()
        (module / "package.json").write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"AKB_NODE": str(node), "AKB_PLAYWRIGHT_MODULE": str(module)}), patch.object(
                downloads.subprocess, "run", side_effect=AssertionError("must use explicit module")):
            self.assertEqual(downloads.browser_runtime("12345"), (node, module))

    def test_normal_node_module_resolution_is_bounded_and_uses_engine_directory(self):
        node = self.root / "node.exe"
        node.write_bytes(b"fixture")
        module = self.root / "node_modules" / "playwright"
        module.mkdir(parents=True)
        package = module / "package.json"
        package.write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {"AKB_NODE": str(node), "AKB_PLAYWRIGHT_MODULE": ""}), patch.object(
                downloads.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(package))) as run:
            self.assertEqual(downloads.browser_runtime("12345"), (node, module))
        self.assertEqual(run.call_args.kwargs["timeout"], 10)
        self.assertEqual(run.call_args.kwargs["cwd"], downloads.ROOT)

    def test_legacy_sync_is_disabled_even_without_no_sync_flag(self):
        with patch("subprocess.run", side_effect=AssertionError("no Git process")):
            result = knowledge_sync.sync_outputs(self.root)
        self.assertFalse(result.ok)
        self.assertFalse(result.pushed)


if __name__ == "__main__":
    unittest.main()
