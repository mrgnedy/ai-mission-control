#!/usr/bin/env python3
"""Small payload/behavior tests for the Cursor hook bridge."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parent.parent
ADAPTER = PLUGIN / "hooks/cursor_adapter.py"


class CursorAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="learning-loop-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        book = self.root / ".claude"
        book.mkdir()
        (book / "rules.md").write_text("# Rulebook\n")
        (book / "checks.tsv").write_text("ID\t*.txt\tbad\tNo bad text\n")

    def call(self, tool, path, payload=None):
        event = payload if payload is not None else {
            "tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(self.root)
        }
        return subprocess.run(
            ["python3", str(ADAPTER)], input=json.dumps(event),
            text=True, capture_output=True, check=True,
        ).stdout

    def test_violation_and_quiet_edit(self):
        path = self.root / "hello world.txt"
        path.write_text("bad\n")
        result = json.loads(self.call("Write", path))
        self.assertIn("ID", result["additional_context"])
        path.write_text("good\n")
        self.assertEqual(self.call("Write", path), "")

    def test_non_edit_and_missing_path(self):
        self.assertEqual(self.call("Read", self.root / "missing.txt"), "")
        self.assertEqual(self.call("Write", self.root / "missing.txt"), "")

    def test_custom_pointer(self):
        book = self.root / ".Codex"
        book.mkdir()
        (book / "rules.md").write_text("# Custom\n")
        (book / "checks.tsv").write_text("CUSTOM\t*.txt\tbad\tCustom hit\n")
        (self.root / ".learning-loop-rulebook-dir").write_text(".Codex\n")
        path = self.root / "custom.txt"
        path.write_text("bad\n")
        result = json.loads(self.call("Edit", path))
        self.assertIn("CUSTOM", result["additional_context"])
        (self.root / ".learning-loop-rulebook-dir").write_text("../outside\n")
        result = json.loads(self.call("Edit", path))
        self.assertIn("MISWIRED", result["additional_context"])


if __name__ == "__main__":
    unittest.main()
