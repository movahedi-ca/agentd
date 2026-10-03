"""Tests for agentd.cli: start/stop/status/rotate-token, pidfile handling."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from agentd import cli, config


def _cfg(tmpdir, **overrides):
    cfg = dict(config.DEFAULTS)
    cfg["token_file"] = os.path.join(tmpdir, "token")
    cfg["sessions_dir"] = os.path.join(tmpdir, "sessions")
    cfg["audit_log"] = os.path.join(tmpdir, "audit.log")
    cfg["pid_file"] = os.path.join(tmpdir, "agentd.pid")
    cfg.update(overrides)
    return config._expand_paths(cfg)


class PidfileTest(unittest.TestCase):
    def test_start_foreground_then_stop(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, port=0)
            env = dict(os.environ, PYTHONPATH=os.path.expanduser("~/workspace/agentd"))
            proc = subprocess.Popen(
                [sys.executable, "-m", "agentd", "start", "--foreground",
                 "--port", "0", "--token-file", cfg["token_file"],
                 "--sessions-dir", cfg["sessions_dir"],
                 "--audit-log", cfg["audit_log"],
                 "--pid-file", cfg["pid_file"]],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 10
                while time.time() < deadline:
                    if os.path.exists(cfg["pid_file"]):
                        break
                    time.sleep(0.1)
                self.assertTrue(os.path.exists(cfg["pid_file"]),
                                "pidfile was not written")
                pid = int(open(cfg["pid_file"]).read().strip())
                self.assertEqual(pid, proc.pid)
                rc = cli.main(["stop", "--pid-file", cfg["pid_file"]])
                self.assertEqual(rc, 0)
                proc.wait(timeout=10)
                self.assertFalse(os.path.exists(cfg["pid_file"]))
            finally:
                if proc.poll() is None:
                    proc.kill()

    def test_stop_without_pidfile(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            rc = cli.main(["stop", "--pid-file", cfg["pid_file"]])
            self.assertNotEqual(rc, 0)

    def test_status_reports_not_running(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            rc = cli.main(["status", "--pid-file", cfg["pid_file"]])
            self.assertNotEqual(rc, 0)


class RotateTokenCliTest(unittest.TestCase):
    def test_rotate_prints_new_token(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            from agentd import auth
            first = auth.get_token(cfg)
            # capture stdout via main() return; rotate-token prints the token
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["rotate-token", "--token-file", cfg["token_file"]])
            self.assertEqual(rc, 0)
            printed = buf.getvalue().strip()
            self.assertTrue(printed)
            self.assertNotEqual(printed, first)
            self.assertEqual(auth.get_token(cfg), printed)


if __name__ == "__main__":
    unittest.main()
