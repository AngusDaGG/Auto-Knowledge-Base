"""Offline route policy checks; retain small fixtures under workspace/state."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import douyin_download as downloads
import knowledge_sync
import transcript_tool as tool


VIDEO_ID = "12345"
SOURCE = f"https://www.douyin.com/video/{VIDEO_ID}"
SHORT_SOURCE = "https://v.douyin.com/offline-fixture/"


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        # Fixtures stay inside state and are intentionally retained, with no recursive cleanup.
        fixtures = Path(tempfile.gettempdir()) / "akb-engine-routes"
        fixtures.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName[5:35] + "-", dir=fixtures))
        self.knowledge = self.root / "videos" / "douyin"
        self.knowledge.mkdir(parents=True)
        self.events = []
        self.visible_error = None
        self.share_error = None
        self.browser_error = None
        self.ytdlp_error = None
        self.media_errors = {}
        self.visible_result = self.info("visible-browser")
        self.share_result = (None, VIDEO_ID, ["No media in public share HTML"])
        self.browser_result = self.info("playwright")
        self.ytdlp_options = []

        self.start_patch(patch.object(tool, "ROOT", self.root))
        self.start_patch(patch.object(tool, "OUTPUTS", self.root / "outputs"))
        self.start_patch(patch.object(tool, "LOG", self.root / "results.jsonl"))
        self.start_patch(patch.object(tool, "KNOWLEDGE_TRANSCRIPTS", self.knowledge))
        self.backoff = self.start_patch(patch.object(tool, "sleep"))
        self.visible = self.start_patch(patch.object(
            downloads, "visible_browser_info", side_effect=self.visible_provider, create=True))
        self.share = self.start_patch(patch.object(downloads, "share_info", side_effect=self.share_provider))
        self.browser = self.start_patch(patch.object(downloads, "browser_info", side_effect=self.browser_provider))
        self.media = self.start_patch(patch.object(downloads, "download_media", side_effect=self.download_media))
        # Route tests use synthetic bytes; decoder validity belongs to media tests.
        self.start_patch(patch.object(downloads, "validate_media"))
        self.start_patch(patch.object(downloads, "build_opener", side_effect=AssertionError("Offline test: network forbidden")))
        self.start_patch(patch.object(downloads.subprocess, "run", side_effect=AssertionError("Offline test: browser forbidden")))
        self.model = Mock(side_effect=lambda: self.events.append("model"))

        owner = self

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options
                owner.ytdlp_options.append(dict(options))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def extract_info(self, _source, download):
                owner.events.append("yt-dlp:download" if download else "yt-dlp:metadata")
                if owner.ytdlp_error:
                    raise owner.ytdlp_error
                if download:
                    path = Path(self.options["outtmpl"].replace("%(ext)s", "m4a"))
                    path.write_bytes(b"offline yt-dlp media")
                return owner.info("yt-dlp")

        self.start_patch(patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)}))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def info(method):
        return {"id": VIDEO_ID, "title": "Offline fixture", "uploader": "Fixture author",
                "video": {}, "download_method": method, "expected_duration": 3.5}

    @staticmethod
    def no_media(method):
        return downloads.FallbackError(f"{method}: no playable media", VIDEO_ID, "NO_MEDIA", "media_url")

    @staticmethod
    def blocked(method):
        return downloads.FallbackError(f"{method}: HTTP 403", VIDEO_ID, "BLOCKED_PLATFORM", "metadata")

    def visible_provider(self, *_args, **_kwargs):
        self.events.append("visible")
        if self.visible_error:
            raise self.visible_error
        return self.visible_result

    def share_provider(self, *_args, **_kwargs):
        self.events.append("share")
        if self.share_error:
            raise self.share_error
        return self.share_result

    def browser_provider(self, *_args, **_kwargs):
        self.events.append("browser")
        if self.browser_error:
            raise self.browser_error
        return self.browser_result

    def download_media(self, info, directory):
        method = info["download_method"]
        self.events.append(f"media:{method}")
        if error := self.media_errors.get(method):
            raise error
        path = directory / (method + ".m4a")
        path.write_bytes(b"offline media fixture")
        info["download_bytes"] = path.stat().st_size
        return path

    def run_download(self, source=SOURCE, **kwargs):
        return tool.download_for_transcription(source, self.root, before_download=self.model, **kwargs)

    def existing_transcript(self, directory=None):
        directory = directory or self.knowledge
        directory.mkdir(exist_ok=True)
        path = directory / f"{VIDEO_ID}.md"
        path.write_text(
            f"# Fixture\n- 影片 ID：{VIDEO_ID}\n- 原始分享連結：{SOURCE}\n"
            "- 辨識模型：fixture\n- 語言：zh\n\n## 完整逐字稿\n"
            "[00:00:00.000 → 00:00:01.000] Existing transcript\n", encoding="utf-8")
        return path

    def test_visible_success_is_first_and_stops_fallbacks(self):
        info, path = self.run_download()
        self.assertEqual(info["download_method"], "visible-browser")
        self.assertEqual(info["expected_duration"], 3.5)
        self.assertTrue(path.is_file())
        self.assertEqual(self.events, ["visible", "model", "media:visible-browser"])
        self.visible.assert_called_once()
        self.assertEqual(self.visible.call_args.args[:2], (SOURCE, VIDEO_ID))
        self.assertEqual(self.visible.call_args.args[2], self.root / "state" / "visible-browser-profile")
        self.share.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(self.ytdlp_options, [])

    def test_legacy_share_is_second_and_stops_before_browser_and_ytdlp(self):
        self.visible_error = self.no_media("visible")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        info, path = self.run_download()
        self.assertEqual(info["download_method"], "share-page")
        self.assertTrue(path.is_file())
        self.assertEqual(self.events, ["visible", "share", "model", "media:share-page"])
        self.visible.assert_called_once()
        self.share.assert_called_once()
        self.browser.assert_not_called()
        self.assertEqual(self.ytdlp_options, [])

    def test_missing_browser_dependency_stops_without_legacy_share(self):
        self.visible_error = downloads.FallbackError(
            "Local browser executable is unavailable", VIDEO_ID, "BROWSER_UNAVAILABLE", "page_load")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        with self.assertRaises(downloads.FallbackError):
            self.run_download()
        self.assertEqual(self.events, ["visible"])
        self.backoff.assert_not_called()

    def test_local_storage_failure_does_not_start_another_route(self):
        self.media_errors["visible-browser"] = OSError("Local disk is full")
        with self.assertRaisesRegex(OSError, "Local disk is full"):
            self.run_download()
        self.assertEqual(self.events, ["visible", "model", "media:visible-browser"])

    def test_legacy_browser_follows_share_and_stops_before_ytdlp(self):
        self.visible_error = self.no_media("visible")
        info, _ = self.run_download()
        self.assertEqual(info["download_method"], "playwright")
        self.assertEqual(self.events, ["visible", "share", "browser", "model", "media:playwright"])
        self.visible.assert_called_once()
        self.share.assert_called_once()
        self.browser.assert_called_once_with(VIDEO_ID)
        self.assertEqual(self.ytdlp_options, [])

    def test_ytdlp_is_third_with_each_earlier_provider_attempted_once(self):
        self.visible_error = self.no_media("visible")
        self.browser_error = self.no_media("legacy browser")
        info, path = self.run_download()
        self.assertEqual(info["download_method"], "yt-dlp")
        self.assertTrue(path.is_file())
        self.assertEqual(self.events, ["visible", "share", "browser", "yt-dlp:metadata", "model", "yt-dlp:download"])
        self.visible.assert_called_once()
        self.share.assert_called_once()
        self.browser.assert_called_once()
        for options in self.ytdlp_options:
            for name in ("retries", "fragment_retries", "extractor_retries"):
                self.assertEqual(options[name], 0)

    def test_media_failure_can_fallback_without_loading_model_twice(self):
        self.media_errors["visible-browser"] = self.no_media("visible media")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        info, _ = self.run_download()
        self.assertEqual(info["download_method"], "share-page")
        self.assertEqual(self.events, ["visible", "model", "media:visible-browser", "share", "media:share-page"])
        self.model.assert_called_once_with()

    def test_known_knowledge_id_skips_all_providers_model_and_media(self):
        self.existing_transcript()
        info, path = self.run_download()
        self.assertEqual((info["_skip_status"], path), ("skipped_existing", None))
        self.assertEqual(self.events, [])
        self.model.assert_not_called()
        self.media.assert_not_called()

    def test_known_local_output_id_skips_all_providers_and_model(self):
        self.existing_transcript(self.root / "outputs")
        info, path = self.run_download()
        self.assertEqual((info["_skip_status"], path), ("skipped", None))
        self.assertEqual(self.events, [])
        self.model.assert_not_called()

    def test_seen_id_skips_without_starting_browser_or_model(self):
        info, path = self.run_download(seen_ids={VIDEO_ID})
        self.assertEqual((info["_skip_status"], path), ("skipped_duplicate", None))
        self.assertEqual(self.events, [])
        self.model.assert_not_called()

    def test_short_link_dedup_resolves_metadata_without_starting_browser(self):
        self.existing_transcript()
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        info, path = self.run_download(SHORT_SOURCE)
        self.assertEqual((info["_skip_status"], path), ("skipped_existing", None))
        self.assertEqual(self.events, ["share"])
        self.share.assert_called_once()
        self.visible.assert_not_called()
        self.browser.assert_not_called()
        self.model.assert_not_called()
        self.media.assert_not_called()

    def test_short_link_metadata_is_cached_but_visible_media_has_priority(self):
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        info, _ = self.run_download(SHORT_SOURCE)
        self.assertEqual(info["download_method"], "visible-browser")
        self.assertEqual(self.events, ["share", "visible", "model", "media:visible-browser"])
        self.share.assert_called_once()

    def test_short_link_reuses_cached_legacy_metadata_after_visible_failure(self):
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        self.visible_error = self.no_media("visible")
        info, _ = self.run_download(SHORT_SOURCE)
        self.assertEqual(info["download_method"], "share-page")
        self.assertEqual(self.events, ["share", "visible", "model", "media:share-page"])
        self.share.assert_called_once()

    def test_legacy_resolution_cannot_replace_a_known_target_id(self):
        self.visible_error = self.no_media("visible")
        wrong_info = dict(self.info("share-page"), id="67890")
        self.share_result = (wrong_info, "67890", [])
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        self.assertEqual(caught.exception.category, "INVALID_TARGET")
        self.assertEqual(caught.exception.video_id, VIDEO_ID)
        self.assertEqual(self.events, ["visible", "share"])
        self.model.assert_not_called()
        self.media.assert_not_called()

    def test_byte_counts_are_not_mistaken_for_http_access_denials(self):
        self.visible_error = self.no_media("visible")
        self.share_result = (None, VIDEO_ID, ["HTTP 200, 401 bytes, 403 bytes, 429 bytes", "metadata missing"])
        info, _ = self.run_download()
        self.assertEqual(info["download_method"], "playwright")
        self.assertEqual(self.events, ["visible", "share", "browser", "model", "media:playwright"])

    def test_wrapped_explicit_http_denial_still_stops_fallbacks(self):
        self.visible_error = RuntimeError("Wrapped HTTP Error 403: Forbidden")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["visible"])
        self.model.assert_not_called()

    def test_all_recoverable_failures_return_a_clear_final_error_once(self):
        self.visible_error = self.no_media("visible")
        self.browser_error = self.no_media("legacy browser")
        self.ytdlp_error = RuntimeError("yt-dlp has no playable formats")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        message = str(caught.exception)
        for expected in ("visible", "legacy browser", "yt-dlp"):
            self.assertIn(expected, message)
        self.assertEqual(caught.exception.video_id, VIDEO_ID)
        self.assertEqual(self.events, ["visible", "share", "browser", "yt-dlp:metadata"])
        self.model.assert_not_called()

    def test_platform_block_in_visible_metadata_stops_all_fallbacks(self):
        self.visible_error = self.blocked("visible")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["visible"])
        self.model.assert_not_called()

    def test_platform_block_in_media_stops_all_fallbacks(self):
        self.media_errors["visible-browser"] = self.blocked("visible media")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["visible", "model", "media:visible-browser"])

    def test_platform_block_in_legacy_browser_does_not_try_ytdlp(self):
        self.visible_error = self.no_media("visible")
        self.browser_error = self.blocked("legacy browser")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download()
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["visible", "share", "browser"])
        self.model.assert_not_called()

    def test_platform_block_during_short_link_resolution_stops_before_browser(self):
        self.share_error = self.blocked("share resolution")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download(SHORT_SOURCE)
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["share"])
        self.model.assert_not_called()

    def test_model_failure_propagates_without_media_or_route_fallback(self):
        model_error = RuntimeError("local model could not load")
        self.model.side_effect = model_error
        with self.assertRaises(RuntimeError) as caught:
            self.run_download()
        self.assertIs(caught.exception, model_error)
        self.assertEqual(caught.exception.video_id, VIDEO_ID)
        self.assertEqual(self.events, ["visible"])
        self.model.assert_called_once_with()
        self.media.assert_not_called()

    def test_model_failure_at_legacy_stage_does_not_try_ytdlp(self):
        self.visible_error = self.no_media("visible")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        model_error = RuntimeError("local model could not load")
        self.model.side_effect = model_error
        with self.assertRaises(RuntimeError) as caught:
            self.run_download()
        self.assertIs(caught.exception, model_error)
        self.assertEqual(self.events, ["visible", "share"])
        self.model.assert_called_once_with()
        self.media.assert_not_called()

    def test_model_failure_at_ytdlp_stage_does_not_download(self):
        self.visible_error = self.no_media("visible")
        self.browser_error = self.no_media("legacy browser")
        model_error = RuntimeError("local model could not load")
        self.model.side_effect = model_error
        with self.assertRaises(RuntimeError) as caught:
            self.run_download()
        self.assertIs(caught.exception, model_error)
        self.assertEqual(self.events, ["visible", "share", "browser", "yt-dlp:metadata"])
        self.model.assert_called_once_with()
        self.media.assert_not_called()

    def test_missing_local_model_runtime_does_not_trigger_site_fallback(self):
        self.model.side_effect = ModuleNotFoundError("Local faster_whisper runtime missing")
        with self.assertRaises(ModuleNotFoundError):
            self.run_download()
        self.assertEqual(self.events, ["visible"])
        self.model.assert_called_once_with()
        self.media.assert_not_called()

    def test_ytdlp_first_compatibility_does_not_call_visible_route(self):
        info, path = self.run_download(ytdlp_first=True)
        self.assertEqual(info["download_method"], "yt-dlp")
        self.assertTrue(path.is_file())
        self.assertEqual(self.events, ["yt-dlp:metadata", "model", "yt-dlp:download"])
        self.visible.assert_not_called()
        self.share.assert_not_called()

    def test_ytdlp_cookie_requirement_is_terminal_even_with_compatibility_override(self):
        self.ytdlp_error = RuntimeError("Fresh cookies are needed")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        with self.assertRaises(downloads.FallbackError) as caught:
            self.run_download(ytdlp_first=True)
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["yt-dlp:metadata"])
        self.visible.assert_not_called()

    def test_original_audio_missing_retries_once_then_succeeds_without_fallback(self):
        self.visible.side_effect = [downloads.FallbackError("original_audio_not_observed", VIDEO_ID, "NO_MEDIA"),
                                    self.visible_result]
        info, path = self.run_download()
        self.assertEqual(info["download_method"], "visible-browser")
        self.assertTrue(path.is_file())
        self.assertEqual(self.visible.call_count, 2)
        self.model.assert_called_once_with()
        self.share.assert_not_called()
        self.backoff.assert_called_once_with(1)

    def test_two_audio_observations_then_existing_share_fallback(self):
        self.visible_error = downloads.FallbackError("original_audio_not_observed", VIDEO_ID, "NO_MEDIA")
        self.share_result = (self.info("share-page"), VIDEO_ID, [])
        info, _ = self.run_download()
        self.assertEqual(info["download_method"], "share-page")
        self.assertEqual(self.events, ["visible", "visible", "share", "model", "media:share-page"])
        self.assertEqual(self.visible.call_count, 2)
        self.backoff.assert_called_once_with(1)

    def test_media_retry_uses_new_directory_and_does_not_reload_model_or_reuse_partial(self):
        folders = []
        def media(info, directory):
            folders.append(directory)
            (directory / "old.part").write_bytes(b"retained partial")
            if len(folders) == 1:
                raise HTTPError(SOURCE, 503, "Temporary", {}, None)
            self.assertFalse((directory / "stale.m4a").exists())
            target = directory / "media.m4a"
            target.write_bytes(b"successful second attempt")
            return target
        self.media.side_effect = media
        _, path = self.run_download()
        self.assertEqual(len(folders), 2)
        self.assertNotEqual(folders[0], folders[1])
        self.assertEqual((folders[0] / "old.part").read_bytes(), b"retained partial")
        self.assertEqual(path.parent, folders[1])
        self.model.assert_called_once_with()
        self.backoff.assert_called_once_with(1)
        self.share.assert_not_called()

    def test_short_link_resolution_consumes_share_budget_and_cache_is_not_an_extra_attempt(self):
        count = 0
        def share(*args):
            nonlocal count
            count += 1
            self.events.append("share")
            if count == 1:
                raise TimeoutError("read timed out")
            if count > 2:
                self.fail("share route exceeded its two real requests")
            return self.info("share-page"), VIDEO_ID, []
        self.share.side_effect = share
        self.visible_error = self.no_media("visible")
        self.media_errors["share-page"] = TimeoutError("read timed out")
        info, _ = self.run_download(SHORT_SOURCE)
        self.assertEqual(info["download_method"], "playwright")
        self.assertEqual(self.share.call_count, 2)
        self.assertEqual(self.events, ["share", "share", "visible", "model", "media:share-page", "browser", "media:playwright"])
        self.model.assert_called_once_with()
        self.backoff.assert_called_once_with(1)

    def test_ytdlp_first_http_denial_does_not_start_another_route(self):
        self.ytdlp_error = HTTPError(SOURCE, 403, "Forbidden", {}, None)
        with self.assertRaises(Exception) as caught:
            self.run_download(ytdlp_first=True)
        self.assertEqual(tool.trace.category(caught.exception), "BLOCKED_PLATFORM")
        self.assertEqual(self.events, ["yt-dlp:metadata"])
        self.model.assert_not_called()

    def test_no_sync_entry_writes_local_markdown_without_cloud_sync(self):
        active_model = SimpleNamespace(transcribe=Mock(return_value=(
            [SimpleNamespace(start=0, end=1, text="Offline fixture transcript")],
            SimpleNamespace(language="zh"))))
        whisper_factory = Mock(return_value=active_model)
        fake_whisper = SimpleNamespace(WhisperModel=whisper_factory)
        with patch.object(tool, "MODEL_PATH", self.root), patch.dict(sys.modules, {"faster_whisper": fake_whisper}), patch.object(
            knowledge_sync, "sync_outputs", side_effect=AssertionError("--no-sync must stay local")
        ) as sync, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = tool.main(["--workspace", str(self.root), "--no-sync", "--url", SOURCE])
        self.assertEqual(result, 0)
        output = tool.output_for_video_id(VIDEO_ID)
        self.assertTrue(tool.is_complete(output))
        self.assertIn("Offline fixture transcript", output.read_text(encoding="utf-8"))
        sync.assert_not_called()
        whisper_factory.assert_called_once()
        self.assertIs(whisper_factory.call_args.kwargs["local_files_only"], True)
        self.assertEqual(whisper_factory.call_args.kwargs["device"], "cpu")
        self.share.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(self.ytdlp_options, [])


if __name__ == "__main__":
    unittest.main()
