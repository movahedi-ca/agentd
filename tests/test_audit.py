"""Tests for agentd.audit: metadata-only logging, no prompt content."""
import json
import os
import tempfile
import unittest

from agentd import audit


class AuditTest(unittest.TestCase):
    def test_records_metadata_not_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "audit.log")
            log = audit.AuditLog(path)
            log.record("POST", "/v1/sessions/abc/prompt", 200,
                       session_id="abc", duration_ms=12)
            lines = open(path).read().strip().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertEqual(entry["method"], "POST")
            self.assertEqual(entry["status"], 200)
            self.assertEqual(entry["session_id"], "abc")
            self.assertIn("ts", entry)
            blob = open(path).read()
            self.assertNotIn("prompt", blob.replace("/v1/sessions/abc/prompt", ""))

    def test_disabled_log_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "audit.log")
            log = audit.AuditLog(path, enabled=False)
            log.record("GET", "/v1/health", 200)
            self.assertFalse(os.path.exists(path))

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sub", "deep", "audit.log")
            log = audit.AuditLog(path)
            log.record("GET", "/v1/health", 200)
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
