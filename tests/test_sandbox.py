"""Tests for agentd.sandbox: session dir isolation, env scrubbing."""
import os
import unittest

from agentd import sandbox


class SessionDirTest(unittest.TestCase):
    def test_dir_created_under_base(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = sandbox.make_session_dir(td, "abc123")
            self.assertTrue(os.path.isdir(d))
            self.assertTrue(os.path.realpath(d).startswith(os.path.realpath(td)))

    def test_traversal_id_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for bad in ("../evil", "..", "a/b", "a\\b", "", ".", "x" * 300):
                with self.assertRaises(sandbox.SandboxError, msg=bad):
                    sandbox.make_session_dir(td, bad)

    def test_remove_session_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = sandbox.make_session_dir(td, "abc123")
            open(os.path.join(d, "note.txt"), "w").write("x")
            sandbox.remove_session_dir(td, "abc123")
            self.assertFalse(os.path.exists(d))

    def test_remove_refuses_traversal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(sandbox.SandboxError):
                sandbox.remove_session_dir(td, "../evil")


class ScrubEnvTest(unittest.TestCase):
    def test_secrets_scrubbed(self):
        env = dict(os.environ)
        env["AGENTD_TOKEN"] = "supersecret"
        env["MY_RANDOM_SECRET"] = "shh"
        env["AWS_SECRET_ACCESS_KEY"] = "shh"
        scrubbed = sandbox.scrub_env(env, {})
        self.assertNotIn("AGENTD_TOKEN", scrubbed)
        self.assertNotIn("MY_RANDOM_SECRET", scrubbed)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", scrubbed)

    def test_allowlist_kept(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "en_US.UTF-8",
               "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k2",
               "RANDOM_NOISE": "x"}
        scrubbed = sandbox.scrub_env(env, {})
        for keep in ("PATH", "HOME", "LANG", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            self.assertIn(keep, scrubbed)
        self.assertNotIn("RANDOM_NOISE", scrubbed)

    def test_extra_env_additions_applied(self):
        env = {"PATH": "/usr/bin", "SPECIAL_VAR": "s"}
        scrubbed = sandbox.scrub_env(env, {"extra_env": {"MY_TOOL": "1"},
                                           "extra_env_allow": ["SPECIAL_VAR"]})
        self.assertEqual(scrubbed["MY_TOOL"], "1")
        self.assertEqual(scrubbed["SPECIAL_VAR"], "s")


if __name__ == "__main__":
    unittest.main()
