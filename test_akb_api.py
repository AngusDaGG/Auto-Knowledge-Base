"""Offline API contract tests, including a real loopback HTTP transport fixture."""

import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ssl
import threading
import unittest
from unittest import mock

import akb_api


SOURCE = ("[00:05] The author proposes testing one clear hypothesis at a time.\n"
          "[00:15] Keep a written baseline, then compare observed outcomes fairly.\n"
          "[00:25] Small samples cannot establish a broadly reliable conclusion.")
METADATA = {"id": "youtube_fixture123", "platform": "youtube", "title": "Testing claims",
            "source_url": "https://example.invalid/watch/fixture123",
            "content_type": "transcript", "duration_seconds": 60}
SETTINGS = {"endpoint": "http://127.0.0.1:1234/v1/chat/completions", "model": "test-model"}


def draft():
    return {"source_id": METADATA["id"], "title": "假說驗證與證據界線",
            "summary": "作者主張：每次只驗證一個明確假說，保留比較基準，並承認小樣本無法證實廣泛有效的結論。",
            "core_knowledge": ["作者主張：假說需要明確且可觀察的驗證結果。"],
            "methods": ["作者主張：先記錄比較基準，再觀察結果並公平比較。"],
            "conditions": ["AI 推論：比較條件需要清楚記錄，否則不能排除混淆。"],
            "risks": ["作者主張：少量樣本不足以證明結論普遍可靠。"],
            "examples": ["AI 推論：來源未提供具體案例，不自行捏造測試成效。"],
            "topics": ["證據驗證"], "aliases": ["hypothesis testing"],
            "evidence": akb_api.EVIDENCE,
            "anchors": [{"quote": "The author proposes testing one clear hypothesis at a time.",
                         "explanation": "作者主張：原文直接說明每次驗證一個假說。",
                         "timestamp_seconds": 5}]}


def response(value=None, finish="stop", **message_fields):
    message = {"role": "assistant", "refusal": None,
               "content": json.dumps(draft() if value is None else value, ensure_ascii=False)}
    message.update(message_fields)
    return json.dumps({"choices": [{"message": message, "finish_reason": finish}],
                       "model": "untrusted-server-label", "usage": {
                           "prompt_tokens": 120, "completion_tokens": 100, "total_tokens": 220,
                           "untrusted": "do not persist"}}, ensure_ascii=False).encode()


@contextlib.contextmanager
def server(replies):
    """No external requests; errors deliberately contain a synthetic secret."""
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            captured.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            status, body, headers = replies[min(len(captured) - 1, len(replies) - 1)]
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{service.server_port}/v1/chat/completions", captured
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)


