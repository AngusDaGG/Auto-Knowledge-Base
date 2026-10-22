from pathlib import Path
from tempfile import mkdtemp
import json
import errno
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import download_diagnostics as trace
from douyin_download import FallbackError
import douyin_download as downloads


class DiagnosticTests(unittest.TestCase):
    def test_only_explicit_network_interruptions_retry_through_supported_wrappers(self):
        for interruption in (ConnectionResetError, ConnectionAbortedError):
            for wrapped in ('direct', 'reason', 'cause', 'exc_info'):
                inner = interruption('connection interrupted')
                error = inner
                if wrapped == 'reason': error = URLError(inner)
                if wrapped == 'cause':
                    error = RuntimeError('wrapped network failure')
                    error.__cause__ = inner
                if wrapped == 'exc_info':
                    error = RuntimeError('wrapped network failure')
                    error.exc_info = (type(inner), inner, None)
                with self.subTest(interruption=interruption.__name__, wrapped=wrapped):
                    self.assertFalse(trace.local_failure(error))
                    self.assertEqual(trace.retry_reason(error), 'network_interrupted')
        for fault in (PermissionError('denied'), OSError(errno.ENOSPC, 'No space left')):
            self.assertTrue(trace.local_failure(URLError(fault)))
            self.assertIsNone(trace.retry_reason(URLError(fault)))

    def test_media_limit_in_exception_chain_overrides_timeout_and_server_failure(self):
        outer_timeout = TimeoutError('network timed out')
        outer_timeout.__cause__ = FallbackError('too large', category='MEDIA_LIMIT')
        outer_limit = FallbackError('network timed out', category='MEDIA_LIMIT')
        outer_limit.__cause__ = HTTPError('https://private.example/', 503, 'Temporary', {}, None)
        for error in (outer_timeout, outer_limit):
            self.assertEqual(trace.category(error), 'MEDIA_LIMIT')
            self.assertIsNone(trace.retry_reason(error))
        outer_limit.__cause__ = HTTPError('https://private.example/', 403, 'Denied', {}, None)
        self.assertEqual(trace.category(outer_limit), 'BLOCKED_PLATFORM')
        self.assertIsNone(trace.retry_reason(outer_limit))

    def test_retry_policy_follows_wrapped_denials_before_transient_timeout(self):
        for code in (401, 403, 412, 429, 451):
            denied = HTTPError('https://private.example/?signature=SECRET', code, 'Denied', {}, None)
            for wrapped in ('cause', 'context', 'reason', 'exc_info'):
                error = RuntimeError('network timed out')
                if wrapped == 'cause': error.__cause__ = denied
                if wrapped == 'context': error.__context__ = denied
                if wrapped == 'reason': error = URLError(denied)
                if wrapped == 'exc_info': error.exc_info = (type(denied), denied, None)
                with self.subTest(code=code, wrapped=wrapped):
                    self.assertEqual(trace.category(error), 'BLOCKED_PLATFORM')
                    self.assertIsNone(trace.retry_reason(error))

    def test_retry_policy_limits_recovery_and_stops_nested_local_faults(self):
        for error in (TimeoutError('read timed out'), URLError(TimeoutError('network timed out')),
                      subprocess.TimeoutExpired('fixture', 75), FallbackError('original_audio_not_observed', category='NO_MEDIA'),
                      FallbackError('complete_original_audio_not_observed', category='NO_SUPPORTED_MEDIA'),
                      FallbackError('ready_player_not_observed', category='NO_MEDIA'),
                      FallbackError('observation_deadline_exceeded', category='LOCAL_BROWSER_ERROR')):
            self.assertIsNotNone(trace.retry_reason(error))
        for code in (408, 500, 503, 599):
            self.assertIsNotNone(trace.retry_reason(HTTPError('https://private.example/', code, 'Temporary', {}, None)))
        diagnostic_timeout = FallbackError('browser_navigation_or_runtime_error', category='LOCAL_BROWSER_ERROR')
        diagnostic_timeout.diagnostics = {'failure_phase': 'observation', 'error_type': 'ProbeTimeout'}
        self.assertEqual(trace.retry_reason(diagnostic_timeout), 'observation_timeout')
        for error in (URLError(PermissionError('permission denied')), URLError(OSError(errno.ENOSPC, 'No space left')),
                      ModuleNotFoundError('local dependency'), FallbackError('browser unavailable', category='BROWSER_UNAVAILABLE')):
            self.assertTrue(trace.local_failure(error))
            self.assertIsNone(trace.retry_reason(error))
        self.assertIsNone(trace.retry_reason(RuntimeError('unknown extractor structure')))
        self.assertIsNone(trace.retry_reason(FallbackError('ambiguous_audio', category='NO_SUPPORTED_MEDIA')))
        self.assertEqual(trace.category(RuntimeError('HTTP 200, 401 bytes, 403 bytes, 451 bytes')), 'UNKNOWN')

    def test_attempt_fields_are_bounded_integers_and_retry_trace_has_no_payload(self):
        folder = Path(mkdtemp(prefix='retry_trace_'))
        path = folder/'trace.jsonl'
        with trace.Trace(path):
            trace.event('route_retry', 'scheduled', method='yt-dlp', attempt=1, max_attempts=2,
                        reason_code='network_timeout', payload='https://private.example/?signature=SECRET')
            trace.event('route_retry', 'invalid', attempt=True, max_attempts=3)
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual((data['events'][0]['attempt'], data['events'][0]['max_attempts']), (1, 2))
        self.assertNotIn('attempt', data['events'][1])
        self.assertNotIn('max_attempts', data['events'][1])
        self.assertNotIn('SECRET', path.read_text(encoding='utf-8'))
        self.assertNotIn('https://', path.read_text(encoding='utf-8'))

    def test_safe_trace_distinguishes_unattempted_stages_and_platform_block(self):
        folder = Path(mkdtemp(prefix="diagnostic_test_"))
        path = folder / "trace.jsonl"
        try:
            with self.assertRaises(FallbackError):
                with trace.Trace(path, "12345"):
                    with trace.stage("metadata", api_type="aweme_detail"):
                        raise FallbackError("do not persist signature=SECRET", "12345", "BLOCKED_PLATFORM")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "BLOCKED_PLATFORM")
            self.assertEqual(data["stages"]["media_download"], "not_attempted")
            self.assertEqual(data["stages"]["metadata"], "BLOCKED_PLATFORM")
            self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(data["events"][0]["elapsed_ms"], 0)
        finally:
            if path.exists():
                path.unlink()
            folder.rmdir()

    def test_reason_redaction_and_response_shape(self):
        value = trace.safe_reason('failure https://example.com/video/123?signature=SECRET#token; Cookie: SESSION_SECRET')
        self.assertNotIn("SECRET", value)
        self.assertNotIn("SIGNED_PATH", trace.safe_reason("https://cdn.example/SIGNED_PATH?token=SECRET"))
        self.assertEqual(trace.response_kind(b""), "empty")
        self.assertEqual(trace.response_kind(b"<html>Forbidden</html>"), "html")
        self.assertEqual(trace.response_kind(b'{"status_code":0}'), "json")

    def test_a_denied_response_does_not_relabel_a_separate_http_200_response(self):
        folder = Path(mkdtemp(prefix="diagnostic_test_"))
        path = folder / "trace.jsonl"
        payload = {"ok": False, "category": "BLOCKED_PLATFORM", "reason": "HTTP 403", "diagnostics": {
            "navigation_status": 200, "browser_closed": True, "detail_responses": [
                {"status": 200, "body_type": "json", "target_found": True}, {"status": 403, "body_type": "text", "access_denied": True}]}}
        result = SimpleNamespace(returncode=1, stdout=json.dumps(payload))
        try:
            with self.assertRaises(FallbackError), trace.Trace(path, "12345"), patch.object(
                downloads.subprocess, "run", return_value=result
            ), patch.object(downloads, "browser_runtime", return_value=(Path("node"), Path("playwright"))):
                downloads.browser_info("12345")
            data = json.loads(path.read_text(encoding="utf-8"))
            responses = [event for event in data["events"] if event.get("api_type") == "aweme_detail"]
            self.assertEqual([event["status"] for event in responses], ["success", "BLOCKED_PLATFORM"])
            self.assertEqual(data["status"], "BLOCKED_PLATFORM")
        finally:
            if path.exists():
                path.unlink()
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
