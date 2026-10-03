"""Local HTTP API for agentd. Binds loopback only, bearer-token auth on every
route except /v1/health, JSON everywhere, SSE for streaming prompts.

Bind policy: any configured bind address that is not a loopback address
(127.0.0.0/8 or ::1) is REFUSED at startup with BindError. There is no
flag to override this; exposing the daemon to a network is a deployment
decision that does not belong in this codebase.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agentd import adapters, audit, auth, sessions

VERSION = "0.1.0"

HEALTH_PATH = "/v1/health"


class BindError(Exception):
    pass


def _is_loopback(bind):
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "agentd/" + VERSION

    # -- helpers ------------------------------------------------------
    def log_message(self, fmt, *args):  # keep stderr quiet; audit log covers it
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, code, message):
        self._send_json(status, {"error": code, "message": message})

    def _read_json_body(self):
        cfg = self.server.cfg
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > cfg["max_request_bytes"]:
            return None, "too_large"
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, None
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError):
            return None, "bad_json"

    def _check_auth(self):
        if self.path == HEALTH_PATH or self.path.startswith(HEALTH_PATH + "?"):
            return True
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        # Re-read the token file on every request so `agentd rotate-token`
        # takes effect on a running daemon without a restart. Falls back to
        # the startup token if the file vanished mid-run.
        current = auth.read_token_file(self.server.cfg) or self.server.token
        if scheme.lower() != "bearer" or not auth.verify_token(
                token.strip(), current):
            self._error(401, "unauthorized", "valid bearer token required")
            return False
        return True

    def _audit(self, status, session_id=None, started=None):
        duration_ms = None
        if started is not None:
            duration_ms = int((time.monotonic() - started) * 1000)
        self.server.audit.record(self.command, self.path, status,
                                 session_id=session_id, duration_ms=duration_ms)

    # -- routing ------------------------------------------------------
    _SESSION_RE = re.compile(r"^/v1/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,63})$")
    _PROMPT_RE = re.compile(r"^/v1/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/prompt$")
    _STREAM_RE = re.compile(r"^/v1/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/stream$")

    def _route(self):
        started = time.monotonic()
        if not self._check_auth():
            self._audit(401, started=started)
            return
        mgr = self.server.sessions
        method, path = self.command, self.path.split("?", 1)[0]

        if method == "GET" and path == HEALTH_PATH:
            payload = {"status": "ok", "version": VERSION,
                       "providers": adapters.available_providers()}
            self._send_json(200, payload)
            return self._audit(200, started=started)

        if method == "POST" and path == "/v1/sessions":
            body, err = self._read_json_body()
            if err == "too_large":
                self._error(413, "body_too_large", "request body exceeds limit")
                return self._audit(413, started=started)
            if err:
                self._error(400, "bad_json", "request body must be JSON")
                return self._audit(400, started=started)
            try:
                session = mgr.create(body.get("provider", ""))
            except adapters.UnknownProviderError as exc:
                self._error(400, "unknown_provider", str(exc))
                return self._audit(400, started=started)
            except sessions.SessionLimitError as exc:
                self._error(429, "session_limit", str(exc))
                return self._audit(429, started=started)
            self._send_json(201, session.to_dict())
            return self._audit(201, session_id=session.id, started=started)

        if method == "GET" and path == "/v1/sessions":
            self._send_json(200, {"sessions": mgr.list()})
            return self._audit(200, started=started)

        match = self._SESSION_RE.match(path)
        if match:
            sid = match.group(1)
            if method == "GET":
                try:
                    self._send_json(200, mgr.get(sid).to_dict())
                    return self._audit(200, session_id=sid, started=started)
                except sessions.SessionNotFoundError:
                    self._error(404, "no_such_session", f"no session {sid!r}")
                    return self._audit(404, session_id=sid, started=started)
            if method == "DELETE":
                try:
                    mgr.delete(sid)
                except sessions.SessionNotFoundError:
                    self._error(404, "no_such_session", f"no session {sid!r}")
                    return self._audit(404, session_id=sid, started=started)
                self.send_response(204)
                self.end_headers()
                return self._audit(204, session_id=sid, started=started)

        match = self._PROMPT_RE.match(path)
        if match and method == "POST":
            sid = match.group(1)
            body, err = self._read_json_body()
            if err == "too_large":
                self._error(413, "body_too_large", "request body exceeds limit")
                return self._audit(413, session_id=sid, started=started)
            if err:
                self._error(400, "bad_json", "request body must be JSON")
                return self._audit(400, session_id=sid, started=started)
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                self._error(400, "missing_prompt", "JSON body needs a 'prompt' string")
                return self._audit(400, session_id=sid, started=started)
            timeout = body.get("timeout")
            if timeout is not None and (
                    not isinstance(timeout, (int, float)) or timeout <= 0):
                self._error(400, "bad_timeout", "'timeout' must be positive seconds")
                return self._audit(400, session_id=sid, started=started)
            try:
                result = mgr.prompt(sid, prompt, timeout=timeout)
            except sessions.SessionNotFoundError:
                self._error(404, "no_such_session", f"no session {sid!r}")
                return self._audit(404, session_id=sid, started=started)
            except sessions.PromptTimeoutError as exc:
                self._error(504, "prompt_timeout", str(exc))
                return self._audit(504, session_id=sid, started=started)
            except adapters.AdapterError as exc:
                self._error(502, "adapter_error", str(exc))
                return self._audit(502, session_id=sid, started=started)
            self._send_json(200, {
                "output": result.output,
                "exit_code": result.exit_code,
                "duration_ms": int(result.duration_s * 1000),
            })
            return self._audit(200, session_id=sid, started=started)

        match = self._STREAM_RE.match(path)
        if match and method == "POST":
            sid = match.group(1)
            body, err = self._read_json_body()
            if err == "too_large":
                self._error(413, "body_too_large", "request body exceeds limit")
                return self._audit(413, session_id=sid, started=started)
            if err:
                self._error(400, "bad_json", "request body must be JSON")
                return self._audit(400, session_id=sid, started=started)
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                self._error(400, "missing_prompt", "JSON body needs a 'prompt' string")
                return self._audit(400, session_id=sid, started=started)
            # Peek at the first chunk before sending headers so a missing
            # session (or an immediate adapter failure) still gets a
            # proper JSON error instead of a half-open SSE stream.
            # StopIteration here means the CLI exited cleanly with zero
            # output: a legitimate empty answer, not an error.
            chunks = mgr.stream(sid, prompt)
            iterator = iter(chunks)
            empty_answer = False
            try:
                first_chunk = next(iterator)
            except StopIteration:
                empty_answer = True
                first_chunk = None
            except sessions.SessionNotFoundError:
                self._error(404, "no_such_session", f"no session {sid!r}")
                return self._audit(404, session_id=sid, started=started)
            except adapters.AdapterError as exc:
                self._error(502, "adapter_error", str(exc))
                return self._audit(502, session_id=sid, started=started)
            except sessions.PromptTimeoutError as exc:
                self._error(504, "prompt_timeout", str(exc))
                return self._audit(504, session_id=sid, started=started)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                import itertools
                if not empty_answer:
                    for chunk in itertools.chain([first_chunk], iterator):
                        line = "data: " + json.dumps({"chunk": chunk}) + "\n\n"
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                self.wfile.write(b'data: {"done": true}\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except sessions.PromptTimeoutError as exc:
                try:
                    self.wfile.write(
                        ("data: " + json.dumps({"error": str(exc)}) + "\n\n").encode())
                    self.wfile.write(b'data: {"done": true}\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            # The stream is request/response style: one prompt, one answer.
            # Close the connection after the done event.
            self.close_connection = True
            self._audit(200, session_id=sid, started=started)
            return

        self._error(404, "not_found", "unknown route")
        self._audit(404, started=started)

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def do_PUT(self):
        self._route()

    def do_PATCH(self):
        self._route()


class AgentdServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler, cfg, manager, token, audit_log):
        self.cfg = cfg
        self.sessions = manager
        self.token = token
        self.audit = audit_log
        super().__init__(server_address, handler)


def create_server(cfg, manager=None):
    """Build (but do not start) the daemon server. Refuses non-loopback bind."""
    bind = cfg["bind"]
    if not _is_loopback(bind):
        raise BindError(
            f"refusing to bind {bind!r}: agentd only serves loopback addresses")
    token = auth.get_token(cfg)
    manager = manager or sessions.SessionManager(cfg)
    audit_log = audit.AuditLog(cfg["audit_log"], enabled=cfg["audit_enabled"])
    server = AgentdServer((bind, cfg["port"]), _Handler, cfg, manager, token,
                          audit_log)
    # Confirm the socket really is loopback (defense in depth).
    bound_host = server.server_address[0]
    if not _is_loopback(bound_host):
        server.server_close()
        raise BindError(f"socket bound to non-loopback {bound_host!r}; aborting")
    return server
