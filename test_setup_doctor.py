"""Read-only onboarding checks: missing, reused and alternate environments."""
from contextlib import redirect_stdout
from io import StringIO
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import auto_kb as app

SOURCE = Path(__file__).resolve().parent
PACKAGES = ("faster_whisper", "av", "ctranslate2", "yt_dlp", "yt_dlp_ejs")


class SetupDoctorTests(unittest.TestCase):
    def setUp(self):
        fixtures = SOURCE / "state" / "setup-test-runs"
        fixtures.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(dir=fixtures))
        self.code = self.base / "code"
        self.code.mkdir()
        self.root = self.base / "knowledge-data"
        self.enterContext(patch.object(app, "CODE", self.code))
        self.enterContext(patch.dict(os.environ, {"PATH": ""}, clear=True))
        self.enterContext(patch.object(importlib.util, "find_spec", return_value=None))

    def configure(self, engine=None, **extra):
        app.init_workspace(self.root)
        cfg = app.settings_for(self.root)
        cfg.update(extra)
        cfg["engine"] = engine or {}
        (self.root / "akb.json").write_text(json.dumps(cfg), encoding="utf-8")

    def complete_files(self):
        model = self.root / "models" / "fixture"
        model.mkdir(parents=True)
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (model / name).write_bytes(b"synthetic; not a real model")
        vendor = self.root / "vendor"
        for name in PACKAGES:
            package = vendor / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("raise AssertionError('doctor must not import packages')\n")
        module = self.code / "node_modules/playwright"
        module.mkdir(parents=True)
        (module / "package.json").write_text('{"name":"playwright"}')
        env = {}
        for variable in ("AKB_NODE", "AKB_FFMPEG", "AKB_BROWSER_EXECUTABLE"):
            binary = self.root / (variable + ".exe")
            binary.write_bytes(b"synthetic; not an executable")
            env[variable] = str(binary)
        self.configure({"model_path": "models/fixture", "vendor_dir": "vendor", "env": env})
        return model, vendor

    def test_uninitialized_workspace_is_read_only_and_reports_missing_items(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess or download")):
            value = app.doctor(self.root)
        self.assertFalse(self.root.exists())
        self.assertFalse(value["workspace_initialized"])
        self.assertEqual(value["capabilities"]["markdown"], "available")
        self.assertEqual(value["capabilities"]["video"], "needs_setup")
        self.assertIn("model_files", value["video_missing"])
        self.assertIn("yt_dlp", value["video_missing"])
        self.assertEqual(value["capabilities"]["search"], "missing_ripgrep")

    def test_cli_doctor_works_before_init_without_creating_a_workspace(self):
        output = StringIO()
        with redirect_stdout(output):
            code = app.main(["--workspace", str(self.root), "doctor"])
        self.assertEqual(code, 0)  # Report generated, not an installation PASS.
        self.assertFalse(json.loads(output.getvalue())["workspace_initialized"])
        self.assertFalse(self.root.exists())

    def test_reused_vendor_and_relative_paths_match_engine_without_import_or_execution(self):
        self.complete_files()
        before = {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            value = app.doctor(self.root)
        self.assertTrue(all(value["python_packages"].values()))
        self.assertTrue(all(value["runtime_checks"].values()))
        self.assertEqual(value["video_missing"], [])
        self.assertEqual(value["capabilities"]["video"], "detected_not_verified")
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()})

    def test_alternate_python_is_unknown_and_never_claims_current_packages_are_installed_there(self):
        self.configure({"python": str(self.root / "other-python.exe")})
        with patch.object(importlib.util, "find_spec", side_effect=AssertionError("wrong interpreter")):
            value = app.doctor(self.root)
        self.assertFalse(value["runtime_checks"]["engine_python_matches"])
        self.assertTrue(all(found is None for found in value["python_packages"].values()))
        self.assertIn("engine_python_matches", value["video_missing"])

    def test_missing_or_empty_tokenizer_prevents_detected_video_state(self):
        model, _vendor = self.complete_files()
        (model / "tokenizer.json").write_bytes(b"")
        value = app.doctor(self.root)
        self.assertEqual(value["model_missing_files"], ["tokenizer.json"])
        self.assertEqual(value["capabilities"]["video"], "needs_setup")

    def test_explicit_missing_browser_and_ffmpeg_do_not_fall_back_to_path(self):
        self.configure({"env": {"AKB_BROWSER_EXECUTABLE": str(self.root / "absent-browser.exe"),
                                "AKB_FFMPEG": str(self.root / "absent-ffmpeg.exe")}})
        with patch.object(shutil, "which", return_value=sys.executable):
            value = app.doctor(self.root)
        self.assertTrue(value["commands"]["ffmpeg"])
        self.assertFalse(value["runtime_checks"]["ffmpeg"])
        self.assertFalse(value["runtime_checks"]["browser_executable"])

    def test_ffmpeg_directory_override_is_supported(self):
        self.complete_files()
        directory = self.root / "ffmpeg-bin"
        directory.mkdir()
        (directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).write_bytes(b"fixture")
        cfg = app.settings_for(self.root)
        cfg["engine"]["env"]["AKB_FFMPEG"] = str(directory)
        (self.root / "akb.json").write_text(json.dumps(cfg))
        self.assertTrue(app.doctor(self.root)["runtime_checks"]["ffmpeg"])

    def test_browser_channel_only_checks_normal_installations_for_selected_channel(self):
        program_files = self.base / "program-files"
        edge = program_files / "Microsoft/Edge/Application/msedge.exe"
        edge.parent.mkdir(parents=True)
        edge.write_bytes(b"fixture")
        self.configure({"env": {"AKB_BROWSER_CHANNEL": "chrome"}})
        with patch.dict(os.environ, {"ProgramFiles": str(program_files)}):
            self.assertFalse(app.doctor(self.root)["runtime_checks"]["browser_executable"])
            self.configure({"env": {"AKB_BROWSER_CHANNEL": "msedge"}})
            self.assertTrue(app.doctor(self.root)["runtime_checks"]["browser_executable"])

    def test_model_config_takes_precedence_over_environment_without_dumping_env_or_keys(self):
        self.complete_files()
        self.configure(app.settings_for(self.root)["engine"],
                       api={"endpoint": "", "model": "", "api_key_env": "AKB_TEST_KEY"})
        with patch.dict(os.environ, {"AKB_MODEL_PATH": str(self.root / "absent-model"),
                                    "AKB_TEST_KEY": "SYNTHETIC_SECRET_VALUE", "UNRELATED_PRIVATE": "DO_NOT_REPORT"}):
            value = app.doctor(self.root)
        self.assertTrue(value["runtime_checks"]["model_files"])
        self.assertTrue(value["api_key_set"])
        self.assertNotIn("SYNTHETIC_SECRET_VALUE", json.dumps(value))
        self.assertNotIn("DO_NOT_REPORT", json.dumps(value))

    def test_corrupt_existing_config_is_not_silently_replaced_with_defaults(self):
        self.configure()
        path = self.root / "akb.json"
        path.write_bytes(b"not valid JSON")
        with self.assertRaises(ValueError):
            app.doctor(self.root)
        self.assertEqual(path.read_bytes(), b"not valid JSON")

    def test_environment_model_path_is_relative_to_workspace(self):
        self.complete_files()
        cfg = app.settings_for(self.root)
        del cfg["engine"]["model_path"]
        cfg["engine"]["env"]["AKB_MODEL_PATH"] = "models/fixture"
        (self.root / "akb.json").write_text(json.dumps(cfg))
        value = app.doctor(self.root)
        self.assertTrue(value["model_path_configured"])
        self.assertEqual(value["model_missing_files"], [])


if __name__ == "__main__":
    unittest.main()
