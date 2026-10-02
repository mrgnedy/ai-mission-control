from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.configuration import load_configuration, validate_configuration_bundle
from orchestration.cli import main as cli_main
from orchestration.engine import WorkflowEngine
from orchestration.executors.claude import ClaudeExecutor
from orchestration.failures import ValidationError, WorkflowError
from orchestration.io import atomic_write_json
from orchestration.models import task_result
from orchestration.permissions import grant_permission, task_permission_context
from orchestration.permissions import _exact_command_rule
from orchestration.provider_registry import ProviderRegistry
from orchestration.router import route_graph
from orchestration.runs import create_run_manifest, resolve_run
from orchestration.runtime import RuntimeStore
from orchestration.scope import _match, validate_scope


class WorkerPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.schemas = ROOT / ".ai/orchestration/schemas"

    def test_project_provider_rules_reach_only_writing_claude_worker(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        rules = ["Bash(flutter test *)", "Bash(dart analyze *)"]
        bundle["providers"]["claude"]["allowed_bash_rules"] = rules
        validate_configuration_bundle(bundle, self.schemas)
        registry = ProviderRegistry(ROOT, bundle["providers"])
        executor = registry.create_executors(["claude"])["claude"]
        self.assertEqual(executor.allowed_bash_rules, tuple(rules))

        task = {
            "id": "TASK-TEST", "title": "Test", "objective": "Synthetic test.",
            "depends_on": [], "scope": {"allowed": ["file.txt"], "forbidden": []},
            "acceptance_criteria": ["No changes"], "verification": [],
        }
        route = {"profile": "claude-implementation", "isolation": "workspace", "timeout_seconds": 10}
        envelope = json.dumps({"structured_output": {"summary": "done", "outcome": "completed", "no_op_reason": "Already satisfied."}})
        with patch("orchestration.executors.claude.git_head", return_value="head"), patch(
            "orchestration.executors.claude.git_snapshot", return_value={}
        ), patch("orchestration.executors.claude.run_process", return_value=(
            subprocess.CompletedProcess([], 0, envelope, ""), 1
        )) as process:
            result = executor.execute(task, route, {}, ROOT, 1)
        self.assertEqual(result["status"], "completed")
        command = process.call_args.args[0]
        self.assertEqual(command[command.index("--allowedTools") + 1:command.index("--json-schema")], rules)
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")

        read_only_route = {**route, "isolation": "read_only"}
        with patch("orchestration.executors.claude.git_head", return_value="head"), patch(
            "orchestration.executors.claude.git_snapshot", return_value={}
        ), patch("orchestration.executors.claude.run_process", return_value=(
            subprocess.CompletedProcess([], 0, envelope, ""), 1
        )) as process:
            executor.execute(task, read_only_route, {}, ROOT, 1)
        command = process.call_args.args[0]
        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("Bash", command[command.index("--tools") + 1])

    def test_old_manifest_and_unsafe_rules(self) -> None:
        registry = ProviderRegistry(ROOT, self.bundle["providers"])
        self.assertEqual(registry.create_executors(["claude"])["claude"].allowed_bash_rules, ())
        for rules in (["Bash(*)"], ["Bash(:*)"], ["Bash(flutter test a,b)"], ["Bash(flutter\ntest)"], ["Bash"]):
            with self.subTest(rules=rules):
                bundle = copy.deepcopy(self.bundle)
                bundle["providers"]["claude"]["allowed_bash_rules"] = rules
                with self.assertRaises(ValidationError):
                    validate_configuration_bundle(bundle, self.schemas)

    def test_non_claude_provider_cannot_use_claude_rules(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        bundle["providers"]["cursor"]["allowed_bash_rules"] = ["Bash(flutter test *)"]
        with self.assertRaisesRegex(ValidationError, "cannot configure Claude Bash rules"):
            validate_configuration_bundle(bundle, self.schemas)

    def test_worker_denial_is_reported_as_permission_required(self) -> None:
        task = {
            "id": "TASK-TEST", "title": "Test", "objective": "Synthetic test.",
            "depends_on": [], "scope": {"allowed": ["file.txt"], "forbidden": []},
            "acceptance_criteria": ["No changes"], "verification": [],
        }
        route = {"profile": "claude-implementation", "isolation": "workspace", "timeout_seconds": 10}
        envelope = json.dumps({"structured_output": {
            "summary": "Verification paused", "outcome": "permission_required",
            "permission_request": {
                "command": "flutter test test/widget_test.dart", "reason": "Need to verify",
                "checkpoint": "Edits are complete; run the test.",
            },
        }})
        with patch("orchestration.executors.claude.git_head", return_value="head"), patch(
            "orchestration.executors.claude.git_snapshot", return_value={}
        ), patch("orchestration.executors.claude.run_process", return_value=(
            subprocess.CompletedProcess([], 0, envelope, ""), 1
        )):
            result = ClaudeExecutor(ROOT).execute(task, route, {}, ROOT, 1)
        self.assertEqual(result["failure"]["code"], "permission_required")
        self.assertEqual(result["permission_request"]["command"], "flutter test test/widget_test.dart")

    def test_exact_grants_reject_compound_and_wildcard_commands(self) -> None:
        for command in ("rm -rf *", "flutter test; rm file.txt", "flutter test && echo done", "flutter test\nwhoami"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "one simple exact command"):
                _exact_command_rule(command)


class ScopePatternTests(unittest.TestCase):
    def test_segment_and_recursive_matching(self) -> None:
        self.assertTrue(_match("ui/button.dart", "ui/*.dart"))
        self.assertFalse(_match("ui/popups/button.dart", "ui/*.dart"))
        self.assertFalse(_match("elsewhere/ui/button.dart", "ui/*.dart"))
        self.assertTrue(_match("ui", "ui/**"))
        self.assertTrue(_match("ui/popups/deep/button.dart", "ui/**"))
        self.assertTrue(_match("ui/button.dart", "ui/**/button.dart"))
        self.assertTrue(_match("ui/popups/deep/button.dart", "ui/**/button.dart"))
        self.assertFalse(_match("ui/popups/deep/button.dart", "ui/*/button.dart"))

    def test_forbidden_precedes_allowed_without_crossing_segments(self) -> None:
        validate_scope(["ui/popups/button.dart"], ["ui/**"], ["ui/*.dart"])
        with self.assertRaises(WorkflowError):
            validate_scope(["ui/button.dart"], ["ui/**"], ["ui/*.dart"])
        with self.assertRaises(WorkflowError):
            validate_scope(["ui/popups/button.dart"], ["ui/*.dart"], [])


class PausingClaude:
    def __init__(self) -> None:
        self.routes = []

    def execute(self, task, route, profile, workspace, attempt):
        self.routes.append(dict(route))
        if attempt == 1:
            (workspace / "file.txt").write_text("partial\n", encoding="utf-8")
            result = task_result(
                task["id"], "failed", "Need Flutter test", "claude", route["profile"], attempt, 1,
                changed_files=["file.txt"],
                failure={"code": "permission_required", "message": "Need Flutter test", "retryable": False},
            )
            result["permission_request"] = {
                "command": "flutter test test/widget_test.dart",
                "reason": "Verify the changed widget",
                "checkpoint": "Implementation is in file.txt; only run the test, then finish.",
            }
            return result
        result = task_result(task["id"], "completed", "Verified", "claude", route["profile"], attempt, 1)
        result["no_op_reason"] = "The partial edit from the previous attempt is present and verified."
        return result


class PermissionHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-permission-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        for command in (("init", "-q"), ("add", "file.txt"),
                        ("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")):
            if command[0] == "add":
                (self.workspace / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", *command], cwd=self.workspace, check=True, capture_output=True)
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.graph = {
            "schema_version": "1.0",
            "workflow": {"id": "permission-fixture", "objective": "Test permission handoff."},
            "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
            "tasks": [{
                "id": "TASK-1", "title": "Synthetic edit", "type": "implementation",
                "objective": "Edit then verify file.txt", "scope": {"allowed": ["file.txt"], "forbidden": []},
                "acceptance_criteria": ["The file is verified."], "depends_on": [],
                "capabilities": ["implementation"], "complexity": "medium", "risk": "low",
                "verification": [], "requested_profile": "claude-implementation",
            }],
        }
        self.delegation = route_graph(
            self.graph, self.bundle["profiles"], self.bundle["routing_matrix"],
            self.bundle["execution_strategy"]["allowed_worker_providers"],
        )
        change = self.workspace / "specs" / "permission-fixture" / "orchestration"
        graph_path, delegation_path = change / "task-graph.yaml", change / "delegation.yaml"
        atomic_write_json(graph_path, self.graph)
        atomic_write_json(delegation_path, self.delegation)
        self.run_id = str(uuid.uuid4())
        self.runtime = RuntimeStore(self.workspace / ".ai-runtime" / "permission-fixture" / self.run_id)
        self.runtime.write_manifest(create_run_manifest(
            self.run_id, "permission-fixture", self.workspace, change.parent, graph_path, delegation_path,
            parallel=False, use_worktrees=False, review=False, provider_binaries={},
        ))
        self.worker = PausingClaude()

    def _engine(self):
        return WorkflowEngine(
            ROOT, self.graph, self.delegation, self.bundle["profiles"], self.workspace, self.runtime,
            {"claude": self.worker}, use_worktrees=False, reviewer=None,
            allowed_executors=self.bundle["execution_strategy"]["allowed_worker_providers"],
        )

    def test_grant_preserves_partial_work_and_resumes_exact_task(self) -> None:
        state = self._engine().run(run_id=self.run_id)
        self.assertEqual(state["status"], "failed")
        record = resolve_run(self.workspace, ROOT / ".ai/orchestration/schemas", self.run_id)
        self.assertEqual(record["classification"], "permission_required")
        self.assertEqual(record["pending_permissions"][0]["command"], "flutter test test/widget_test.dart")
        with self.assertRaisesRegex(ValueError, "partial edits"):
            grant_permission(self.runtime, ROOT / ".ai/orchestration/schemas", "TASK-1", "In scope")
        grant = grant_permission(
            self.runtime, ROOT / ".ai/orchestration/schemas", "TASK-1", "Verify this task",
            partial_reviewed=True,
        )
        self.assertEqual(grant["rule"], "Bash(flutter test test/widget_test.dart)")
        self.assertEqual((self.workspace / "file.txt").read_text(), "partial\n")
        state = self._engine().run(resume=True)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["tasks"]["TASK-1"]["attempts"], 2)
        self.assertEqual(self.worker.routes[1]["continuation"], "Implementation is in file.txt; only run the test, then finish.")
        self.assertEqual(self.worker.routes[1]["extra_allowed_bash_rules"], [grant["rule"]])

    def test_changed_files_after_grant_stop_continuation(self) -> None:
        self._engine().run(run_id=self.run_id)
        grant_permission(
            self.runtime, ROOT / ".ai/orchestration/schemas", "TASK-1", "Verify this task",
            partial_reviewed=True,
        )
        (self.workspace / "file.txt").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "files changed"):
            task_permission_context(self.runtime.root, self.run_id, "TASK-1", 2, self.workspace)

    def test_cli_status_and_grant_are_visible(self) -> None:
        self._engine().run(run_id=self.run_id)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["status", self.run_id, "--workspace", str(self.workspace)]), 0)
        self.assertEqual(json.loads(output.getvalue())["classification"], "permission_required")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main([
                "grant-permission", self.run_id, "TASK-1", "--workspace", str(self.workspace),
                "--reason", "Verify this task", "--partial-reviewed",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "granted")


if __name__ == "__main__":
    unittest.main()
