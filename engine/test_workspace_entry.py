"""Offline contract for the external knowledge-base entry point."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import transcript_tool as tool

class WorkspaceTests(unittest.TestCase):
    def test_workspace_skip_does_not_load_model_download_or_sync(self):
        root = Path(tempfile.mkdtemp(prefix="workspace_contract_"))
        kb = root / "videos" / "douyin"
        kb.mkdir(parents=True)
        (kb / "12345.md").write_text(
            "# Test\n- 影片 ID：12345\n- 原始分享連結：https://www.douyin.com/video/12345\n"
            "- 辨識模型：fixture\n- 語言：zh\n\n## 完整逐字稿\n"
            "[00:00:00.000 → 00:00:01.000] Fixture\n", encoding="utf-8")
        with patch.object(tool, "ROOT", tool.ROOT), patch.object(tool, "OUTPUTS", tool.OUTPUTS), patch.object(tool, "LOG", tool.LOG), patch.object(tool, "KNOWLEDGE_TRANSCRIPTS", tool.KNOWLEDGE_TRANSCRIPTS), patch.object(tool.downloads, "share_info", side_effect=AssertionError("network")), patch("knowledge_sync.sync_outputs", side_effect=AssertionError("sync")):
            result = tool.main(["--workspace", str(root), "--no-sync", "--url", "https://www.douyin.com/video/12345"])
            self.assertEqual(result, 0)
            self.assertTrue((root / "state" / "results.jsonl").is_file())
            self.assertFalse((root / "outputs").exists())

    def test_interactive_batch_preserves_no_sync_and_legacy_mode(self):
        original = tool.main
        with patch.object(tool, "require_knowledge_transcripts"), patch.object(tool, "migrate_legacy_outputs", return_value=(0, [])), patch("builtins.input", return_value="B"), patch.object(tool, "main", return_value=0) as recursive:
            self.assertEqual(original(["--no-sync", "--yt-dlp-first"]), 0)
            self.assertIn("--no-sync", recursive.call_args.args[0])
            self.assertIn("--yt-dlp-first", recursive.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
