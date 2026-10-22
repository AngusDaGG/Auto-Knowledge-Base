"""Offline Gemini contracts; no SDK install, real key, or network request."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import kb_gemini as gemini


def notes(**changes):
    value = {"title": "Offline video fixture", "refusal": False,
             **{name: [] for name in gemini.LIST_FIELDS}}
    value["key_points"] = ["A source claim with an explicit limitation"]
    value["limitations"] = ["Offline mock; not a real video acceptance result"]
    value["timestamps"] = ["00:05 source claim"]
    return dict(value, **changes)


def response(text=None, *, finish="STOP", block=None, payload=None):
    if text is None:
        text = json.dumps(notes() if payload is None else payload)
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=finish,
                                    content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))],
        prompt_feedback=SimpleNamespace(block_reason=block),
        model_version="gemini-offline-fixture",
        usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=7,
                                       total_token_count=19))


class GeminiTests(unittest.TestCase):
    def setUp(self):
        # Retain fixtures under ignored state; never recursively clean directories.
        fixtures = Path(__file__).parent / "state" / "test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="gemini_offline_", dir=fixtures))
        self.env_file = self.root / ".env"
        # Synthetic values are purposely not shaped as a real API key.
        self.process_value = "offline-process-fixture"
        self.file_value = "offline-file-fixture"
        self.env_patch = patch.dict(os.environ, {"GEMINI_API_KEY": self.process_value,
                                                 "GOOGLE_API_KEY": "offline-unrelated-fixture"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.client = Mock()
        self.client.models.generate_content.return_value = response()
        self.factory = Mock(return_value=self.client)
        self.sleep = Mock()
        self.url = "https://youtu.be/Tw18-4U7mts?si=tracking"

    def collect(self, **kwargs):
        options = {"enabled": True, "budget_authorized": True, "model": "gemini-offline-fixture",
                   "client_factory": self.factory, "sleep_fn": self.sleep}
        options.update(kwargs)
        return gemini.collect_video(self.root, self.url, **options)

    def assert_error(self, expected, **kwargs):
        with self.assertRaises(gemini.GeminiError) as caught:
            self.collect(**kwargs)
        # A boolean assertion ensures failed test output cannot snapshot values.
        self.assertTrue(caught.exception.error_class == expected)
        self.assertFalse(self.process_value in str(caught.exception))
        self.assertFalse(self.file_value in str(caught.exception))

    def test_module_import_never_imports_optional_sdk(self):
        original = builtins.__import__

        def no_google(name, *args, **kwargs):
            if name == "google" or name.startswith("google."):
                raise AssertionError("optional SDK imported eagerly")
            return original(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("gemini_import_fixture", gemini.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=no_google), patch.object(Path, "read_text", side_effect=AssertionError("unexpected file read")):
            spec.loader.exec_module(module)

    def test_disabled_and_unfunded_do_not_read_key_or_construct_client(self):
        with patch.object(gemini, "_select_key", side_effect=AssertionError("key read")), patch.object(gemini, "_sdk_version", side_effect=AssertionError("SDK inspected")), patch.object(gemini.importlib, "import_module", side_effect=AssertionError("SDK import")):
            self.assert_error("Disabled", enabled=False)
            self.assert_error("BudgetNotAuthorized", budget_authorized=False)
        self.factory.assert_not_called()

    def test_truthy_strings_cannot_enable_or_authorize(self):
        self.assert_error("Disabled", enabled="true")
        self.assert_error("BudgetNotAuthorized", budget_authorized="true")
        self.factory.assert_not_called()

    def test_model_must_be_explicit_before_key_read(self):
        with patch.object(gemini, "_select_key", side_effect=AssertionError("key read")):
            self.assert_error("ModelRequired", model=None)
        self.factory.assert_not_called()

    def test_missing_sdk_is_safe_error(self):
        with patch.object(gemini.importlib, "import_module", side_effect=ModuleNotFoundError(self.process_value)):
            self.assert_error("MissingSDK", client_factory=None)

    def test_key_priority_avoids_file_and_google_key(self):
        with patch.object(gemini, "_dotenv_key", side_effect=AssertionError("file read")):
            self.collect()
        supplied = self.factory.call_args.kwargs
        self.assertTrue(supplied["api_key"] == self.process_value)
        self.assertFalse(supplied["vertexai"])
        self.assertTrue(supplied["http_options"]["retry_options"] == {"attempts": 1})
        self.assertTrue(supplied["http_options"]["timeout"] == 60000)

    def test_empty_process_value_uses_explicit_env_without_mutation(self):
        self.env_file.write_text("UNRELATED=ignored\nexport GEMINI_API_KEY = 'offline-file-fixture' # comment\n", encoding="utf-8")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "  "}):
            self.collect(env_path=self.env_file)
            self.assertTrue(os.environ["GEMINI_API_KEY"] == "  ")
        self.assertTrue(self.factory.call_args.kwargs["api_key"] == self.file_value)

    def test_missing_key_does_not_fall_back_to_google_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assert_error("MissingAPIKey")
        self.factory.assert_not_called()

    def test_status_contains_only_safe_values_and_never_imports_sdk(self):
        with patch.object(gemini, "_sdk_version", return_value="1.2.3"), patch.object(gemini.importlib, "import_module", side_effect=AssertionError("SDK import")):
            status = gemini.get_status(self.root)
        self.assertTrue(status["key_set"])
        self.assertFalse(status["enabled"])
        self.assertTrue(all(type(value) is bool or value is None or value == "1.2.3"
                            for value in status.values()))
        self.assertFalse(self.process_value in json.dumps(status))
        self.factory.assert_not_called()

    def test_invalid_env_is_redacted_in_status_and_collection(self):
        self.env_file.write_text("GEMINI_API_KEY='unterminated\n", encoding="utf-8")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            status = gemini.get_status(self.root)
            self.assertTrue(status["error_class"] == "EnvironmentFileError")
            self.assert_error("EnvironmentFileError")
        self.factory.assert_not_called()

    def test_video_is_real_file_data_part_and_only_fixed_prompt_leaves_machine(self):
        result = self.collect()
        request = self.client.models.generate_content.call_args.kwargs
        parts = request["contents"][0]["parts"]
        self.assertTrue(parts[0] == {"file_data": {"file_uri": "https://www.youtube.com/watch?v=Tw18-4U7mts", "mime_type": "video/*"}})
        self.assertTrue(parts[1] == {"text": gemini.INSTRUCTION})
        self.assertTrue(len(parts) == 2)
        self.assertFalse(str(self.root) in json.dumps(request))
        self.assertFalse(self.process_value in json.dumps(request))
        self.assertTrue(result["content_type"] == "gemini_video_notes")
        self.assertTrue(result["evidence"] == "model_generated_video_notes_not_asr")
        self.assertTrue(result["usage"]["total_token_count"] == 19)
        self.client.close.assert_called_once()

    def test_url_validation_rejects_spoofing_and_canonicalizes(self):
        for url in ("https://youtube.com.attacker.invalid/watch?v=Tw18-4U7mts",
                    "https://www.youtube.com@attacker.invalid/watch?v=Tw18-4U7mts",
                    "file:///private", "https://www.youtube.com/watch?v=bad",
                    "https://www.youtube.com/watch?v=Tw18-4U7mts&v=abcdefghijk"):
            with self.subTest(case=url):
                with self.assertRaises(gemini.GeminiError):
                    gemini.canonical_youtube_url(url)
        for url in (self.url, "https://www.youtube.com/shorts/Tw18-4U7mts?feature=share",
                    "https://m.youtube.com/watch?v=Tw18-4U7mts&list=ignored"):
            self.assertTrue(gemini.canonical_youtube_url(url) == "https://www.youtube.com/watch?v=Tw18-4U7mts")

    def test_response_blocks_refusal_truncation_and_empty_stop_without_retry(self):
        cases = [(response(block="SAFETY"), "BlockedResponse"),
                 (response(finish="SAFETY"), "BlockedResponse"),
                 (response(finish="MAX_TOKENS"), "TruncatedResponse"),
                 (response(text=" "), "EmptyResponse"),
                 (response(finish="OTHER"), "IncompleteResponse"),
                 (response(payload=notes(refusal=True)), "RefusedResponse"),
                 (response(payload=notes(key_points=[])), "EmptyResponse"),
                 (response(text="not JSON"), "InvalidNotesJSON"),
                 (response(payload={"title": "partial"}), "InvalidNotesSchema")]
        for payload, error_class in cases:
            with self.subTest(error_class=error_class):
                self.client.models.generate_content.reset_mock()
                self.client.models.generate_content.return_value = payload
                self.assert_error(error_class, max_retries=2)
                self.assertTrue(self.client.models.generate_content.call_count == 1)
        self.sleep.assert_not_called()

    def test_thought_parts_are_not_published(self):
        payload = response()
        payload.candidates[0].content.parts.insert(0, SimpleNamespace(text="internal thought", thought=True))
        self.client.models.generate_content.return_value = payload
        self.assertTrue(self.collect()["notes"]["title"] == "Offline video fixture")

    def test_quota_auth_errors_are_redacted_and_never_retried(self):
        for code in (401, 403, 429, 400):
            with self.subTest(code=code):
                error = RuntimeError(self.process_value + " " + self.file_value)
                error.code = code
                self.client.models.generate_content.reset_mock()
                self.client.models.generate_content.side_effect = error
                expected = ("AuthenticationOrPermissionError" if code in {401, 403}
                            else "QuotaOrRateLimitError" if code == 429 else "APIError")
                self.assert_error(expected, max_retries=2)
                self.assertTrue(self.client.models.generate_content.call_count == 1)
        self.sleep.assert_not_called()

    def test_transient_retry_is_bounded_and_sdk_retries_are_off(self):
        error = RuntimeError("offline transient fixture")
        error.code = 503
        self.client.models.generate_content.side_effect = [error, error, response()]
        result = self.collect(max_retries=2)
        self.assertTrue(result["attempts"] == 3)
        self.assertTrue(result["request_usage"][0]["usage"] is None)
        self.assertTrue(result["request_usage"][2]["usage"]["total_token_count"] == 19)
        self.assertTrue(self.client.models.generate_content.call_count == 3)
        self.assertTrue([call.args[0] for call in self.sleep.call_args_list] == [1, 2])

    def test_truncated_response_keeps_usage_metadata_without_raw_response(self):
        self.client.models.generate_content.return_value = response(finish="MAX_TOKENS")
        with self.assertRaises(gemini.GeminiError) as caught:
            self.collect()
        details = caught.exception.request_metadata
        self.assertTrue(details["attempts"] == 1)
        self.assertTrue(details["request_usage"][0]["usage"]["total_token_count"] == 19)
        self.assertTrue(details["content_type"] == "gemini_video_notes")
        self.assertFalse(self.process_value in json.dumps(details))

    def test_timeout_exhaustion_and_unknown_errors_are_safe(self):
        self.client.models.generate_content.side_effect = TimeoutError(self.process_value)
        self.assert_error("TransientNetworkError", max_retries=1)
        self.assertTrue(self.client.models.generate_content.call_count == 2)
        self.client.models.generate_content.reset_mock()
        self.client.models.generate_content.side_effect = ValueError(self.process_value)
        self.assert_error("APIError", max_retries=2)
        self.assertTrue(self.client.models.generate_content.call_count == 1)

    def test_client_and_cleanup_errors_do_not_expose_exception_text(self):
        self.factory.side_effect = RuntimeError(self.process_value)
        self.assert_error("ClientError")
        self.factory.side_effect = None
        self.client.close.side_effect = RuntimeError(self.process_value)
        self.assertTrue(self.collect()["content_type"] == "gemini_video_notes")

    def test_limit_validation_precedes_key_or_client(self):
        for options in ({"timeout_seconds": float("inf")}, {"timeout_seconds": 0},
                        {"max_retries": 3}, {"max_retries": True}, {"max_output_tokens": 0}):
            with self.subTest(options=options):
                with patch.object(gemini, "_select_key", side_effect=AssertionError("key read")):
                    self.assert_error("InvalidLimits", **options)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
