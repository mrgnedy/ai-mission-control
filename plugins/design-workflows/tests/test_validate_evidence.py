import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/figma-intake/scripts/validate_evidence.py"
SPEC = importlib.util.spec_from_file_location("validate_evidence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture():
    return {
        "schema_version": "figma-design-evidence/v1",
        "source": {
            "url": "https://www.figma.com/design/example?node-id=12-34",
            "node_id": "12:34",
            "captured_at": "2026-10-28T12:00:00Z",
        },
        "frame": {"id": "12:34", "name": "Card", "screenshot": "frame.png"},
        "nodes": [
            {"id": "12:34", "parent": None, "children": ["12:35", "12:36"],
             "kind": "FRAME", "name": "Card", "layout": {"mode": "vertical"}, "appearance": {}},
            {"id": "12:35", "parent": "12:34", "children": [], "kind": "TEXT", "name": "Title",
             "text": "Your card", "layout": {}, "appearance": {"style_ref": "heading"}},
            {"id": "12:36", "parent": "12:34", "children": [], "kind": "IMAGE", "name": "Photo",
             "layout": {}, "appearance": {}, "asset_ids": ["asset-1"]},
        ],
        "assets": [{"id": "asset-1", "node_id": "12:36", "kind": "image", "path": "assets/photo.png"}],
        "unknowns": [{"id": "U-1", "node_id": "12:36", "question": "Is this cropped on tablet?",
                      "impact": "nonblocking"}],
    }


class ValidateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "assets").mkdir()
        (self.base / "frame.png").write_bytes(b"screenshot fixture")
        (self.base / "assets/photo.png").write_bytes(b"asset fixture")

    def test_valid_handoff_and_blocking_count(self):
        data = fixture()
        result = MODULE.validate(data, self.base)
        self.assertEqual(result, {"nodes": 3, "assets": 1, "unknowns": 1, "blocking": 0})
        data["unknowns"][0]["impact"] = "blocking"
        self.assertEqual(MODULE.validate(data, self.base)["blocking"], 1)

    def test_exact_text_and_bidirectional_tree_are_required(self):
        data = fixture()
        del data["nodes"][1]["text"]
        with self.assertRaisesRegex(MODULE.EvidenceError, "exact text"):
            MODULE.validate(data, self.base)
        data = fixture()
        data["nodes"][0]["children"].remove("12:35")
        with self.assertRaisesRegex(MODULE.EvidenceError, "absent from parent"):
            MODULE.validate(data, self.base)

    def test_asset_must_be_linked_and_files_must_be_durable(self):
        data = fixture()
        data["nodes"][2].pop("asset_ids")
        with self.assertRaisesRegex(MODULE.EvidenceError, "not linked"):
            MODULE.validate(data, self.base)
        data = fixture()
        data["assets"][0]["path"] = "assets/missing.png"
        with self.assertRaisesRegex(MODULE.EvidenceError, "does not exist"):
            MODULE.validate(data, self.base)
        data = fixture()
        data["assets"][0]["path"] = "../private.png"
        with self.assertRaisesRegex(MODULE.EvidenceError, "safe relative"):
            MODULE.validate(data, self.base)

    def test_unsaved_asset_requires_explanation(self):
        data = fixture()
        data["assets"][0].pop("path")
        with self.assertRaisesRegex(MODULE.EvidenceError, "saved path or an unavailability note"):
            MODULE.validate(data, self.base)
        data["assets"][0]["note"] = "Export unavailable from connected Figma server"
        self.assertEqual(MODULE.validate(data, self.base)["assets"], 1)

    def test_screenshot_and_source_provenance_are_required(self):
        data = fixture()
        data["frame"]["screenshot"] = "missing.png"
        with self.assertRaisesRegex(MODULE.EvidenceError, "does not exist"):
            MODULE.validate(data, self.base)
        data = fixture()
        data["source"]["url"] = "https://example.com/design/example"
        with self.assertRaisesRegex(MODULE.EvidenceError, "Figma URL"):
            MODULE.validate(data, self.base)
        data = fixture()
        data["source"]["captured_at"] = "2026-10-28"
        with self.assertRaisesRegex(MODULE.EvidenceError, "timezone"):
            MODULE.validate(data, self.base)

    def test_unreachable_nodes_are_rejected(self):
        data = fixture()
        extra = copy.deepcopy(data["nodes"][1])
        extra["id"] = "99:1"
        extra["parent"] = None
        data["nodes"].append(extra)
        with self.assertRaisesRegex(MODULE.EvidenceError, "missing parent"):
            MODULE.validate(data, self.base)


if __name__ == "__main__":
    unittest.main()
