"""Audit log: append-only JSON lines of API-call metadata.

Records method, path, status, session id, and duration. Prompt bodies and
responses are never written here. Disable with audit_enabled=false in config.
"""
from __future__ import annotations

import json
import os
import threading
import time


class AuditLog:
    def __init__(self, path, enabled=True):
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()

    def record(self, method, path, status, *, session_id=None, duration_ms=None):
        if not self.enabled:
            return
        entry = {
            "ts": time.time(),
            "method": method,
            "path": path,
            "status": status,
        }
        if session_id is not None:
            entry["session_id"] = session_id
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        line = json.dumps(entry) + "\n"
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._lock:
            with open(self.path, "a") as fh:
                fh.write(line)
