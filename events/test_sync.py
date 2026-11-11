"""
Testes do que ficou no sync.py desde o ticket 4 (Issue #18): o enlace, a
saúde do contrato e o token lido de um arquivo. A partir de events/:

    python3 test_sync.py
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import sync
from event_manager import EventManager

JPEG = b"\xff\xd8\xff\xe0 foto de teste \xff\xd9"


class LinkTest(unittest.TestCase):

    def test_backend_on_this_machine_counts_as_wifi(self):
        with patch.object(sync, "BACKEND_URL", "http://127.0.0.1:8000/api"):
            self.assertEqual(sync.active_link(), "wifi")

    def test_unknown_host_is_no_link(self):
        with patch.object(sync, "BACKEND_URL", "https://nao-existe.invalid/api"):
            self.assertIsNone(sync.active_link())


class TokenTest(unittest.TestCase):

    def test_file_comes_first_and_loses_the_line_break(self):
        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as f:
            f.write("abc123\n")
        self.addCleanup(os.remove, f.name)
        with patch.multiple(sync, TOKEN_FILE=f.name, DEVICE_TOKEN="do-ambiente"):
            self.assertEqual(sync.device_token(), "abc123")

    def test_without_file_uses_the_environment(self):
        with patch.multiple(sync, TOKEN_FILE="", DEVICE_TOKEN="do-ambiente"):
            self.assertEqual(sync.device_token(), "do-ambiente")

    def test_missing_file_is_empty(self):
        with patch.multiple(sync, TOKEN_FILE="/nao/existe", DEVICE_TOKEN="do-ambiente"):
            self.assertEqual(sync.device_token(), "")


class HealthTest(unittest.TestCase):

    def test_contract_fields(self):
        with tempfile.TemporaryDirectory(prefix="vguard-teste-") as folder:
            manager = EventManager(folder)
            try:
                manager.record_event("fadiga", 0.9, JPEG, 1000.0)
                health = sync.health(manager, "wifi")
            finally:
                manager.close()
        self.assertEqual(set(health), {"ts", "link", "pending", "storage", "temperature", "cellular_bytes"})
        self.assertEqual(health["link"], "wifi")
        self.assertEqual(health["pending"], {"events": 1})
        self.assertEqual(set(health["storage"]), {"used", "free_bytes", "level"})
        self.assertIsInstance(health["cellular_bytes"], int)


if __name__ == "__main__":
    unittest.main()
