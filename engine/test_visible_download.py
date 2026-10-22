"""Offline Python probe and duration contracts; no real browser or audio decoder."""

from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import douyin_download as downloads
import download_diagnostics as trace


VIDEO_ID = "12345"
SOURCE = f"https://www.douyin.com/video/{VIDEO_ID}"
TEST_MEDIA = "https://v3.douyinvod.com/media-audio-test?signature=TEST_ONLY_SIGNATURE"


class VisibleProbeTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(tempfile.gettempdir()) / "akb-engine-visible"
        fixtures.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=self._testMethodName[5:35] + "-", dir=fixtures))
        self.profile = self.root / "state" / "visible-browser-profile"
        self.info = {"id": VIDEO_ID, "title": "Fixture title", "expected_duration": 350.0,
                     "video": {"audio": {"play_url": {"url_list": [TEST_MEDIA]}}}}
        self.payload = {"ok": True, "info": self.info,
                        "diagnostics": {"navigation_status": 200, "browser_closed": True,
                                        "timings_ms": {"navigation": 2}, "unexpected_url": TEST_MEDIA}}
        self.runtime = patch.object(downloads, "browser_runtime", return_value=(Path("fixture-node"), Path("fixture-module")))
        self.runtime_mock = self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.process = patch.object(downloads.subprocess, "run", side_effect=self.response)
        self.process_mock = self.process.start()
        self.addCleanup(self.process.stop)

    def response(self, *_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.payload))

    def inspect(self):
        return downloads.visible_browser_info(SOURCE, VIDEO_ID, self.profile)

    def test_probe_uses_confirmed_target_and_dedicated_profile_with_bounded_runtime(self):
        info = self.inspect()
        self.assertEqual(info["download_method"], "visible-browser")
        self.assertEqual(info["expected_duration"], 350.0)
        self.assertTrue(self.profile.is_dir())
        self.runtime_mock.assert_called_once_with(VIDEO_ID)
        command = self.process_mock.call_args.args[0]
        self.assertEqual(command[-3:], [SOURCE, VIDEO_ID, str(self.profile.resolve())])
        self.assertEqual(self.process_mock.call_args.kwargs["timeout"], 75)

    def test_probe_target_mismatch_is_terminal(self):
        self.info["id"] = "67890"
        with self.assertRaises(downloads.FallbackError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.category, "INVALID_TARGET")
        self.assertEqual(caught.exception.video_id, VIDEO_ID)

    def test_probe_requires_a_finite_positive_player_duration(self):
        for value in (None, 0, -1, float("nan"), float("inf"), "350"):
            with self.subTest(duration=value):
                self.info["expected_duration"] = value
                with self.assertRaises(downloads.FallbackError) as caught:
                    self.inspect()
                self.assertEqual(caught.exception.category, "INVALID_TARGET")

    def test_diagnostics_do_not_store_media_payloads_or_signed_urls(self):
        log = self.root / "diagnostics.jsonl"
        with trace.Trace(log):
            self.inspect()
        contents = log.read_text(encoding="utf-8")
        for forbidden in (TEST_MEDIA, "TEST_ONLY_SIGNATURE", "Fixture title", "unexpected_url", "https://"):
            self.assertNotIn(forbidden, contents)
        record = json.loads(contents)
        self.assertEqual(record["video_id"], VIDEO_ID)
        self.assertEqual(record["stages"]["metadata"], "success")

    def test_probe_denial_keeps_category_and_redacts_signed_reason(self):
        self.payload = {"ok": False, "category": "BLOCKED_PLATFORM",
                        "reason": "HTTP 403 " + TEST_MEDIA, "diagnostics": {}}
        with self.assertRaises(downloads.FallbackError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
        self.assertNotIn("TEST_ONLY_SIGNATURE", str(caught.exception))
        self.assertNotIn("media-audio-test", str(caught.exception))

    def test_observation_and_each_ready_player_survive_wrapper_on_success_and_denial(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                self.payload["ok"] = not blocked
                self.payload.update(category="BLOCKED_PLATFORM", reason="media_access_denied")
                self.payload["diagnostics"].update({
                    "timings_ms": {"navigation": 2, "observation": 25006},
                    "audio_resources": 0, "active_players": 0, "ready_players": 2,
                    "response_excluded": {"frame": 1, "target": 2, "path": 3, "mime": 4, "status": 5,
                                          "unexpected_url": TEST_MEDIA},
                    "ready_player_observations": [
                        {"player_index": 0, "current_src_kind": "blob", "paused": True, "muted": True,
                         "duration_seconds": 644.726, "currentSrc": TEST_MEDIA},
                        {"player_index": 1, "current_src_kind": "allowlisted_direct", "paused": False,
                         "muted": False, "duration_seconds": 350.0}],
                })
                log = self.root / ("blocked.jsonl" if blocked else "success.jsonl")
                if blocked:
                    with self.assertRaises(downloads.FallbackError) as caught, trace.Trace(log):
                        self.inspect()
                    self.assertEqual(caught.exception.category, "BLOCKED_PLATFORM")
                else:
                    with trace.Trace(log):
                        self.inspect()
                data = json.loads(log.read_text(encoding="utf-8"))
                observation = next(event for event in data["events"] if event["stage"] == "browser_observation")
                self.assertEqual(observation["elapsed_ms"], 25006)
                self.assertNotIn("error_type", observation)
                self.assertNotIn("source_kind", observation)
                self.assertEqual([observation[key] for key in ("audio_resources", "active_players", "ready_players")], [0, 0, 2])
                self.assertEqual([observation["response_excluded_" + key] for key in
                                  ("frame", "target", "path", "mime", "status")], [1, 2, 3, 4, 5])
                players = [event for event in data["events"] if event["stage"] == "browser_player"]
                self.assertEqual([event["player_index"] for event in players], [0, 1])
                self.assertEqual([event["current_src_kind"] for event in players], ["blob", "allowlisted_direct"])
                self.assertEqual([event["paused"] for event in players], [True, False])
                self.assertEqual([event["muted"] for event in players], [True, False])
                self.assertEqual([event["duration_seconds"] for event in players], [644.726, 350.0])
                self.assertEqual(data["stages"]["metadata"], "BLOCKED_PLATFORM" if blocked else "success")
                self.assertEqual(data["stages"]["media_download"], "not_attempted")
                for forbidden in (TEST_MEDIA, "TEST_ONLY_SIGNATURE", "currentSrc", "unexpected_url", "https://"):
                    self.assertNotIn(forbidden, log.read_text(encoding="utf-8"))

    def test_observation_trace_rejects_unknown_enums_payloads_and_nonfinite_numbers(self):
        self.payload["diagnostics"].update({
            "timings_ms": {"observation": float("nan")},
            "error_type": "PRIVATE", "failure_phase": TEST_MEDIA,
            "audio_resources": "PRIVATE", "active_players": -1, "ready_players": True,
            "response_excluded": {"frame": float("inf"), "target": "PRIVATE", "path": False,
                                  "mime": 0.5, "status": -1},
            "ready_player_observations": [
                {"player_index": 0, "current_src_kind": "PRIVATE", "paused": "PRIVATE", "muted": 1,
                 "duration_seconds": float("inf"), "unknown": {"url": TEST_MEDIA}},
                {"player_index": "PRIVATE", "current_src_kind": TEST_MEDIA, "duration_seconds": True},
                TEST_MEDIA],
        })
        log = self.root / "malformed.jsonl"
        with trace.Trace(log):
            self.inspect()
        data = json.loads(log.read_text(encoding="utf-8"))
        observation = next(event for event in data["events"] if event["stage"] == "browser_observation")
        self.assertEqual(observation, {"stage": "browser_observation", "status": "observed",
                                      "elapsed_ms": 0, "method": "visible-browser"})
        players = [event for event in data["events"] if event["stage"] == "browser_player"]
        self.assertEqual(players, [
            {"stage": "browser_player", "status": "observed", "elapsed_ms": 0, "method": "visible-browser", "player_index": 0},
            {"stage": "browser_player", "status": "observed", "elapsed_ms": 0, "method": "visible-browser"}])
        contents = log.read_text(encoding="utf-8")
        for forbidden in ("PRIVATE", "TEST_ONLY_SIGNATURE", "https://", "Infinity", "NaN", "unknown"):
            self.assertNotIn(forbidden, contents)

    def test_observation_deadline_trace_preserves_phase_error_and_elapsed_without_payload(self):
        self.payload.update(ok=False, category="LOCAL_BROWSER_ERROR", reason="observation_deadline_exceeded")
        self.payload["diagnostics"].update({
            "timings_ms": {"navigation": 622, "observation": 25006},
            "error_type": "ProbeTimeout", "failure_phase": "observation",
            "audio_resources": 0, "active_players": 1, "ready_players": 1,
            "response_excluded": {"frame": 0, "target": 0, "path": 217, "mime": 0, "status": 0},
            "ready_player_observations": [
                {"player_index": 0, "current_src_kind": "blob", "paused": False, "muted": False,
                 "duration_seconds": 644.726, "currentSrc": TEST_MEDIA}],
            "body": TEST_MEDIA,
        })
        log = self.root / "observation-timeout.jsonl"
        with self.assertRaises(downloads.FallbackError) as caught, trace.Trace(log):
            self.inspect()
        self.assertEqual(caught.exception.category, "LOCAL_BROWSER_ERROR")
        self.assertIn("observation_deadline_exceeded", str(caught.exception))
        data = json.loads(log.read_text(encoding="utf-8"))
        observation = next(event for event in data["events"] if event["stage"] == "browser_observation")
        self.assertEqual(observation["elapsed_ms"], 25006)
        self.assertEqual(observation["error_type"], "ProbeTimeout")
        self.assertEqual(observation["source_kind"], "observation")
        self.assertEqual(observation["response_excluded_path"], 217)
        self.assertEqual(data["category"], "LOCAL_BROWSER_ERROR")
        self.assertEqual(data["stages"]["media_download"], "not_attempted")
        for forbidden in ("failure_phase", "body", "currentSrc", TEST_MEDIA, "TEST_ONLY_SIGNATURE", "https://"):
            self.assertNotIn(forbidden, log.read_text(encoding="utf-8"))

    def test_probe_timeout_is_a_classified_local_browser_failure(self):
        self.process_mock.side_effect = subprocess.TimeoutExpired("fixture", 75)
        with self.assertRaises(downloads.FallbackError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.category, "LOCAL_BROWSER_ERROR")
        self.assertEqual(caught.exception.stage, "page_load")

    def test_missing_runtime_is_explicit_and_does_not_launch_or_create_profile(self):
        self.runtime_mock.side_effect = downloads.FallbackError(
            "Node or Playwright missing; no installation attempted", VIDEO_ID,
            "LOCAL_BROWSER_ERROR", "page_load")
        with self.assertRaises(downloads.FallbackError) as caught:
            self.inspect()
        self.assertEqual(caught.exception.category, "LOCAL_BROWSER_ERROR")
        self.process_mock.assert_not_called()
        self.assertFalse(self.profile.exists())

    def test_unconfirmed_target_does_not_launch_or_create_profile(self):
        with self.assertRaises(downloads.FallbackError):
            downloads.visible_browser_info(SOURCE, "", self.profile)
        self.runtime_mock.assert_not_called()
        self.process_mock.assert_not_called()
        self.assertFalse(self.profile.exists())


class DurationValidationTests(unittest.TestCase):
    def validate_fixture(self, duration, expected=350.0, audio=True, frames=True):
        # This verifies validation policy only, not actual decoding or playable bytes.
        container = Mock()
        container.__enter__ = Mock(return_value=container)
        container.__exit__ = Mock(return_value=None)
        container.streams.audio = [object()] if audio else []
        container.duration = duration * 1_000_000 if duration is not None else None
        container.decode.return_value = iter([object()] if frames else [])
        fake_av = SimpleNamespace(open=Mock(return_value=container), time_base=1_000_000)
        with patch.dict(sys.modules, {"av": fake_av}):
            downloads.validate_media(Path("not-read-by-mocked-decoder.m4a"), expected_duration=expected)

    def test_matching_player_and_media_duration_is_accepted(self):
        self.validate_fixture(350.0)

    def test_mismatched_missing_or_nonfinite_duration_is_terminal(self):
        for value in (120, 370, None, float("nan"), float("inf")):
            with self.subTest(duration=value), self.assertRaises(downloads.FallbackError) as caught:
                self.validate_fixture(value)
            self.assertEqual(caught.exception.category, "INVALID_TARGET")
            self.assertEqual(caught.exception.stage, "audio_processing")

    def test_no_audio_or_no_decodable_frame_is_rejected(self):
        for audio, frames in ((False, True), (True, False)):
            with self.subTest(audio=audio, frames=frames), self.assertRaises(downloads.FallbackError):
                self.validate_fixture(350, audio=audio, frames=frames)


if __name__ == "__main__":
    unittest.main()
