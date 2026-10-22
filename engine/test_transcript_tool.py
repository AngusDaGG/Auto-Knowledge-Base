"""Offline checks for output, deduplication and batch failure handling."""

from pathlib import Path
from types import SimpleNamespace
from io import BytesIO
import json
import errno
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import transcript_tool as tool
import douyin_download as downloads
import knowledge_sync


def patch_ytdlp(youtube_dl):
    return patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=youtube_dl)})


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="douyin_test_"))
        self.knowledge = self.root / "knowledge" / "videos" / "douyin"
        self.knowledge.mkdir(parents=True)
        self.patches = [
            patch.object(tool, "ROOT", self.root),
            patch.object(tool, "OUTPUTS", self.root / "outputs"),
            patch.object(tool, "LOG", self.root / "results.jsonl"),
            patch.object(tool, "KNOWLEDGE_TRANSCRIPTS", self.knowledge),
            patch.object(downloads, "visible_browser_info", side_effect=downloads.FallbackError(
                "offline legacy fixture", category="NO_MEDIA")),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        # Per-test OS temporary workspace remains for inspection; no recursive cleanup.

    def test_url_and_formats(self):
        self.assertEqual(tool.extract_url("複製 https://v.douyin.com/abc/，謝謝"), "https://v.douyin.com/abc/")
        cleaned = tool.safe_title("  A/B:*? " + "x" * 150, "12345")
        self.assertLessEqual(len(cleaned), 120)
        self.assertFalse(any(char in cleaned for char in '<>:"/\\|?*'))
        with self.assertRaises(ValueError):
            tool.extract_url("https://evil-douyin.com/x")
        segments = [SimpleNamespace(start=1.234, end=2.5, text=" 你好 ")]
        path = tool.write_outputs(self.root / "outputs", {
            "id": "12345", "title": "測試影片", "uploader": "作者", "source_url": "https://v.douyin.com/abc/",
        }, segments, "zh")
        self.assertEqual(path.name, "測試影片.md")
        self.assertIn("[00:00:01.234 → 00:00:02.500] 你好", path.read_text(encoding="utf-8"))
        self.assertEqual(list(path.parent.iterdir()), [path])
        collision = tool.write_outputs(self.root / "outputs", {
            "id": "67890", "title": "測試影片", "uploader": "作者", "source_url": "https://v.douyin.com/def/",
        }, segments, "zh")
        self.assertEqual(collision.name, "測試影片 (2).md")
        self.assertEqual(tool.output_for_video_id("12345", path.parent), path)

    def test_legacy_migration_preserves_source_and_old_files(self):
        old = self.root / "outputs" / "12345" / "transcript.md"
        old.parent.mkdir(parents=True)
        old.write_text("# 舊標題\n\n- 原始分享連結：https://v.douyin.com/abc/\n- 影片 ID：12345\n- 原作者：作者\n- 轉錄日期：2026-10-15T00:00:00+08:00\n- 辨識模型：whisper\n- 語言：zh\n\n## 完整逐字稿\n\n[00:00:00.000 → 00:00:01.000] 原始語句\n", encoding="utf-8")
        count, conflicts = tool.migrate_legacy_outputs(self.root / "outputs")
        migrated = self.root / "outputs" / "12345.md"
        self.assertEqual(count, 1)
        self.assertEqual(conflicts, [])
        self.assertEqual(old.read_bytes(), migrated.read_bytes())
        self.assertTrue(old.exists())

    def test_legacy_migration_respects_title_named_markdown(self):
        old = self.root / "outputs" / "12345" / "transcript.md"
        old.parent.mkdir(parents=True)
        content = "# 標題\n\n- 原始分享連結：https://v.douyin.com/abc/\n- 影片 ID：12345\n- 原作者：作者\n- 轉錄日期：2026-10-15\n- 辨識模型：whisper\n- 語言：zh\n\n## 完整逐字稿\n\n[00:00:00.000 → 00:00:01.000] 語句\n"
        old.write_text(content, encoding="utf-8")
        titled = self.root / "outputs" / "我的標題.md"
        titled.write_text(content, encoding="utf-8")
        count, conflicts = tool.migrate_legacy_outputs(self.root / "outputs")
        self.assertEqual((count, conflicts), (0, []))
        self.assertFalse((self.root / "outputs" / "12345.md").exists())
        self.assertTrue(titled.exists())

    def test_single_download_and_duplicate_skip(self):
        calls = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def extract_info(self, url, download):
                calls.append(download)
                if download:
                    Path(self.options["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"fake audio")
                return {"id": "12345", "title": "測試影片", "uploader": "作者", "duration": 12}

        class FakeModel:
            def transcribe(self, media, **_):
                self_media = Path(media)
                assert self_media.exists()
                return [SimpleNamespace(start=0, end=1, text="測試逐字稿")], SimpleNamespace(language="zh")

        with patch_ytdlp(FakeYoutubeDL), patch.object(downloads, "validate_media"), patch.object(
            downloads, "share_info", side_effect=AssertionError("yt-dlp 成功不應啟動 fallback")
        ):
            seen_ids = set()
            first = tool.process_one("https://v.douyin.com/abc/", FakeModel(), ytdlp_first=True, seen_ids=seen_ids)
            second = tool.process_one("https://v.douyin.com/abc/", FakeModel(), ytdlp_first=True, seen_ids=seen_ids)
        self.assertEqual(first[:2], ("success", "12345"))
        self.assertEqual(second[:2], ("skipped_duplicate", "12345"))
        self.assertEqual(calls, [False, True, False])
        self.assertTrue(tool.is_complete(tool.output_for_video_id("12345", self.root / "outputs")))

    def test_complete_knowledge_transcript_skips_before_media_download(self):
        path = tool.write_outputs(self.root / "fixture", {
            "id": "12345", "title": "知識庫既有影片", "source_url": "https://www.douyin.com/video/12345",
        }, [SimpleNamespace(start=0, end=1, text="既有逐字稿")], "zh")
        self.knowledge.joinpath("12345.md").write_bytes(path.read_bytes())
        with patch_ytdlp(lambda *_args, **_kwargs: self.fail("已有逐字稿時不應呼叫 yt-dlp")), patch.object(
            downloads, "share_info", side_effect=AssertionError("已有逐字稿時不應查詢中繼資料")), patch.object(
            downloads, "download_media", side_effect=AssertionError("已有逐字稿時不應下載媒體")
        ):
            status, video_id, _ = tool.process_one("https://www.douyin.com/video/12345", object())
        self.assertEqual((status, video_id), ("skipped_existing", "12345"))

    def test_short_link_resolves_metadata_before_knowledge_dedup(self):
        path = tool.write_outputs(self.root / "fixture", {
            "id": "12345", "title": "知識庫既有影片", "source_url": "https://v.douyin.com/abc/",
        }, [SimpleNamespace(start=0, end=1, text="既有逐字稿")], "zh")
        self.knowledge.joinpath("12345.md").write_bytes(path.read_bytes())
        info = {"id": "12345", "title": "知識庫既有影片"}
        with patch_ytdlp(lambda *_args, **_kwargs: self.fail("metadata dedup must happen before yt-dlp")), patch.object(
            downloads, "share_info", return_value=(info, "12345", [])) as metadata, patch.object(
            downloads, "download_media", side_effect=AssertionError("metadata dedup must happen before media download")
        ) as media:
            status, video_id, _ = tool.process_one("https://v.douyin.com/abc/", object())
        self.assertEqual((status, video_id), ("skipped_existing", "12345"))
        metadata.assert_called_once()
        media.assert_not_called()

    def test_missing_knowledge_directory_is_an_explicit_error(self):
        missing = self.root / "missing-knowledge"
        with patch.object(tool, "KNOWLEDGE_TRANSCRIPTS", missing), patch.object(
            tool, "download_for_transcription", side_effect=AssertionError("must fail before downloader")
        ):
            with self.assertRaisesRegex(RuntimeError, "知識庫逐字稿目錄不存在"):
                tool.process_one("https://www.douyin.com/video/12345", object())

    def test_knowledge_dedup_requires_matching_id_and_complete_content(self):
        path = self.knowledge / "12345.md"
        path.write_text(
            "# Wrong ID\n- 原始分享連結：https://v.douyin.com/x/\n- 影片 ID：67890\n"
            "- 辨識模型：whisper\n- 語言：zh\n\n## 完整逐字稿\n"
            "[00:00:00.000 → 00:00:01.000] 內容\n",
            encoding="utf-8",
        )
        self.assertIsNone(tool.knowledge_transcript("12345"))
        path.write_text(
            "# Incomplete\n- 原始分享連結：https://v.douyin.com/x/\n- 影片 ID：12345\n"
            "- 辨識模型：whisper\n- 語言：zh\n\n## 完整逐字稿\n",
            encoding="utf-8",
        )
        self.assertIsNone(tool.knowledge_transcript("12345"))

    def test_failed_item_can_be_retried_in_a_later_run(self):
        calls = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def extract_info(self, _url, download):
                calls.append(download)
                if download and calls.count(True) == 1:
                    raise RuntimeError("temporary download failure")
                if download:
                    Path(self.options["outtmpl"].replace("%(ext)s", "m4a")).write_bytes(b"fake audio")
                return {"id": "12345", "title": "可重試影片", "uploader": "作者", "duration": 12}

        class FakeModel:
            def transcribe(self, _media, **_):
                return [SimpleNamespace(start=0, end=1, text="成功重試")], SimpleNamespace(language="zh")

        source = "https://www.douyin.com/video/12345"
        with patch_ytdlp(FakeYoutubeDL), patch.object(downloads, "validate_media"), patch.object(
            downloads, "share_info", side_effect=AssertionError("yt-dlp 路徑不應啟動 fallback")
        ):
            with self.assertRaisesRegex(RuntimeError, "temporary download failure"):
                tool.process_one(source, FakeModel(), ytdlp_first=True, seen_ids=set(), seen_sources=set())
            self.assertIsNone(tool.output_for_video_id("12345", self.root / "outputs"))
            tool.log_result(source, "failed", "12345", "temporary download failure")
            self.assertEqual(
                tool.process_one(source, FakeModel(), ytdlp_first=True, seen_ids=set(), seen_sources=set())[:2],
                ("success", "12345"),
            )
        self.assertEqual(calls, [False, True, False, True])

    def test_browser_fallback_and_duplicate_skip(self):
        def fake_download(info, directory):
            path = directory / "playwright_media.bin"
            path.write_bytes(b"test audio")
            info["download_bytes"] = 10
            return path

        model = SimpleNamespace(transcribe=lambda *_args, **_kwargs: (
            [SimpleNamespace(start=0, end=1, text="瀏覽器逐字稿")], SimpleNamespace(language="zh")))
        info = {"id": "12345", "title": "測試", "uploader": "作者", "video": {}, "duration": 12, "download_method": "playwright"}
        with patch_ytdlp(lambda *_args, **_kwargs: self.fail("新預設模式不應呼叫 yt-dlp")), patch.object(
            downloads, "share_info", return_value=(None, "12345", ["HTTP 200，沒有影片資料"])
        ), patch.object(downloads, "browser_info", return_value=info) as browser, patch.object(
            downloads, "download_media", side_effect=fake_download
        ):
            self.assertEqual(tool.process_one("https://v.douyin.com/abc/", model)[0], "success")
            self.assertEqual(tool.process_one("https://v.douyin.com/abc/", model)[0], "skipped")
        browser.assert_called_once_with("12345")

    def test_logged_source_is_skipped_without_network(self):
        source = "https://v.douyin.com/abc/"
        tool.write_outputs(self.root / "outputs", {
            "id": "12345", "title": "測試", "source_url": source,
        }, [SimpleNamespace(start=0, end=1, text="已完成")], "zh")
        tool.log_result("分享文字 " + source, "success", "12345")
        with patch.object(tool, "download_for_transcription", side_effect=AssertionError("不應發送請求")):
            self.assertEqual(tool.process_one(source, object())[:2], ("skipped", "12345"))

    def test_router_data_is_structured_and_id_must_match(self):
        value = {"loaderData": {"isSpider": False, "item_list": [
            {"aweme_id": "other", "video": {"play_addr": {}}},
            {"aweme_id": "12345", "desc": "大括號 } 與跳脫引號 \"", "video": {"play_addr": {}}},
        ]}}
        data = downloads.router_data("window._ROUTER_DATA = " + json.dumps(value) + "; window.other={};")
        self.assertEqual(downloads.router_values(data, "isSpider"), [False])
        self.assertEqual(downloads.find_aweme(data, "12345")["aweme_id"], "12345")
        self.assertIsNone(downloads.find_aweme(data, "99999"))

    def test_audio_is_preferred_and_background_music_is_excluded(self):
        audio = "https://v3.douyinvod.com/audio"
        video = "https://v3.douyinvod.com/video"
        value = {"bit_rate_audio": [{"audio_meta": {"url_list": {"main_url": [audio]}}}],
                 "bit_rate": [{"format": "mp4", "bit_rate": 100, "play_addr": {"url_list": [video]}},
                              {"format": "dash", "play_addr": {"url_list": ["https://v3.douyinvod.com/silent"]}}],
                 "music": {"play_url": {"url_list": ["https://v3.douyinvod.com/music"]}}}
        self.assertEqual(downloads.media_candidates(value), [audio, video])
        self.assertFalse(downloads.supported_media("https://127.0.0.1/audio"))
        self.assertFalse(downloads.supported_media("https://douyinvod.com.evil.example/audio"))

    def test_verified_zjcdn_hosts_keep_media_validation_boundaries(self):
        audio = "https://v5-hl-mly-ov.zjcdn.com/audio?signature=TEST_VALUE"
        self.assertTrue(downloads.supported_media(audio))
        self.assertTrue(downloads.supported_media("https://v5-dy-ov-experiment.zjcdn.com/video"))
        self.assertFalse(downloads.supported_media("https://arbitrary.zjcdn.com/video"))
        self.assertFalse(downloads.supported_media("https://v5-hl-mly-ov.zjcdn.com.evil.example/audio"))
        self.assertFalse(downloads.supported_media("https://user:secret@v5-hl-mly-ov.zjcdn.com/audio"))
        self.assertFalse(downloads.supported_media("https://v5-hl-mly-ov.zjcdn.com:8080/audio"))
        video = {"bit_rate_audio": [{"audio_meta": {"url_list": {"main_url": audio}}}]}
        self.assertEqual(downloads.media_candidates(video), [audio])
        self.assertNotIn("TEST_VALUE", json.dumps(downloads.media_source_summary(video)))

    def test_media_download_requires_bytes_and_audio_validation(self):
        class Response(BytesIO):
            status = 200
            headers = {"Content-Length": "10"}

        info = {"id": "12345", "download_method": "playwright",
                "video": {"play_addr": {"url_list": ["https://v3.douyinvod.com/audio"]}}}
        response = Response(b"0123456789")
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
        with patch.object(downloads, "build_opener", return_value=opener), patch.object(downloads, "validate_media") as validate:
            path = downloads.download_media(info, self.root)
        self.assertEqual(path.read_bytes(), b"0123456789")
        self.assertEqual(info["download_bytes"], 10)
        validate.assert_called_once()
        path.unlink()
        # Parsing a URL or receiving HTTP 200 with a HTML error page is insufficient.
        response = Response(b"0123456789")
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
        with patch.object(downloads, "build_opener", return_value=opener), patch.object(
            downloads, "validate_media", side_effect=downloads.FallbackError("不是可解碼音訊")
        ), self.assertRaises(downloads.FallbackError):
            downloads.download_media(info, self.root)
        self.assertFalse((self.root / "playwright_media.part").exists())

    def test_browser_access_denial_is_preserved(self):
        response = SimpleNamespace(returncode=1, stdout=json.dumps({
            "ok": False, "reason": "公開 API HTTP 403，停止此路徑", "diagnostics": {"navigation_status": 403},
        }))
        with patch.object(downloads, "browser_runtime", return_value=(Path("node"), Path("playwright"))), patch.object(downloads.subprocess, "run", return_value=response) as run, self.assertRaises(
            downloads.FallbackError
        ) as caught:
            downloads.browser_info("12345")
        self.assertIn("403", str(caught.exception))
        self.assertEqual(caught.exception.video_id, "12345")
        self.assertEqual(run.call_count, 1)

    def test_missing_browser_runtime_stops_before_launch(self):
        with patch.object(downloads.Path, "is_file", return_value=False), patch.object(
            downloads.subprocess, "run"
        ) as run, self.assertRaisesRegex(downloads.FallbackError, "Node／Playwright 不可用"):
            downloads.browser_info("12345")
        run.assert_not_called()

    def test_only_expected_ytdlp_failures_trigger_fallback(self):
        self.assertTrue(downloads.should_fallback(RuntimeError("HTTP 403 Forbidden")))
        self.assertFalse(downloads.should_fallback(RuntimeError("disk full")))

    def test_platform_category_survives_default_flow_without_extractor_retry(self):
        with patch.object(downloads, "share_info", return_value=(None, "12345", [])), patch.object(
            downloads, "browser_info", side_effect=downloads.FallbackError("HTTP 403", "12345", "BLOCKED_PLATFORM", "metadata")
        ) as browser, patch_ytdlp(lambda *_args, **_kwargs: self.fail("No retry after denial")), self.assertRaises(
            downloads.FallbackError
        ) as caught:
            tool.download_for_transcription("https://v.douyin.com/abc/", self.root)
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(caught.exception.stage, "metadata")
        browser.assert_called_once_with("12345")

    def test_batch_continues_after_failure(self):
        (self.root / "urls.txt").write_text("https://v.douyin.com/bad/\nhttps://v.douyin.com/good/\n", encoding="utf-8")
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(
            tool, "process_one", side_effect=[RuntimeError("403 Forbidden"), ("success", "12345", "完成")]
        ) as process, redirect_stdout(output):
            with patch.object(knowledge_sync, "sync_outputs") as sync:
                self.assertEqual(tool.main(["--batch"]), 1)
        self.assertEqual(process.call_count, 2)
        sync.assert_not_called()
        self.assertIn("輸入 2 筆；新影片處理 2 筆；重複／略過 0 筆；成功 1 筆；失敗 1 筆", output.getvalue())
        statuses = [(line["status"], line["reason"]) for line in map(
            __import__("json").loads, (self.root / "results.jsonl").read_text(encoding="utf-8").splitlines()
        )]
        self.assertEqual(statuses, [("failed", "403 Forbidden"), ("success", "")])

    def test_empty_batch_is_a_normal_no_work_run(self):
        (self.root / "urls.txt").write_text("# nothing to do\n\n", encoding="utf-8")
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(tool.main(["--batch"]), 0)
        self.assertIn("輸入 0 筆；新影片處理 0 筆；重複／略過 0 筆；成功 0 筆；失敗 0 筆", output.getvalue())

    def test_batch_of_existing_knowledge_transcripts_is_a_normal_skip(self):
        (self.root / "urls.txt").write_text("https://www.douyin.com/video/12345\n", encoding="utf-8")
        fixture = tool.write_outputs(self.root / "fixture", {
            "id": "12345", "title": "既有影片", "source_url": "https://www.douyin.com/video/12345",
        }, [SimpleNamespace(start=0, end=1, text="既有逐字稿")], "zh")
        self.knowledge.joinpath("12345.md").write_bytes(fixture.read_bytes())
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        model = Mock(side_effect=AssertionError("不應載入模型"))
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=model)}), redirect_stdout(output):
            with patch.object(knowledge_sync, "sync_outputs") as sync:
                self.assertEqual(tool.main(["--batch"]), 0)
        sync.assert_not_called()
        model.assert_not_called()
        self.assertIn("輸入 1 筆；新影片處理 0 筆；重複／略過 1 筆；成功 0 筆；失敗 0 筆", output.getvalue())

    def test_sync_collect_rejects_incomplete_or_secret_documents(self):
        output = self.root / "outputs"
        output.mkdir()
        path = output / "video.md"
        path.write_text("# Title\n- 影片 ID：12345\n## 完整逐字稿\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "欄位不完整"):
            knowledge_sync.collect_sources(output)
        path.write_text("# Title\n- 影片 ID：12345\n- 原始分享連結：https://v.douyin.com/x\n## 完整逐字稿\nghp_" + "A" * 30, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "憑證或 Cookie"):
            knowledge_sync.collect_sources(output)


if __name__ == "__main__":
    unittest.main()
