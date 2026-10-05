#!/usr/bin/env python3
"""Check Cursor catalog paths and explicit component selection."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "ai-tools/plugins/learning-loop"


class CursorManifestTest(unittest.TestCase):
    def test_catalog_points_to_single_plugin(self):
        catalog = json.loads((ROOT / ".cursor-plugin/marketplace.json").read_text())
        self.assertEqual(len(catalog["plugins"]), 1)
        entry = catalog["plugins"][0]
        self.assertEqual(entry["name"], "learning-loop")
        self.assertEqual(ROOT / entry["source"], PLUGIN)

    def test_explicit_components_exist(self):
        manifest = json.loads((PLUGIN / ".cursor-plugin/plugin.json").read_text())
        self.assertEqual(manifest["name"], "learning-loop")
        self.assertEqual(len(manifest["skills"]), 2)
        for skill in manifest["skills"]:
            self.assertTrue((PLUGIN / skill / "SKILL.md").is_file())
        self.assertEqual(manifest["hooks"], "./hooks/cursor.json")
        config = json.loads((PLUGIN / manifest["hooks"]).read_text())
        self.assertIn("postToolUse", config["hooks"])


if __name__ == "__main__":
    unittest.main()