class RefinementTests(unittest.TestCase):
    def refine(self, value=None, settings=None, metadata=None, source=SOURCE):
        with mock.patch.object(akb_api, "_request", return_value=(200, response(value))):
            return akb_api.refine(source, METADATA if metadata is None else metadata,
                                  ["驗證方法"], SETTINGS if settings is None else settings)

    def assert_code(self, code, function):
        with self.assertRaises(akb_api.RefinementError) as result:
            function()
        self.assertEqual(result.exception.code, code)

    def test_complete_draft_is_returned_with_safe_usage_and_model(self):
        result = self.refine()
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["usage"], {"prompt_tokens": 120, "completion_tokens": 100,
                                          "total_tokens": 220})
        self.assertEqual(result["claim_status"], "author_claims_and_ai_inference")
        self.assertEqual(result["source_id"], METADATA["id"])

    def test_real_loopback_payload_and_explicit_key(self):
        metadata = dict(METADATA, unrelated_private_data="never send this")
        with server([(200, response(), {})]) as (endpoint, captured):
            settings = dict(SETTINGS, endpoint=endpoint, api_key_env="AKB_UNIT_TEST_KEY")
            with mock.patch.dict(os.environ, {"AKB_UNIT_TEST_KEY": "synthetic-test-value"}):
                result = akb_api.refine(SOURCE, metadata, ["測試方法"], settings)
        self.assertEqual(len(captured), 1)
        sent = captured[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer synthetic-test-value")
        payload = sent["body"]
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["max_completion_tokens"], 6000)
        self.assertIs(payload["stream"], False)
        self.assertIs(payload["response_format"]["json_schema"]["strict"], True)
        task = json.loads(payload["messages"][1]["content"])
        self.assertEqual(task["source_text"], SOURCE)
        self.assertEqual(task["metadata"], METADATA)
        self.assertNotIn("synthetic-test-value", json.dumps(result))
        self.assertNotIn("unrelated_private_data", json.dumps(payload))

    def test_remote_default_does_not_read_keys_or_send(self):
        settings = dict(SETTINGS, endpoint="https://example.invalid/v1/chat/completions",
                        api_key_env="AKB_UNIT_TEST_KEY")
        with mock.patch.object(akb_api, "_request") as request, mock.patch.object(os.environ, "get") as env:
            self.assert_code("remote_not_authorized", lambda: self.refine(settings=settings))
        request.assert_not_called()
        env.assert_not_called()

    def test_explicit_remote_requires_key_environment_selection(self):
        settings = dict(SETTINGS, endpoint="https://example.invalid/v1/chat/completions",
                        allow_remote=True)
        self.assert_code("missing_key_env", lambda: self.refine(settings=settings))

    def test_plain_http_remote_cannot_be_enabled(self):
        settings = dict(SETTINGS, endpoint="http://example.invalid/v1/chat/completions",
                        allow_remote=True)
        self.assert_code("https_required", lambda: self.refine(settings=settings))

    def test_malformed_or_credential_bearing_endpoints_rejected(self):
        endpoints = ["https://user:pass@example.invalid/v1/chat/completions",
                     "https://example.invalid/v1/chat/completions?key=value",
                     "https://example.invalid/v1/chat/completions#fragment",
                     "file:///v1/chat/completions", "https://x.invalid:bad/v1/chat/completions",
                     "https://example.invalid/wrong", "https://example.invalid\\@127.0.0.1/v1/chat/completions"]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.assert_code("invalid_endpoint", lambda: self.refine(settings=dict(SETTINGS, endpoint=endpoint)))

    def test_inline_key_and_transport_overrides_rejected(self):
        for key in ("api_key", "verify_tls", "max_attempts", "proxy"):
            with self.subTest(key=key):
                self.assert_code("unknown_settings", lambda: self.refine(settings=dict(SETTINGS, **{key: "x"})))

    def test_source_never_sliced(self):
        self.assert_code("empty_or_oversized_source", lambda: self.refine(source=SOURCE * 300))
        self.assert_code("empty_or_oversized_source", lambda: self.refine(source=SOURCE + " " * 60_000))

    def test_obvious_secrets_rejected_before_key_read_and_request(self):
        candidates = ["sk-" + "S" * 25, "ghp_" + "X" * 30,
                      "password=synthetic-value", "Bearer " + "T" * 30,
                      "-----BEGIN " + "PRIVATE KEY-----", "AKIA" + "A" * 16,
                      'password="synthetic-value"', "api_key='synthetic-value'",
                      'password="correct horse battery staple"',
                      "https://test-user:test-value@example.invalid/file",
                      "AIza" + "G" * 35, "Cookie: session=synthetic_value",
                      "Set-Cookie: session=synthetic_value; HttpOnly"]
        for secret in candidates:
            with self.subTest(pattern=secret[:5]), mock.patch.object(os.environ, "get") as env:
                self.assert_code("sensitive_source", lambda: self.refine(source=SOURCE + secret))
                env.assert_not_called()

    def test_metadata_and_topic_secrets_rejected(self):
        self.assert_code("sensitive_source", lambda: self.refine(metadata=dict(METADATA, title="password=long-secret")))
        with self.assertRaises(akb_api.RefinementError):
            akb_api.refine(SOURCE, METADATA, ["api_key=long-secret"], SETTINGS)

    def test_missing_selected_key_is_safe(self):
        settings = dict(SETTINGS, api_key_env="AKB_MISSING_TEST_KEY")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_code("missing_or_invalid_key", lambda: self.refine(settings=settings))

    def test_bad_limits_and_metadata_rejected(self):
        for key, value in [("timeout", 0), ("timeout", float("nan")),
                           ("max_input_chars", True), ("max_output_tokens", 2),
                           ("max_output_tokens", 500.5)]:
            with self.subTest(key=key, value=value):
                self.assert_code("invalid_limits", lambda: self.refine(settings=dict(SETTINGS, **{key: value})))
        self.assert_code("invalid_metadata", lambda: self.refine(metadata=dict(METADATA, duration_seconds=float("inf"))))
        self.assert_code("invalid_source_identity", lambda: self.refine(metadata=dict(METADATA, id="../../other")))

    def test_empty_or_superficial_sections_rejected(self):
        for field in akb_api._LIST_FIELDS:
            value = draft()
            value[field] = []
            self.assert_code("invalid_draft_sections", lambda: self.refine(value))
        value = draft()
        value["core_knowledge"] = ["hello"]
        self.assert_code("insubstantial_draft", lambda: self.refine(value))

    def test_source_identity_and_schema_hallucinations_rejected(self):
        value = draft()
        value["source_id"] = "other"
        self.assert_code("source_identity_mismatch", lambda: self.refine(value))
        value = draft()
        value["source_url"] = "https://example.invalid/hallucination"
        self.assert_code("invalid_draft_schema", lambda: self.refine(value))

    def test_evidence_cannot_claim_external_verification(self):
        value = draft()
        value["evidence"] = "Verified by independent studies"
        self.assert_code("unsupported_evidence", lambda: self.refine(value))

    def test_unsupported_quote_rejected(self):
        value = draft()
        value["anchors"][0]["quote"] = "This quotation does not exist in the actual source."
        self.assert_code("unsupported_quote", lambda: self.refine(value))

    def test_timestamp_needs_exact_source_time_and_verified_duration(self):
        for timestamp in (-1, 4, 61, True):
            value = draft()
            value["anchors"][0]["timestamp_seconds"] = timestamp
            self.assert_code("unsupported_timestamp", lambda: self.refine(value))
        value = draft()
        value["anchors"][0]["timestamp_seconds"] = float("inf")
        self.assert_code("nonfinite_json", lambda: self.refine(value))
        self.assert_code("unsupported_timestamp", lambda: self.refine(metadata={k: v for k, v in METADATA.items() if k != "duration_seconds"}))
        self.assert_code("unsupported_timestamp", lambda: self.refine(metadata=dict(METADATA, platform="document")))
        value = draft()
        value["anchors"][0]["timestamp_seconds"] = None
        self.assertEqual(self.refine(value, metadata=dict(METADATA, platform="document"))["anchors"][0]["timestamp_seconds"], None)

    def test_attribution_and_fabricated_link_rejected(self):
        value = draft()
        value["summary"] = value["summary"].replace("作者主張：", "", 1)
        self.assert_code("unattributed_claim", lambda: self.refine(value))
        value = draft()
        value["risks"].append("AI 推論：可參考 https://fabricated.invalid/research 的不存在研究。")
        self.assert_code("unsupported_url", lambda: self.refine(value))

    def test_refusal_truncation_tool_calls_and_invalid_json_never_retry(self):
        replies = [response(finish="length"), response(refusal="no"),
                   response(tool_calls=[{"name": "execute"}]), response(content="```json\n{}\n```"),
                   response(content="{broken"), b"{}", b"{bad"]
        for reply in replies:
            with self.subTest(reply=reply[:30]), mock.patch.object(akb_api, "_request", return_value=(200, reply)) as request:
                with self.assertRaises(akb_api.RefinementError):
                    akb_api.refine(SOURCE, METADATA, [], SETTINGS)
                self.assertEqual(request.call_count, 1)

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        self.assert_code("duplicate_json_key", lambda: akb_api._json('{"x":1,"x":2}'))
        self.assert_code("nonfinite_json", lambda: akb_api._json('{"x":NaN}'))

    def test_transient_http_failures_retry_at_most_once(self):
        for status in (408, 500, 502, 503, 599):
            with self.subTest(status=status), mock.patch.object(akb_api, "_request", side_effect=[(status, b""), (200, response())]) as request:
                akb_api.refine(SOURCE, METADATA, [], SETTINGS)
                self.assertEqual(request.call_count, 2)
        with mock.patch.object(akb_api, "_request", return_value=(503, b"")) as request:
            self.assert_code("http_503", lambda: akb_api.refine(SOURCE, METADATA, [], SETTINGS))
            self.assertEqual(request.call_count, 2)

    def test_timeout_retries_once_but_other_transport_errors_do_not(self):
        with mock.patch.object(akb_api, "_request", side_effect=[TimeoutError(), (200, response())]) as request:
            akb_api.refine(SOURCE, METADATA, [], SETTINGS)
            self.assertEqual(request.call_count, 2)
        with mock.patch.object(akb_api, "_request", side_effect=TimeoutError()) as request:
            self.assert_code("timeout", lambda: akb_api.refine(SOURCE, METADATA, [], SETTINGS))
            self.assertEqual(request.call_count, 2)
        for error in (OSError("raw secret"), http.client.RemoteDisconnected("raw secret"), ssl.SSLCertVerificationError("raw secret")):
            with mock.patch.object(akb_api, "_request", side_effect=error) as request:
                self.assert_code("transport_error", lambda: akb_api.refine(SOURCE, METADATA, [], SETTINGS))
                self.assertEqual(request.call_count, 1)

    def test_auth_rate_limit_and_redirect_not_retried_or_followed(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 429):
            with self.subTest(status=status), server([(status, b"synthetic-private-error", {"Location": "https://external.invalid/"})]) as (endpoint, captured):
                self.assert_code("http_" + str(status), lambda: akb_api.refine(SOURCE, METADATA, [], dict(SETTINGS, endpoint=endpoint)))
                self.assertEqual(len(captured), 1)

    def test_response_size_bounded(self):
        with server([(200, b"x" * 300, {})]) as (endpoint, _captured):
            with mock.patch.object(akb_api, "_MAX_RESPONSE_BYTES", 200):
                self.assert_code("response_too_large", lambda: akb_api.refine(SOURCE, METADATA, [], dict(SETTINGS, endpoint=endpoint)))

    def test_https_uses_default_verification_context(self):
        fake = mock.MagicMock()
        fake.getresponse.return_value.status = 200
        fake.getresponse.return_value.read.return_value = response()
        context = ssl.create_default_context()
        with mock.patch.object(ssl, "create_default_context", return_value=context), mock.patch.object(http.client, "HTTPSConnection", return_value=fake) as connection:
            with mock.patch.dict(os.environ, {"AKB_UNIT_TEST_KEY": "synthetic-test-value"}):
                settings = dict(SETTINGS, endpoint="https://example.invalid/v1/chat/completions",
                                allow_remote=True, api_key_env="AKB_UNIT_TEST_KEY")
                akb_api.refine(SOURCE, METADATA, [], settings)
        self.assertIs(connection.call_args.kwargs["context"], context)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_validate_persisted_result_without_key_or_network(self):
        result = self.refine()
        with mock.patch.object(akb_api, "_request") as request, mock.patch.object(os.environ, "get") as env:
            self.assertEqual(akb_api.validate_result(result, SOURCE, METADATA), result)
        request.assert_not_called()
        env.assert_not_called()
        result["anchors"][0]["quote"] = "A locally tampered unsupported quotation."
        self.assert_code("unsupported_quote", lambda: akb_api.validate_result(result, SOURCE, METADATA))

    def test_english_attribution_is_accepted(self):
        value = draft()
        value["summary"] = value["summary"].replace("作者主張：", "Author claim:")
        self.assertEqual(self.refine(value)["summary"], value["summary"])

    def test_persisted_result_provenance_and_usage_are_checked(self):
        result = self.refine()
        result["usage"]["untrusted"] = "do not persist"
        self.assert_code("invalid_usage", lambda: akb_api.validate_result(result, SOURCE, METADATA))
        result = self.refine()
        result["claim_status"] = "independently_verified"
        self.assert_code("invalid_result_provenance", lambda: akb_api.validate_result(result, SOURCE, METADATA))

    def test_provider_cannot_echo_selected_key_into_draft(self):
        value = draft()
        value["summary"] += " synthetic-test-value"
        with mock.patch.dict(os.environ, {"AKB_UNIT_TEST_KEY": "synthetic-test-value"}):
            self.assert_code("sensitive_output", lambda: self.refine(value, settings=dict(SETTINGS, api_key_env="AKB_UNIT_TEST_KEY")))

    def test_google_and_cookie_patterns_in_each_outbound_field_are_rejected(self):
        secrets = ["AIza" + "G" * 35, "Cookie: session=synthetic_value",
                   "Set-Cookie: session=synthetic_value; HttpOnly"]
        for value in secrets:
            for field in ("source", "metadata", "topics"):
                with self.subTest(pattern=value[:10], field=field):
                    source = SOURCE + "\n" + value if field == "source" else SOURCE
                    metadata = dict(METADATA, title=value) if field == "metadata" else METADATA
                    topics = [value] if field == "topics" else []
                    with mock.patch.object(akb_api, "_request") as request:
                        self.assert_code("sensitive_source", lambda: akb_api.refine(source, metadata, topics, SETTINGS))
                    request.assert_not_called()

    def test_exact_selected_credential_in_each_outbound_field_is_rejected(self):
        for key in ("unitSynthExternalCredential-2026", 'unit"test\\value'):
            for field in ("source", "metadata", "topics"):
                with self.subTest(field=field, escaped=key.startswith('unit"')):
                    source = SOURCE + "\n" + key if field == "source" else SOURCE
                    metadata = dict(METADATA, title=key) if field == "metadata" else METADATA
                    topics = [key] if field == "topics" else []
                    settings = dict(SETTINGS, api_key_env="AKB_UNIT_TEST_KEY")
                    with mock.patch.dict(os.environ, {"AKB_UNIT_TEST_KEY": key}):
                        with mock.patch.object(akb_api, "_request") as request:
                            self.assert_code("sensitive_source", lambda: akb_api.refine(source, metadata, topics, settings))
                    request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
