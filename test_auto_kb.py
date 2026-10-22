"""Offline high-level CLI regression: paths, handoff and publication recovery.

Real local initialization/intake/config files; API and remote publication are
mocked at their existing boundaries. No platform, model or remote service runs.
"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import akb_api
import akb_cards
import akb_intake
import auto_kb as app
import kb_workflow as flow
import knowledge_base as kb


class EntryTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent / "state" / "entry-test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="case-", dir=fixtures))
        self.code = self.base / "program"
        self.code.mkdir()
        selected = patch.object(app, "CODE", self.code)
        selected.start()
        self.addCleanup(selected.stop)
        self.root = self.base / "data"
        app.init_workspace(self.root)

    def config(self, root=None, **updates):
        root = root or self.root
        value = app.settings_for(root)
        value.update(updates)
        (root / "akb.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def publish_enabled(self):
        return self.config(publish={"enabled": True, "repository": "example/knowledge", "branch": "main"})

    def task(self, key="youtube_abcdefghijk", stage="validated", **extra):
        return flow.save(self.root, dict(id=key, stage=stage, platform="youtube", **extra))

    def cli(self, args):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = app.main(["--workspace", str(self.root), *args])
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def document(self):
        inbox = self.root / "markdown"
        (inbox / "fixture.md").write_text(
            "# Local fixture\n\nThe source explains a clear method, conditions, and limitations.\n",
            encoding="utf-8")

    def test_workspace_normalizes_parent_aliases_before_source_isolation(self):
        aliases = (self.code, self.base,
                   self.base / ".." / self.base.name / "program",
                   self.code / ".." / "program")
        for value in aliases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.workspace_path(value)
        self.assertEqual(app.workspace_path(self.code / "workspace"), self.code / "workspace")
        with self.assertRaises(ValueError):
            app.workspace_path(self.code / "unapproved-data")

    def test_workspace_checks_symlink_or_junction_before_resolving(self):
        original = flow._plain
        observations = []
        def plain(value):
            observations.append(value)
            return original(value)
        with patch.object(flow, "_plain", side_effect=plain):
            self.assertEqual(app.workspace_path(self.root), self.root.resolve())
        self.assertEqual(observations, [self.root.absolute()])
        with patch.object(flow, "_plain", side_effect=ValueError("reparse point fixture")):
            with self.assertRaises(ValueError):
                app.workspace_path(self.root)

    def test_init_preserves_existing_config_index_and_gitignore_bytes(self):
        self.config(mode="api", api={"endpoint": "http://127.0.0.1:1234/v1/chat/completions", "model": "fixture"})
        (self.root / "knowledge/README.md").write_bytes(b"# Human notes\r\nPreserve bytes.\r\n")
        (self.root / ".gitignore").write_bytes(b"# Human ignore rules\r\n*.private\r\n")
        before = {name: (self.root / name).read_bytes() for name in ("akb.json", "knowledge/README.md", ".gitignore")}
        result = app.init_workspace(self.root)
        self.assertEqual(result["created"], [])
        self.assertEqual(result["mode"], "api")
        self.assertEqual(before, {name: (self.root / name).read_bytes() for name in before})
        self.assertEqual(app.settings_for(self.root)["mode"], "api")

    def test_default_agent_scans_and_hands_off_without_api_or_engine(self):
        self.document()
        with patch.object(akb_api, "refine", side_effect=AssertionError("no API")) as api, patch.object(
                akb_cards, "refine_task", side_effect=AssertionError("no API helper")) as cards, patch.object(
                akb_intake, "_run_engine", side_effect=AssertionError("no video engine")) as engine:
            code, result, errors = self.cli(["run"])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(result["mode"], "agent")
        self.assertEqual(len(result["pending"]), 1)
        self.assertIsNotNone(result["intake"]["scan_id"])
        self.assertIsNone(result["completion"])
        self.assertIn("Python", result["notice"])
        api.assert_not_called(); cards.assert_not_called(); engine.assert_not_called()

    def test_api_pending_error_returns_exit_one_and_redacts_raw_exception(self):
        self.document()
        self.config(mode="api")
        with patch.object(akb_cards, "refine_task", side_effect=ValueError("SECRET_PROVIDER_RESPONSE")) as refine:
            code, result, errors = self.cli(["run"])
        self.assertEqual(code, 1)
        self.assertEqual(result["pending"][0]["error"], "ValueError")
        self.assertNotIn("SECRET_PROVIDER_RESPONSE", json.dumps(result) + errors)
        self.assertIsNone(result["completion"])
        refine.assert_called_once()

    def test_cross_workspace_settings_and_temporary_mode_override_are_isolated(self):
        other = self.base / "other-data"
        app.init_workspace(other)
        before = self.config(mode="api", max_batch=1, engine={"model_path": "fixture-a"})
        self.config(root=other, max_batch=7, engine={"model_path": "fixture-b"})
        empty = {"scan_id": None, "tasks": [], "pending": [], "failed": [], "skipped": []}
        with patch.object(akb_intake, "intake", return_value=empty) as receive:
            first = app.run(self.root, [], mode="agent")
            second = app.run(other, [])
        one, two = receive.call_args_list
        self.assertEqual(one.args[2]["engine"]["model_path"], "fixture-a")
        self.assertEqual(two.args[2]["engine"]["model_path"], "fixture-b")
        self.assertEqual((first["mode"], second["mode"]), ("agent", "agent"))
        self.assertEqual(app.settings_for(self.root), before)
        with patch.dict(os.environ, {"AKB_WORKSPACE": str(other)}):
            self.assertEqual(app.workspace_path(), other.resolve())
            self.assertEqual(app.workspace_path(self.root), self.root.resolve())

    def test_finish_archives_only_ids_in_this_scan_manifest(self):
        scan_id = "a" * 32
        scanned = "doc_" + "1" * 64
        external = "doc_" + "2" * 64
        video = "youtube_abcdefghijk"
        manifest = flow._scan_path(self.root, scan_id)
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"schema": 1, "scan_id": scan_id, "entries": [{"id": scanned}]}), encoding="utf-8")
        for key in (scanned, external, video):
            self.task(key)
        with patch.object(flow, "validate_task", side_effect=lambda root, key: flow.read(root, key)), patch.object(
                flow, "archive_markdown", return_value={"archived": [scanned]}) as archive:
            result = app.finish(self.root, [video, scanned, external, scanned], scan_id)
        archive.assert_called_once_with(self.root, [scanned], scan_id=scan_id)
        self.assertEqual(set(result["validated"]), {scanned, external, video})
        self.assertEqual(result["publication"], "disabled")

    def test_published_task_is_validated_without_republishing(self):
        self.publish_enabled()
        task = self.task(stage="published")
        with patch.object(flow, "validate_task", return_value=task), patch.object(
                kb, "publish", side_effect=AssertionError("published means no second push")) as publish:
            result = app.finish(self.root, [task["id"]])
        self.assertEqual(result["publication"]["status"], "already_published")
        self.assertTrue(app.completed_without_errors(result))
        publish.assert_not_called()

    def test_receipts_resume_verify_or_retry_and_fresh_ids_publish_once(self):
        self.publish_enabled()
        stages = ("main_pushed", "branch_pushed", "validated")
        tasks = [self.task("youtube_abcdefghij" + str(i), stage,
                           publication_receipt="state/publish/" + str(i) + "/receipt.json")
                 for i, stage in enumerate(stages)]
        fresh = self.task("youtube_zzzzzzzzzzz")
        with patch.object(flow, "validate_task", side_effect=lambda root, key: flow.read(root, key)), patch.object(
                kb, "publish", return_value={"status": "published"}) as publish:
            result = app.finish(self.root, [task["id"] for task in tasks] + [fresh["id"]])
        self.assertEqual(publish.call_count, 4)
        for call, stage, task in zip(publish.call_args_list, stages, tasks):
            self.assertEqual(call.kwargs["receipt_path"], Path(task["publication_receipt"]))
            self.assertEqual(call.kwargs["verify"], stage in {"main_pushed", "branch_pushed"})
            self.assertEqual(call.kwargs["retry"], stage == "validated")
            self.assertNotIn("ids", call.kwargs)
        self.assertEqual(publish.call_args_list[-1].kwargs, {"ids": [fresh["id"]]})
        self.assertEqual(result["publication"]["status"], "published")

    def test_run_hands_main_pushed_and_branch_pushed_to_receipt_recovery(self):
        tasks = [self.task("youtube_abcdefghij" + str(i), stage) for i, stage in enumerate(("main_pushed", "branch_pushed"))]
        received = {"scan_id": None, "tasks": [task["id"] for task in tasks], "pending": [], "failed": [], "skipped": []}
        with patch.object(akb_intake, "intake", return_value=received), patch.object(app, "finish", return_value={"validated": []}) as finish:
            result = app.run(self.root, [])
        self.assertEqual(result["pending"], [])
        finish.assert_called_once_with(self.root, [task["id"] for task in tasks], None)

    def test_failed_intake_archive_validation_and_publication_are_nonzero(self):
        values = (
            {"intake": {"failed": [{"error_type": "IntakeError"}]}},
            {"completion": {"failed": [{"id": "fixture"}]}},
            {"completion": {"archive": {"error": "ValueError", "status": "originals_preserved"}}},
            {"completion": {"publication": {"status": "not_published"}}},
        )
        for value in values:
            with self.subTest(value=value), patch.object(app, "run", return_value=value):
                code, _result, _errors = self.cli(["run"])
                self.assertEqual(code, 1)

    def test_publication_exception_is_safe_failure_and_exit_one(self):
        self.publish_enabled()
        task = self.task()
        with patch.object(flow, "validate_task", return_value=task), patch.object(
                kb, "publish", side_effect=RuntimeError("SECRET_GITHUB_RESPONSE")):
            code, result, errors = self.cli(["finish", "--id", task["id"]])
        self.assertEqual(code, 1)
        self.assertEqual(result["publication"]["status"], "not_published")
        self.assertNotIn("SECRET_GITHUB_RESPONSE", json.dumps(result) + errors)

    def test_missing_receipt_for_pushed_task_is_not_reported_published(self):
        self.publish_enabled()
        for stage in ("main_pushed", "branch_pushed"):
            task = self.task(stage=stage)
            with self.subTest(stage=stage), patch.object(flow, "validate_task", return_value=task), patch.object(
                    kb, "publish", side_effect=AssertionError("missing receipt cannot authorize a push")) as publish:
                result = app.finish(self.root, [task["id"]])
                self.assertFalse(app.completed_without_errors(result))
                self.assertNotIn(result["publication"]["status"], {"published", "already_published"})
                publish.assert_not_called()

    def test_doctor_accepts_local_api_without_key_environment_name(self):
        self.config(api={"endpoint": "http://127.0.0.1:1234/v1/chat/completions",
                         "model": "fixture", "api_key_env": None})
        with patch.dict(os.environ, {"AKB_API_KEY": "SYNTHETIC_FIXTURE"}):
            code, result, errors = self.cli(["doctor"])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertTrue(result["api_configured"])
        self.assertFalse(result["api_key_set"])
        self.assertNotIn("SYNTHETIC_FIXTURE", json.dumps(result))

    def test_api_mode_pushed_tasks_resume_receipts_without_refinement(self):
        self.config(mode="api")
        tasks = [self.task("youtube_abcdefghij" + str(i), stage,
                           publication_receipt="state/publish/" + str(i) + "/receipt.json")
                 for i, stage in enumerate(("main_pushed", "branch_pushed"))]
        received = {"scan_id": None, "tasks": [task["id"] for task in tasks],
                    "pending": [], "failed": [], "skipped": []}
        with patch.object(akb_intake, "intake", return_value=received), patch.object(
                akb_cards, "refine_task", side_effect=AssertionError("publication recovery is not refinement")) as refine, patch.object(
                app, "finish", return_value={"validated": [task["id"] for task in tasks]}) as finish:
            result = app.run(self.root, [])
        self.assertEqual(result["pending"], [])
        refine.assert_not_called()
        finish.assert_called_once_with(self.root, [task["id"] for task in tasks], None)


if __name__ == "__main__":
    unittest.main()
