"""Tests for agentd.auth: token lifecycle, file perms, env override, verification."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agentd import auth, config


def _cfg(tmpdir):
    cfg = dict(config.DEFAULTS)
    cfg["token_file"] = os.path.join(tmpdir, "token")
    return config._expand_paths(cfg)


class TokenFileTest(unittest.TestCase):
    def test_first_run_generates_token_with_0600(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            token = auth.get_token(cfg)
            self.assertTrue(token)
            st = os.stat(cfg["token_file"])
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)

    def test_token_persists_across_calls(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            self.assertEqual(auth.get_token(cfg), auth.get_token(cfg))

    def test_rotate_replaces_token(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            first = auth.get_token(cfg)
            second = auth.rotate_token(cfg)
            self.assertNotEqual(first, second)
            self.assertEqual(auth.get_token(cfg), second)

    def test_existing_file_with_wrong_perms_gets_fixed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            Path(cfg["token_file"]).write_text("sometoken")
            os.chmod(cfg["token_file"], 0o644)
            auth.get_token(cfg)
            st = os.stat(cfg["token_file"])
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)


class EnvOverrideTest(unittest.TestCase):
    def test_env_token_wins_over_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            file_token = auth.get_token(cfg)
            os.environ["AGENTD_TOKEN"] = "env-token-value"
            try:
                self.assertEqual(auth.get_token(cfg), "env-token-value")
                self.assertNotEqual(auth.get_token(cfg), file_token)
            finally:
                del os.environ["AGENTD_TOKEN"]


class VerifyTest(unittest.TestCase):
    def test_correct_token_verifies(self):
        self.assertTrue(auth.verify_token("abc123", "abc123"))

    def test_wrong_token_rejected(self):
        self.assertFalse(auth.verify_token("abc123", "abc124"))

    def test_empty_tokens_rejected(self):
        self.assertFalse(auth.verify_token("", "abc123"))
        self.assertFalse(auth.verify_token("abc123", ""))
        self.assertFalse(auth.verify_token("", ""))

    def test_none_rejected(self):
        self.assertFalse(auth.verify_token(None, "abc123"))

    def test_generated_tokens_are_unique(self):
        self.assertNotEqual(auth.generate_token(), auth.generate_token())


if __name__ == "__main__":
    unittest.main()
