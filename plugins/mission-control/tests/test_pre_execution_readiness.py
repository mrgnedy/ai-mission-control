from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.cli import main as cli_main
from orchestration.configuration import load_configuration
from orchestration.doctor import inspect_packaged_assets
from orchestration.failures import ValidationError
from orchestration.io import atomic_write_json
from orchestration.router import route_graph
from orchestration.validator import validate_required_artifacts


def _graph() -> dict:
    return {
        "schema_version": "1.0",
        "workflow": {"id": "readiness-fixture", "objective": "Exercise new-run readiness."},
        "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
        "tasks": [{
            "id": "TASK-1", "title": "Synthetic task", "type": "implementation",
            "objective": "Change a synthetic file.",
            "scope": {"allowed": ["file.txt"], "forbidden": []},
            "acceptance_criteria": ["file.txt has the expected value."],
            "depends_on": [], "capabilities": ["implementation"],
            "complexity": "low", "risk": "low", "verification": [],
            "requested_profile": "claude-implementation",
        }],
    }


class PreExecutionReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-readiness-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.change = self.workspace / "specs" / "readiness-fixture"
        self.orchestration = self.change / "orchestration"
        self.orchestration.mkdir(parents=True)
        self.graph = _graph()
        bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        delegation = route_graph(
            self.graph, bundle["profiles"], bundle["routing_matrix"],
            bundle["execution_strategy"]["allowed_worker_providers"],
        )
        atomic_write_json(self.orchestration / "task-graph.yaml", self.graph)
        atomic_write_json(self.orchestration / "delegation.yaml", delegation)
        for name in ("spec.md", "plan.md", "tasks.md"):
            (self.change / name).write_text(f"# Synthetic {name}\n", encoding="utf-8")

    def _cli(self, command: str, *extra: str, config: Path | None = None) -> dict:
        arguments = [command, "--change", str(self.change)]
        if command == "run":
            arguments.extend(["--workspace", str(self.workspace), "--dry-run"])
        if config is not None:
            arguments.extend(["--config", str(config)])
        arguments.extend(extra)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(arguments), 0)
        return json.loads(output.getvalue())

    def _legacy_config(self) -> Path:
        config_dir = self.workspace / "legacy" / ".ai" / "orchestration"
        shutil.copytree(ROOT / "templates" / ".ai" / "orchestration", config_dir)
        methodology = config_dir / "methodologies" / "disciplined.yaml"
        value = json.loads(methodology.read_text(encoding="utf-8"))
        value.pop("enforce_pre_execution_gates")
        methodology.write_text(json.dumps(value), encoding="utf-8")
        return config_dir / "orchestration.yaml"

    def test_opted_in_gate_requires_artifacts_but_no_empty_decision_file(self) -> None:
        self.assertEqual(self._cli("validate")["status"], "valid")
        self.assertEqual(self._cli("run")["status"], "dry_run")
        (self.change / "spec.md").unlink()
        for command in ("validate", "run"):
            with self.subTest(command=command), self.assertRaisesRegex(ValidationError, "spec.md"):
                self._cli(command)
        self.assertFalse((self.workspace / ".ai-runtime").exists())

    def test_unreferenced_open_or_scheduled_decision_blocks_only_opted_in_run(self) -> None:
        legacy = self._legacy_config()
        for status in ("open", "scheduled"):
            with self.subTest(status=status):
                atomic_write_json(self.orchestration / "decisions.yaml", {
                    "schema_version": "1.0",
                    "decisions": [{"id": "CLR-1", "status": status, "summary": "Choose an API.", "authority": "user"}],
                })
                with self.assertRaisesRegex(ValidationError, "CLR-1"):
                    self._cli("validate")
                with self.assertRaisesRegex(ValidationError, "CLR-1"):
                    self._cli("run")
                self.assertEqual(self._cli("validate", config=legacy)["status"], "valid")
        atomic_write_json(self.orchestration / "decisions.yaml", {
            "schema_version": "1.0",
            "decisions": [{
                "id": "CLR-1", "status": "resolved", "summary": "Choose an API.",
                "authority": "user", "resolution": "Use the approved API.",
            }],
        })
        self.assertEqual(self._cli("validate")["status"], "valid")
        self.assertFalse((self.workspace / ".ai-runtime").exists())

    def test_skip_review_conflict_is_opted_in_only(self) -> None:
        with self.assertRaisesRegex(ValidationError, "skip-review"):
            self._cli("run", "--skip-review")
        legacy = self._legacy_config()
        self.assertEqual(self._cli("run", "--skip-review", config=legacy)["status"], "dry_run")

    def test_missing_selected_asset_refuses_new_run_not_legacy(self) -> None:
        with patch("orchestration.cli.inspect_packaged_assets", return_value={
            "status": "missing", "missing": ["skills/code-discipline/SKILL.md"],
        }):
            with self.assertRaisesRegex(ValidationError, "code-discipline"):
                self._cli("validate")
            with self.assertRaisesRegex(ValidationError, "code-discipline"):
                self._cli("run")
            self.assertEqual(self._cli("run", config=self._legacy_config())["status"], "dry_run")
        self.assertFalse((self.workspace / ".ai-runtime").exists())

    def test_required_artifact_rejects_escape_directory_and_symlink(self) -> None:
        (self.change / "nested").mkdir()
        (self.change / "nested" / "real.md").write_text("ready", encoding="utf-8")
        validate_required_artifacts(self.change, ["nested/real.md"])
        for bad in ("../outside.md", "/tmp/outside.md", "nested", "."):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_required_artifacts(self.change, [bad])
        (self.change / "nested" / "link.md").symlink_to("real.md")
        with self.assertRaisesRegex(ValidationError, "symlink"):
            validate_required_artifacts(self.change, ["nested/link.md"])

    def test_asset_check_is_selected_provider_specific(self) -> None:
        assets = self.workspace / "package"
        (assets / "skills" / "code-discipline").mkdir(parents=True)
        (assets / "skills" / "code-discipline" / "SKILL.md").write_text("rules", encoding="utf-8")
        prompt_dir = assets / ".ai" / "orchestration" / "prompts"
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "claude-task.md").write_text("prompt", encoding="utf-8")
        self.assertEqual(inspect_packaged_assets(assets, ["claude-cli"], review=False)["status"], "complete")
        cursor = inspect_packaged_assets(assets, ["cursor-cli"], review=False)
        self.assertEqual(cursor["missing"], [".ai/orchestration/prompts/cursor-task.md"])
        reviewer = inspect_packaged_assets(assets, ["claude-cli"], review=True)
        self.assertEqual(reviewer["missing"], [".ai/orchestration/prompts/execution-review.md"])

    def test_legacy_positional_resume_bypasses_new_gate(self) -> None:
        (self.change / "spec.md").unlink()
        graph = self.orchestration / "task-graph.yaml"
        delegation = self.orchestration / "delegation.yaml"
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main([
                "resume", str(graph), str(delegation), "--workspace", str(self.workspace),
                "--runtime", str(self.workspace / "legacy-runtime"), "--dry-run",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "dry_run")

    def test_doctor_reports_packaged_assets(self) -> None:
        output = StringIO()
        with patch("orchestration.cli.diagnose_configured", return_value={}), redirect_stdout(output):
            self.assertEqual(cli_main(["doctor"]), 0)
        self.assertEqual(json.loads(output.getvalue())["packaged_assets"]["status"], "complete")

    def test_opted_in_gate_precedes_real_local_execution(self) -> None:
        config = self._legacy_config()
        methodology = config.parent / "methodologies" / "disciplined.yaml"
        value = json.loads(methodology.read_text(encoding="utf-8"))
        value["enforce_pre_execution_gates"] = True
        value["require_independent_review"] = False
        methodology.write_text(json.dumps(value), encoding="utf-8")
        graph = _graph()
        graph["tasks"][0].update({
            "type": "verification", "requested_profile": "local-check",
            "capabilities": ["verification"], "verification": [["sh", "-c", "test -f file.txt"]],
        })
        bundle = load_configuration(ROOT, config)
        delegation = route_graph(
            graph, bundle["profiles"], bundle["routing_matrix"],
            bundle["execution_strategy"]["allowed_worker_providers"],
        )
        atomic_write_json(self.orchestration / "task-graph.yaml", graph)
        atomic_write_json(self.orchestration / "delegation.yaml", delegation)
        (self.workspace / "file.txt").write_text("present\n", encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main([
                "run", "--change", str(self.change), "--workspace", str(self.workspace),
                "--no-worktrees", "--skip-review", "--config", str(config),
            ]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "completed")
        report = self.workspace / ".ai-runtime" / "readiness-fixture" / result["run_id"] / "run-report.md"
        self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()
