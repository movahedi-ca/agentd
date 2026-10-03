"""Tests for agentd.config: defaults, TOML loading, env overrides, path layout."""
import os
import tempfile
import unittest
from pathlib import Path

from agentd import config


class DefaultsTest(unittest.TestCase):
    def test_loopback_bind_by_default(self):
        cfg = config.load_config()
        self.assertEqual(cfg["bind"], "127.0.0.1")

    def test_sane_defaults_present(self):
        cfg = config.load_config()
        for key in ("port", "token_file", "sessions_dir", "audit_log",
                    "pid_file", "max_sessions", "session_ttl_seconds",
                    "prompt_timeout_seconds", "max_request_bytes"):
            self.assertIn(key, cfg, key)

    def test_paths_expand_user(self):
        cfg = config.load_config()
        for key in ("token_file", "sessions_dir", "audit_log", "pid_file"):
            self.assertNotIn("~", cfg[key], key)
            self.assertTrue(os.path.isabs(cfg[key]), key)


class TomlLoadingTest(unittest.TestCase):
    def test_toml_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text('port = 9999\nmax_sessions = 3\n')
            cfg = config.load_config(str(p))
            self.assertEqual(cfg["port"], 9999)
            self.assertEqual(cfg["max_sessions"], 3)
            self.assertEqual(cfg["bind"], "127.0.0.1")  # untouched

    def test_missing_toml_is_fine(self):
        cfg = config.load_config("/nonexistent/path/config.toml")
        self.assertEqual(cfg["port"], config.DEFAULTS["port"])

    def test_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text('definitely_not_a_setting = 1\n')
            with self.assertRaises(config.ConfigError):
                config.load_config(str(p))


class EnvOverrideTest(unittest.TestCase):
    def test_env_overrides_toml(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text('port = 9999\n')
            os.environ["AGENTD_PORT"] = "1234"
            try:
                cfg = config.load_config(str(p))
            finally:
                del os.environ["AGENTD_PORT"]
            self.assertEqual(cfg["port"], 1234)

    def test_bad_env_int_rejected(self):
        os.environ["AGENTD_PORT"] = "notaport"
        try:
            with self.assertRaises(config.ConfigError):
                config.load_config()
        finally:
            del os.environ["AGENTD_PORT"]


class DirsTest(unittest.TestCase):
    def test_ensure_dirs_creates_layout(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = dict(config.DEFAULTS)
            cfg["token_file"] = os.path.join(td, "tok")
            cfg["sessions_dir"] = os.path.join(td, "sess")
            cfg["audit_log"] = os.path.join(td, "sub", "audit.log")
            config.ensure_dirs(cfg)
            self.assertTrue(os.path.isdir(os.path.join(td, "sess")))
            self.assertTrue(os.path.isdir(os.path.join(td, "sub")))


if __name__ == "__main__":
    unittest.main()
