"""Regression tests for adversarial-review findings."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from agentd import adapters, auth, cli, config, server, sessions


def _cfg(tmpdir, **overrides):
    cfg = dict(config.DEFAULTS)
    cfg["token_file"] = os.path.join(tmpdir, "token")
    cfg["sessions_dir"] = os.path.join(tmpdir, "sessions")
    cfg["audit_log"] = os.path.join(tmpdir, "audit.log")
    cfg["pid_file"] = os.path.join(tmpdir, "agentd.pid")
    cfg.update(overrides)
    return config._expand_paths(cfg)


class _EchoAdapter(adapters.AgentAdapter):
    name = "echo"

    def check_available(self):
        return True

    def run(self, prompt, *, cwd, timeout, env):
        return adapters.AdapterResult(output="echo:" + prompt, exit_code=0,
                                      duration_s=0.01)


class _SilentAdapter(adapters.AgentAdapter):
    """Exits 0 with zero output, like a CLI that prints nothing."""
    name = "silent"

    def check_available(self):
        return True

    def run(self, prompt, *, cwd, timeout, env):
        return adapters.AdapterResult(output="", exit_code=0, duration_s=0.01)

    def stream(self, prompt, *, cwd, timeout, env):
        return iter(())


class _HangingAdapter(adapters.AgentAdapter):
    """Yields nothing and never returns, ignoring timeout."""
    name = "hanging"

    def check_available(self):
        return True

    def run(self, prompt, *, cwd, timeout, env):
        time.sleep(60)
        return adapters.AdapterResult(output="x", exit_code=0, duration_s=60)

    def stream(self, prompt, *, cwd, timeout, env):
        time.sleep(60)
        yield "never"


class EmptyTokenFileTest(unittest.TestCase):
    def test_empty_token_file_regenerates(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td)
            Path(cfg["token_file"]).write_text("\n")
            token = auth.get_token(cfg)
            self.assertTrue(token.strip())
            self.assertTrue(auth.verify_token(token, auth.get_token(cfg)))


class RotateWhileRunningTest(unittest.TestCase):
    def _start(self, cfg):
        mgr = sessions.SessionManager(cfg, {"echo": _EchoAdapter})
        srv = server.create_server(cfg, mgr)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv

    def _get(self, port, path, token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_rotation_takes_effect_without_restart(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, port=0)
            old = auth.get_token(cfg)
            srv = self._start(cfg)
            try:
                port = srv.server_address[1]
                self.assertEqual(self._get(port, "/v1/sessions", old), 200)
                new = auth.rotate_token(cfg)
                self.assertNotEqual(old, new)
                self.assertEqual(self._get(port, "/v1/sessions", old), 401)
                self.assertEqual(self._get(port, "/v1/sessions", new), 200)
            finally:
                srv.shutdown()
                srv.server_close()


class SilentStreamTest(unittest.TestCase):
    def test_zero_output_stream_ends_with_done(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, port=0)
            token = auth.get_token(cfg)
            mgr = sessions.SessionManager(cfg, {"silent": _SilentAdapter})
            srv = server.create_server(cfg, mgr)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                port = srv.server_address[1]
                body = json.dumps({"provider": "silent"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/sessions", data=body, method="POST",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    sid = json.load(resp)["id"]
                body = json.dumps({"prompt": "hi"}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/sessions/{sid}/stream",
                    data=body, method="POST",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    text = resp.read().decode()
                self.assertIn('"done": true', text)
                # failed/empty stream must not count as a prompt
                self.assertEqual(mgr.get(sid).prompts, 0)
            finally:
                srv.shutdown()
                srv.server_close()


class StreamTimeoutTest(unittest.TestCase):
    def test_hanging_stream_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, port=0, prompt_timeout_seconds=2)
            mgr = sessions.SessionManager(cfg, {"hanging": _HangingAdapter})
            s = mgr.create("hanging")
            started = time.monotonic()
            with self.assertRaises(sessions.PromptTimeoutError):
                for _ in mgr.stream(s.id, "hi", timeout=2):
                    pass
            self.assertLess(time.monotonic() - started, 15)


class DoubleStartTest(unittest.TestCase):
    def test_second_start_fails_cleanly(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env.update({
                "AGENTD_PORT": "18766",
                "AGENTD_TOKEN_FILE": os.path.join(td, "token"),
                "AGENTD_SESSIONS_DIR": os.path.join(td, "sessions"),
                "AGENTD_AUDIT_LOG": os.path.join(td, "audit.log"),
                "AGENTD_PID_FILE": os.path.join(td, "agentd.pid"),
            })
            cmd = [sys.executable, "-m", "agentd", "start", "--foreground"]
            p1 = subprocess.Popen(cmd, env=env, cwd=repo_root,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        s = __import__("socket").create_connection(
                            ("127.0.0.1", 18766), timeout=0.5)
                        s.close()
                        break
                    except OSError:
                        time.sleep(0.1)
                # Remove the pidfile to simulate the race window: the daemon
                # is running but the pid check cannot see it. The lockfile
                # must still refuse the second start cleanly.
                os.unlink(os.path.join(td, "agentd.pid"))
                p2 = subprocess.run(cmd, env=env, cwd=repo_root,
                                    capture_output=True, timeout=15)
                self.assertNotEqual(p2.returncode, 0)
                self.assertNotIn(b"Traceback", p2.stderr)
                self.assertIn(b"already running", p2.stderr)
            finally:
                p1.terminate()
                p1.wait(timeout=10)


def argparse_ns(cfg):
    import argparse
    ns = argparse.Namespace()
    ns.config = None
    ns.foreground = True
    ns.bind = None
    ns.port = None
    ns.token_file = cfg["token_file"]
    ns.sessions_dir = cfg["sessions_dir"]
    ns.audit_log = cfg["audit_log"]
    ns.pid_file = cfg["pid_file"]
    return ns


if __name__ == "__main__":
    unittest.main()
