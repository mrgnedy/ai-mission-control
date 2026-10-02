from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.cli import _report_after_stop, main as cli_main
from orchestration.io import atomic_write_json
from orchestration.report import generate_report


class RunReportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-report-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name) / "run"
        self.runtime.mkdir()
        self.graph = {
            "workflow": {"id": "synthetic"},
            "tasks": [
                {"id": "TASK-1", "type": "implementation", "risk": "low", "verification": [["test"]]},
                {"id": "TASK-2", "type": "review", "risk": "medium", "verification": []},
            ],
        }
        self.state = {
            "run_id": "run-123", "workflow_id": "synthetic", "status": "completed",
            "review_status": "passed", "remediation_cycles": 1, "unavailable_providers": [],
            "tasks": {
                "TASK-1": {"status": "completed", "attempts": 2},
                "TASK-2": {"status": "completed", "attempts": 1},
            },
        }
        atomic_write_json(self.runtime / "task-graph.yaml", self.graph)
        atomic_write_json(self.runtime / "state.json", self.state)
        self._result("TASK-1", 1, "failed", "test", "cursor", "cheap", 1200, "SECRET_OUTPUT")
        self._result("TASK-1", 2, "completed", None, "claude", "fallback", 800)
        self._result("TASK-2", 1, "completed", None, "claude", "review", 200)
        atomic_write_json(self.runtime / "reviews" / "review-1.json", {
            "status": "remediation_required", "issues": [{"severity": "medium"}],
        })
        atomic_write_json(self.runtime / "route-revisions" / "0001.json", {
            "routes": {"TASK-1": {"from_profile": "cheap", "to_route": {"profile": "fallback"}}},
        })
        atomic_write_json(self.runtime / "permission-grants" / "0001.json", {
            "task_id": "TASK-1", "command": "SECRET_COMMAND",
        })

    def _result(
        self, task_id: str, attempt: int, status: str, code: str | None,
        executor: str, profile: str, duration_ms: int, secret: str = "",
    ) -> None:
        atomic_write_json(self.runtime / "results" / f"{task_id}-attempt-{attempt}.json", {
            "task_id": task_id, "attempt": attempt, "status": status,
            "executor": executor, "profile": profile, "model": "synthetic-model",
            "duration_ms": duration_ms,
            "failure": {"code": code, "message": secret, "retryable": False} if code else None,
            "verification": [{"status": "failed" if code else "passed", "stdout": secret}] if task_id == "TASK-1" else [],
        })

    def test_completed_report_counts_and_redacts(self) -> None:
        record = generate_report(self.runtime)
        report = (self.runtime / "run-report.md").read_text(encoding="utf-8")
        self.assertFalse(record["provisional"])
        self.assertIn("Tasks completed: 2/2; first-attempt completions: 1/2", report)
        self.assertIn("non-completing attempts: 1; repeat attempts: 1", report)
        self.assertIn("non-completing attempt time: 1.2s", report)
        self.assertIn("Verification records: failed 1, passed 1", report)
        self.assertIn("Applied route revision", report)
        self.assertIn("Permission grant", report)
        self.assertIn("Token usage, actual cost", report)
        self.assertNotIn("SECRET_OUTPUT", report)
        self.assertNotIn("SECRET_COMMAND", report)

    def test_incomplete_and_active_reports_are_provisional(self) -> None:
        self.state["status"] = "failed"
        self.state["tasks"]["TASK-1"]["status"] = "failed"
        atomic_write_json(self.runtime / "state.json", self.state)
        self.assertTrue(generate_report(self.runtime)["provisional"])
        (self.runtime / ".lock").write_text("123", encoding="utf-8")
        self.state["status"] = "completed"
        atomic_write_json(self.runtime / "state.json", self.state)
        self.assertTrue(generate_report(self.runtime)["provisional"])
        self.assertIn("PROVISIONAL", (self.runtime / "run-report.md").read_text(encoding="utf-8"))

    def test_report_command_accepts_legacy_runtime(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["report", "--runtime", str(self.runtime)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["run_id"], "run-123")

    def test_recovery_draft_is_distinguished_from_applied_route(self) -> None:
        change_dir = self.runtime.parent / "specs" / "synthetic"
        draft_path = change_dir / "orchestration" / "recovery-run-123-0001-draft-1.yaml"
        atomic_write_json(self.runtime / "run.json", {"change_dir": str(change_dir)})
        atomic_write_json(draft_path, {
            "run_id": "run-123",
            "routes": {"TASK-1": {"from_profile": "cheap", "to_profile": "fallback"}},
        })
        report = (self.runtime / "run-report.md")
        generate_report(self.runtime)
        self.assertIn("proposed; not recorded as applied", report.read_text(encoding="utf-8"))
        atomic_write_json(self.runtime / "route-revisions" / "0001.json", {
            "plan_path": str(draft_path),
            "routes": {"TASK-1": {"from_profile": "cheap", "to_route": {"profile": "fallback"}}},
        })
        generate_report(self.runtime)
        self.assertIn("(applied)", report.read_text(encoding="utf-8"))

    def test_event_counts_include_review_failure_without_leaking_error(self) -> None:
        events = self.runtime / "logs" / "events.jsonl"
        events.parent.mkdir()
        events.write_text(
            '{"event":"task_retry_scheduled"}\n'
            '{"event":"workflow_resumed"}\n'
            '{"event":"review_failed","cycle":2,"failure_code":"review","error":"SECRET_REVIEW_ERROR"}\n',
            encoding="utf-8",
        )
        generate_report(self.runtime)
        report = (self.runtime / "run-report.md").read_text(encoding="utf-8")
        self.assertIn("Automatic retry decisions: 1; resume events: 1", report)
        self.assertIn("review failures: 1", report)
        self.assertNotIn("SECRET_REVIEW_ERROR", report)

    def test_report_error_does_not_raise_from_closeout(self) -> None:
        output = StringIO()
        with patch("orchestration.cli.generate_report", side_effect=ValueError("synthetic report error")):
            with redirect_stderr(output):
                self.assertIsNone(_report_after_stop(self.runtime))
        self.assertIn("report could not be generated", output.getvalue())


if __name__ == "__main__":
    unittest.main()
