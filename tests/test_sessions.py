"""Tests for agentd.sessions: lifecycle, limits, TTL expiry, prompt accounting."""
import os
import tempfile
import time
import unittest

from agentd import adapters, config, sessions


def _cfg(tmpdir, **overrides):
    cfg = dict(config.DEFAULTS)
    cfg["token_file"] = os.path.join(tmpdir, "token")
    cfg["sessions_dir"] = os.path.join(tmpdir, "sessions")
    cfg["audit_log"] = os.path.join(tmpdir, "audit.log")
    cfg.update(overrides)
    return config._expand_paths(cfg)


class _EchoAdapter(adapters.AgentAdapter):
    name = "echo"

    def check_available(self):
        return True

    def run(self, prompt, *, cwd, timeout, env):
        if prompt == "SLEEP":
            time.sleep(30)
        return adapters.AdapterResult(output="echo:" + prompt, exit_code=0,
                                      duration_s=0.01)


class LifecycleTest(unittest.TestCase):
    def test_create_get_list_delete(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td), {"echo": _EchoAdapter})
            s = mgr.create("echo")
            self.assertTrue(os.path.isdir(s.dir))
            self.assertEqual(mgr.get(s.id).id, s.id)
            self.assertEqual(len(mgr.list()), 1)
            mgr.delete(s.id)
            self.assertEqual(mgr.list(), [])
            self.assertFalse(os.path.exists(s.dir))

    def test_unknown_session_raises(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td), {"echo": _EchoAdapter})
            with self.assertRaises(sessions.SessionNotFoundError):
                mgr.get("deadbeef")
            with self.assertRaises(sessions.SessionNotFoundError):
                mgr.delete("deadbeef")

    def test_unknown_provider_raises(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td), {"echo": _EchoAdapter})
            with self.assertRaises(adapters.UnknownProviderError):
                mgr.create("nope")

    def test_max_sessions_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td, max_sessions=2),
                                          {"echo": _EchoAdapter})
            mgr.create("echo")
            mgr.create("echo")
            with self.assertRaises(sessions.SessionLimitError):
                mgr.create("echo")

    def test_delete_frees_slot(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td, max_sessions=1),
                                          {"echo": _EchoAdapter})
            s = mgr.create("echo")
            mgr.delete(s.id)
            mgr.create("echo")  # must not raise


class PromptTest(unittest.TestCase):
    def test_prompt_runs_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td), {"echo": _EchoAdapter})
            s = mgr.create("echo")
            result = mgr.prompt(s.id, "hello", timeout=10)
            self.assertEqual(result.output, "echo:hello")
            self.assertEqual(mgr.get(s.id).prompts, 1)

    def test_prompt_timeout_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td), {"echo": _EchoAdapter})
            s = mgr.create("echo")
            with self.assertRaises(sessions.PromptTimeoutError):
                mgr.prompt(s.id, "SLEEP", timeout=1)


class TtlTest(unittest.TestCase):
    def test_expired_session_auto_removed(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = sessions.SessionManager(_cfg(td, session_ttl_seconds=1),
                                          {"echo": _EchoAdapter})
            s = mgr.create("echo")
            time.sleep(1.1)
            with self.assertRaises(sessions.SessionNotFoundError):
                mgr.get(s.id)
            self.assertFalse(os.path.exists(s.dir))


if __name__ == "__main__":
    unittest.main()
