from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "init_orchestration.py"
TEMPLATE = ROOT / "templates" / ".ai" / "orchestration"


def _run(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(workspace), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class SetupOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-setup-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.destination = self.workspace / ".ai" / "orchestration"

    def test_missing_configuration_is_inspected_and_initialized(self) -> None:
        status = _run(self.workspace, "--status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report["status"], "missing")
        self.assertEqual(report["template_only"], sorted(_files(TEMPLATE)))

        created = _run(self.workspace)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(_files(self.destination), _files(TEMPLATE))

    def test_existing_configuration_default_is_a_noop(self) -> None:
        self.assertEqual(_run(self.workspace).returncode, 0)
        profile = self.destination / "profiles.yaml"
        profile.write_text(profile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        before = _files(self.destination)

        result = _run(self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Unchanged:", result.stdout)
        self.assertEqual(_files(self.destination), before)

    def test_status_reports_changed_missing_and_project_only_files(self) -> None:
        self.assertEqual(_run(self.workspace).returncode, 0)
        profile = self.destination / "profiles.yaml"
        profile.write_text(profile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        (self.destination / "prompts" / "cursor-task.md").unlink()
        (self.destination / "project-policy.json").write_text("{}\n", encoding="utf-8")

        result = _run(self.workspace, "--status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "diverged")
        self.assertEqual(report["changed"], ["profiles.yaml"])
        self.assertEqual(report["template_only"], ["prompts/cursor-task.md"])
        self.assertEqual(report["project_only"], ["project-policy.json"])

    def test_project_only_file_requires_a_visible_reconciliation_decision(self) -> None:
        self.assertEqual(_run(self.workspace).returncode, 0)
        (self.destination / "project-policy.json").write_text("{}\n", encoding="utf-8")
        result = _run(self.workspace, "--status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "diverged")
        self.assertEqual(report["project_only"], ["project-policy.json"])

    def test_orchestration_directory_symlink_is_rejected(self) -> None:
        actual = self.workspace / "actual-orchestration"
        actual.mkdir()
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(actual, target_is_directory=True)

        status = _run(self.workspace, "--status", "--json")
        self.assertNotEqual(status.returncode, 0)
        self.assertIn("must not be a symlink", status.stderr)

        replace = _run(self.workspace, "--replace", "--yes")
        self.assertNotEqual(replace.returncode, 0)
        self.assertIn("must not be a symlink", replace.stderr)
        self.assertTrue(self.destination.is_symlink())

    def test_replace_requires_confirmation_and_preserves_backup(self) -> None:
        self.assertEqual(_run(self.workspace).returncode, 0)
        profile = self.destination / "profiles.yaml"
        profile.write_text(profile.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        custom = self.destination / "project-policy.json"
        custom.write_text('{"project": true}\n', encoding="utf-8")
        before = _files(self.destination)

        refused = _run(self.workspace, "--replace")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("requires --yes", refused.stderr)
        self.assertEqual(_files(self.destination), before)

        replaced = _run(self.workspace, "--replace", "--yes")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        backup_line = next(line for line in replaced.stdout.splitlines() if line.startswith("Backup: "))
        backup = Path(backup_line.removeprefix("Backup: "))
        self.assertEqual(_files(self.destination), _files(TEMPLATE))
        self.assertEqual(_files(backup), before)
        self.assertFalse(custom.exists())


if __name__ == "__main__":
    unittest.main()
