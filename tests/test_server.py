"""Tests for agentd.server: auth, localhost-only bind, routing, limits, SSE."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

from agentd import adapters, auth, config, server, sessions


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
        if prompt == "BOOM":
            raise adapters.AdapterError("fake failure")
        if prompt == "SLOW":
            raise adapters.AdapterTimeout("fake timeout")
        return adapters.AdapterResult(output="echo:" + prompt, exit_code=0,
                                      duration_s=0.01)

    def stream(self, prompt, *, cwd, timeout, env):
        yield "chunk-one "
        yield "chunk-two"


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name, port=0)
        self.token = auth.get_token(self.cfg)
        mgr = sessions.SessionManager(self.cfg, {"echo": _EchoAdapter})
        self.srv = server.create_server(self.cfg, mgr)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token="USE"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if token == "USE":
            token = self.token
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_health_no_auth_ok(self):
        status, body = self._req("GET", "/v1/health", token=None)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("providers", payload)

    def test_missing_token_401(self):
        status, _ = self._req("GET", "/v1/sessions", token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_401(self):
        status, _ = self._req("GET", "/v1/sessions", token="wrong")
        self.assertEqual(status, 401)

    def test_session_lifecycle(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "echo"})
        self.assertEqual(status, 201)
        sid = json.loads(body)["id"]

        status, body = self._req("GET", f"/v1/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], sid)

        status, body = self._req("GET", "/v1/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["sessions"]), 1)

        status, body = self._req("POST", f"/v1/sessions/{sid}/prompt",
                                 {"prompt": "hello"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["output"], "echo:hello")
        self.assertEqual(payload["exit_code"], 0)

        status, _ = self._req("DELETE", f"/v1/sessions/{sid}")
        self.assertEqual(status, 204)
        status, _ = self._req("GET", f"/v1/sessions/{sid}")
        self.assertEqual(status, 404)

    def test_unknown_provider_400(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "nope"})
        self.assertEqual(status, 400)

    def test_missing_prompt_400(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "echo"})
        self.assertEqual(status, 201)
        sid = json.loads(body)["id"]
        status, _ = self._req("POST", f"/v1/sessions/{sid}/prompt", {})
        self.assertEqual(status, 400)

    def test_prompt_adapter_error_502(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "echo"})
        sid = json.loads(body)["id"]
        status, _ = self._req("POST", f"/v1/sessions/{sid}/prompt", {"prompt": "BOOM"})
        self.assertEqual(status, 502)

    def test_prompt_timeout_504(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "echo"})
        sid = json.loads(body)["id"]
        status, _ = self._req("POST", f"/v1/sessions/{sid}/prompt", {"prompt": "SLOW"})
        self.assertEqual(status, 504)

    def test_oversize_body_413(self):
        big = "x" * (self.cfg["max_request_bytes"] + 1)
        status, _ = self._req("POST", "/v1/sessions/abc/prompt", {"prompt": big})
        self.assertEqual(status, 413)

    def test_stream_sse(self):
        status, body = self._req("POST", "/v1/sessions", {"provider": "echo"})
        sid = json.loads(body)["id"]
        url = f"http://127.0.0.1:{self.port}/v1/sessions/{sid}/stream"
        data = json.dumps({"prompt": "hi"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
            text = resp.read().decode()
        self.assertIn("data:", text)
        self.assertIn("chunk-one", text)
        self.assertIn("chunk-two", text)
        self.assertIn('"done": true', text)

    def test_unknown_route_404(self):
        status, _ = self._req("GET", "/v1/nope")
        self.assertEqual(status, 404)


class BindTest(unittest.TestCase):
    def test_non_loopback_bind_refused(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, bind="0.0.0.0")
            mgr = sessions.SessionManager(cfg, {"echo": _EchoAdapter})
            with self.assertRaises(server.BindError):
                server.create_server(cfg, mgr)

    def test_loopback_bind_ok(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _cfg(td, port=0)
            mgr = sessions.SessionManager(cfg, {"echo": _EchoAdapter})
            srv = server.create_server(cfg, mgr)
            try:
                self.assertEqual(srv.server_address[0], "127.0.0.1")
            finally:
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
