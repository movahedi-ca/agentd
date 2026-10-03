"""Tests for agentd.adapters: interface, fake-CLI runs, timeouts, availability."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agentd import adapters


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
prompt = sys.argv[-1]
print(json.dumps({"type": "result", "result": "CLAUDE-ANSWER:" + prompt,
                  "session_id": "sess-123"}))
"""

FAKE_CODEX = """#!/usr/bin/env python3
import sys
print("CODEX-ANSWER:" + sys.argv[-1])
"""

FAKE_SLOW = """#!/usr/bin/env python3
import time
time.sleep(30)
print("too late")
"""


class _FakeBin:
    """Context manager placing fake agent CLIs on PATH."""

    def __init__(self, scripts):
        self.scripts = scripts
        self._old_path = None
        self.tmp = None

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, body in self.scripts.items():
            p = Path(self.tmp.name) / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.tmp.name + os.pathsep + self._old_path
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self._old_path
        self.tmp.cleanup()
        return False


def _env():
    return {"PATH": os.environ["PATH"], "HOME": "/tmp"}


class RegistryTest(unittest.TestCase):
    def test_known_providers_resolve(self):
        self.assertIsInstance(adapters.get_adapter("claude"), adapters.ClaudeAdapter)
        self.assertIsInstance(adapters.get_adapter("codex"), adapters.CodexAdapter)

    def test_unknown_provider_raises(self):
        with self.assertRaises(adapters.UnknownProviderError):
            adapters.get_adapter("gpt-9")

    def test_interface_shape(self):
        for name in ("claude", "codex"):
            adapter = adapters.get_adapter(name)
            self.assertTrue(hasattr(adapter, "check_available"))
            self.assertTrue(hasattr(adapter, "run"))
            self.assertTrue(hasattr(adapter, "stream"))
            self.assertTrue(adapter.name)


class ClaudeAdapterTest(unittest.TestCase):
    def test_run_parses_json_result(self):
        with _FakeBin({"claude": FAKE_CLAUDE}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.ClaudeAdapter()
            self.assertTrue(adapter.check_available())
            result = adapter.run("hello", cwd=td, timeout=10, env=_env())
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.output, "CLAUDE-ANSWER:hello")
            self.assertEqual(result.session_ref, "sess-123")

    def test_run_passes_prompt_as_argv_not_shell(self):
        with _FakeBin({"claude": FAKE_CLAUDE}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.ClaudeAdapter()
            evil = "hello; rm -rf / #"
            result = adapter.run(evil, cwd=td, timeout=10, env=_env())
            self.assertIn(evil, result.output)

    def test_missing_cli_reports_unavailable(self):
        with _FakeBin({}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.ClaudeAdapter()
            self.assertFalse(adapter.check_available())
            with self.assertRaises(adapters.AdapterError):
                adapter.run("hi", cwd=td, timeout=10, env=_env())

    def test_timeout_kills_process(self):
        with _FakeBin({"claude": FAKE_SLOW}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.ClaudeAdapter()
            with self.assertRaises(adapters.AdapterTimeout):
                adapter.run("hi", cwd=td, timeout=1, env=_env())


class CodexAdapterTest(unittest.TestCase):
    def test_run_captures_stdout(self):
        with _FakeBin({"codex": FAKE_CODEX}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.CodexAdapter()
            self.assertTrue(adapter.check_available())
            result = adapter.run("hello", cwd=td, timeout=10, env=_env())
            self.assertEqual(result.exit_code, 0)
            self.assertIn("CODEX-ANSWER:hello", result.output)


class StreamTest(unittest.TestCase):
    def test_stream_yields_chunks(self):
        with _FakeBin({"codex": FAKE_CODEX}), tempfile.TemporaryDirectory() as td:
            adapter = adapters.CodexAdapter()
            chunks = list(adapter.stream("hello", cwd=td, timeout=10, env=_env()))
            self.assertTrue(chunks)
            self.assertIn("CODEX-ANSWER:hello", "".join(chunks))


if __name__ == "__main__":
    unittest.main()
