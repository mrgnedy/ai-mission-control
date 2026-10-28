from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from orchestration.cli import main
from orchestration.configuration import load_configuration
from orchestration.engine import WorkflowEngine
from orchestration.executors.local import LocalExecutor
from orchestration.failures import ValidationError
from orchestration.io import atomic_write_json, load_data
from orchestration.models import task_result
from orchestration.native import workspace_transaction
from orchestration.router import route_graph
from orchestration.runtime import RuntimeStore
from orchestration.scope import git_head, git_snapshot
from orchestration.verification import collect_task_evidence


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-cli-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        (self.workspace / "file.txt").write_text("old\n")
        subprocess.run(["git", "-C", str(self.workspace), "add", "file.txt"], check=True)
        subprocess.run(["git", "-C", str(self.workspace), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.config = self.workspace / ".ai/orchestration/orchestration.yaml"
        shutil.copytree(ROOT / "templates/.ai/orchestration", self.config.parent)
        config = load_data(self.config)
        config["execution_strategy"] = "claude-native"
        config["control_plane"]["review_profile"] = "native-review"
        atomic_write_json(self.config, config)
        self.change = self.workspace / "specs/native"
        orchestration = self.change / "orchestration"
        orchestration.mkdir(parents=True)
        self.graph = {"schema_version": "1.0", "workflow": {"id": "native-cli", "objective": "synthetic"},
                      "parallelization": {"enabled": True, "max_workers": 2, "require_non_overlapping_scope": True},
                      "tasks": [{"id": "TASK-1", "title": "write", "type": "implementation", "objective": "write file.txt",
                                 "scope": {"allowed": ["file.txt"], "forbidden": []}, "acceptance_criteria": ["updated"],
                                 "depends_on": [], "capabilities": ["implementation"], "complexity": "low", "risk": "low", "verification": []}]}
        atomic_write_json(orchestration / "task-graph.yaml", self.graph)
        for name in ("spec.md", "plan.md", "tasks.md"):
            (self.change / name).write_text("synthetic requirements\n")
        self.cli("route", "--change", str(self.change), "--config", str(self.config))

    def cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(list(args)), 0)
        return json.loads(output.getvalue())

    def start(self):
        return self.cli("session", "start", "--change", str(self.change), "--workspace", str(self.workspace), "--config", str(self.config))

    def test_readiness_and_native_assets_gate_before_runtime(self):
        (self.change / "plan.md").unlink()
        with self.assertRaisesRegex(ValidationError, "plan.md"):
            self.start()
        self.assertFalse((self.workspace / ".ai-runtime").exists())
        (self.change / "plan.md").write_text("ready")
        with patch("orchestration.cli.inspect_packaged_assets", return_value={"missing": ["agents/native-reader.md"]}):
            with self.assertRaisesRegex(ValidationError, "native-reader"):
                self.start()
        self.assertFalse((self.workspace / ".ai-runtime").exists())

    def test_native_doctor_reports_session_capability_unproven_and_native_assets(self):
        with patch("orchestration.cli.diagnose_configured", return_value={"providers": {}}), patch("orchestration.cli.inspect_packaged_assets", return_value={"missing": [], "status": "complete"}) as assets:
            report = self.cli("doctor", "--config", str(self.config))
        self.assertEqual(report["native_dispatch"]["status"], "requires_live_session")
        self.assertEqual(assets.call_args.args[1], ["claude-native"])

    def test_native_claim_survives_command_exit_and_cli_cannot_reset_it(self):
        started = self.start()
        run_id = started["run_id"]
        ticket = self.cli("session", "claim", run_id, "TASK-1", "--workspace", str(self.workspace))
        status = self.cli("status", run_id, "--workspace", str(self.workspace))
        self.assertEqual(status["classification"], "active")
        again = self.cli("session", "claim", run_id, "TASK-1", "--workspace", str(self.workspace))
        self.assertEqual(again["claim_id"], ticket["claim_id"])
        self.assertFalse(again["launch_allowed"])
        for command in (["resume", run_id], ["grant-permission", run_id, "TASK-1", "--reason", "needed"],
                        ["recover", run_id], ["reopen", run_id, "TASK-1", "--reason", "needed", "--checkpoint", "remaining"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.cli(*command, "--workspace", str(self.workspace))
        self.assertEqual(load_data(Path(started["runtime"]) / "state.json")["tasks"]["TASK-1"]["attempts"], 1)

    def test_native_writer_hook_uses_claim_and_segment_scope(self):
        self.start()
        # A missing writing claim must fail closed.
        event = {"hook_event_name": "PreToolUse", "tool_name": "Write", "agent_type": "mission-control:native-writer",
                 "agent_id": "synthetic", "cwd": str(self.workspace), "tool_input": {"file_path": str(self.workspace / "file.txt")}}
        environment = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.workspace))
        environment.pop("AI_TASK_SCOPE", None)
        hook = [sys.executable, str(ROOT / "hooks/orchestration_guard.py")]
        self.assertEqual(subprocess.run(hook, input=json.dumps(event), text=True, env=environment, capture_output=True).returncode, 2)
        run_id = self.cli("runs", "--workspace", str(self.workspace))["runs"][0]["run_id"]
        self.cli("session", "claim", run_id, "TASK-1", "--workspace", str(self.workspace))
        self.assertEqual(subprocess.run(hook, input=json.dumps(event), text=True, env=environment, capture_output=True).returncode, 0)
        event["tool_input"]["file_path"] = str(self.workspace / "other.txt")
        self.assertEqual(subprocess.run(hook, input=json.dumps(event), text=True, env=environment, capture_output=True).returncode, 2)

    def test_checks_that_write_outside_scope_fail_even_after_passing_exit(self):
        before = git_snapshot(self.workspace)
        (self.workspace / "file.txt").write_text("new\n")
        task = dict(self.graph["tasks"][0], verification=[[sys.executable, "-c", "from pathlib import Path; Path('forbidden.txt').write_text('oops')"]])
        route = {"executor": "claude", "isolation": "workspace"}
        result = task_result("TASK-1", "completed", "done", "claude", "native-standard", 1, 0)
        result = collect_task_evidence(task, route, self.workspace, git_head(self.workspace), before, result, timeout_seconds=5, force_commands=True)
        self.assertEqual(result["verification"][0]["status"], "passed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["code"], "scope")
        self.assertIn("forbidden.txt", result["changed_files"])

    def test_foreign_shared_cli_cannot_modify_native_owned_checkout(self):
        started = self.start()
        self.cli("session", "claim", started["run_id"], "TASK-1", "--workspace", str(self.workspace))
        bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        foreign = dict(self.graph, tasks=[dict(self.graph["tasks"][0], type="verification", requested_profile="local-check",
                       verification=[[sys.executable, "-c", "from pathlib import Path; Path('file.txt').write_text('foreign')"]])])
        delegation = route_graph(foreign, bundle["profiles"], bundle["routing_matrix"])
        runtime = RuntimeStore(self.workspace / "legacy-outside-discovery")
        engine = WorkflowEngine(ROOT, foreign, delegation, bundle["profiles"], self.workspace, runtime,
                                {"local": LocalExecutor()}, use_worktrees=False)
        with self.assertRaisesRegex(ValueError, "owned"):
            engine.run()
        self.assertEqual((self.workspace / "file.txt").read_text(), "old\n")

    def test_native_claim_refuses_shared_cli_workspace_transaction(self):
        started = self.start()
        with workspace_transaction(self.workspace):
            with self.assertRaisesRegex(ValueError, "operation in progress"):
                self.cli("session", "claim", started["run_id"], "TASK-1", "--workspace", str(self.workspace))

    def test_requested_alias_is_not_accepted_as_observed_model(self):
        started = self.start()
        ticket = self.cli("session", "claim", started["run_id"], "TASK-1", "--workspace", str(self.workspace))
        (self.workspace / "file.txt").write_text("new\n")
        submission = {"claim_id": ticket["claim_id"], "agent_id": "actual", "status": "completed", "summary": "changed", "observed_model": "sonnet"}
        path = self.workspace / ".ai-runtime/submission.json"
        atomic_write_json(path, submission)
        args = ["session", "finish", started["run_id"], "--workspace", str(self.workspace), "--result", str(path),
                "--worker-stopped", "--stop-evidence", "Agent returned"]
        with self.assertRaisesRegex(ValueError, "resolved model ID"):
            self.cli(*args)
        submission.pop("observed_model")
        atomic_write_json(path, submission)
        finished = self.cli(*args)["result"]
        self.assertIsNone(finished["model"])
        self.assertEqual(finished["requested_model"], "sonnet")
        self.assertTrue(finished["warnings"])

    def test_cli_review_reconciles_receipt_after_graph_state_crash(self):
        started = self.start()
        run_id = started["run_id"]
        ticket = self.cli("session", "claim", run_id, "TASK-1", "--workspace", str(self.workspace))
        (self.workspace / "file.txt").write_text("new\n")
        submission_path = self.workspace / ".ai-runtime/submission.json"
        atomic_write_json(submission_path, {"claim_id": ticket["claim_id"], "agent_id": "actual-writer", "status": "completed", "summary": "updated"})
        self.cli("session", "finish", run_id, "--workspace", str(self.workspace), "--result", str(submission_path),
                 "--worker-stopped", "--stop-evidence", "Agent returned")
        reviewer = self.cli("session", "review", run_id, "--workspace", str(self.workspace))
        atomic_write_json(submission_path, {"claim_id": reviewer["claim_id"], "agent_id": "fresh-reader", "review": {
            "schema_version": "1.0", "status": "remediation_required", "summary": "edge missing", "issues": [{
                "id": "REVIEW-EDGE", "severity": "medium", "affected_task": "TASK-1", "description": "fix edge",
                "acceptance_criteria": ["edge covered"]}]}})
        args = ["session", "review", run_id, "--workspace", str(self.workspace), "--result", str(submission_path),
                "--worker-stopped", "--stop-evidence", "Reviewer returned"]
        with patch.object(RuntimeStore, "write_state", side_effect=RuntimeError("receipt-state crash")):
            with self.assertRaisesRegex(RuntimeError, "receipt-state crash"):
                self.cli(*args)
        self.assertEqual(self.cli("status", run_id, "--workspace", str(self.workspace))["classification"], "corrupt")
        repaired = self.cli(*args)
        self.assertEqual(repaired["status"], "remediation_required")
        self.assertEqual(repaired["ready_tasks"], ["TASK-REMEDIATION-REVIEW-EDGE"])


if __name__ == "__main__":
    unittest.main()
