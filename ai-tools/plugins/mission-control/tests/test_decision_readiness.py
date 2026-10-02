from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.cli import main as cli_main
from orchestration.configuration import load_configuration
from orchestration.engine import WorkflowEngine
from orchestration.executors.base import parse_worker_response
from orchestration.executors.claude import ClaudeExecutor
from orchestration.executors.cursor import CursorExecutor
from orchestration.failures import ValidationError, WorkflowError
from orchestration.io import atomic_write_json
from orchestration.models import task_result
from orchestration.report import generate_report
from orchestration.router import route_graph
from orchestration.runs import create_run_manifest, resolve_run, sources_match
from orchestration.runtime import RuntimeStore
from orchestration.state import create_state
from orchestration.validator import validate_decision_dependencies, validate_result


def _task(*, decisions: list[str] | None = None) -> dict:
    result = {
        "id": "TASK-1", "title": "Make a synthetic edit", "type": "implementation",
        "objective": "Create the requested change.",
        "scope": {"allowed": ["file.txt"], "forbidden": []},
        "acceptance_criteria": ["file.txt contains the requested value."],
        "depends_on": [], "capabilities": ["implementation"],
        "complexity": "low", "risk": "low", "verification": [],
        "requested_profile": "claude-implementation",
    }
    if decisions is not None:
        result["decisions"] = decisions
    return result


def _graph(*, decisions: list[str] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "workflow": {"id": "decision-fixture", "objective": "Exercise decision readiness."},
        "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
        "tasks": [_task(decisions=decisions)],
    }


def _index(status: str = "resolved") -> dict:
    entry = {"id": "CLR-1", "status": status, "summary": "Choose the API.", "authority": "user"}
    if status == "resolved":
        entry["resolution"] = "Use the approved API."
    return {"schema_version": "1.0", "decisions": [entry]}


class WorkerOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = _task()
        self.route = {"profile": "claude-implementation", "isolation": "workspace", "timeout_seconds": 10}

    def _execute(self, provider: str, response: object) -> dict:
        if provider == "claude":
            executor = ClaudeExecutor(ROOT)
            envelope = {"structured_output": response}
            module = "orchestration.executors.claude"
        else:
            executor = CursorExecutor(ROOT)
            envelope = {"type": "result", "result": json.dumps(response) if isinstance(response, dict) else response}
            module = "orchestration.executors.cursor"
        with patch(f"{module}.git_head", return_value="head"), patch(
            f"{module}.git_snapshot", return_value={}
        ), patch(f"{module}.run_process", return_value=(
            subprocess.CompletedProcess([], 0, json.dumps(envelope), ""), 1
        )):
            return executor.execute(self.task, self.route, {}, ROOT, 1)

    def test_both_adapters_honor_non_success_and_reject_prose(self) -> None:
        for provider in ("claude", "cursor"):
            with self.subTest(provider=provider):
                failure = self._execute(provider, {"outcome": "failed", "summary": "Required check could not run."})
                self.assertEqual(failure["failure"]["code"], "agent_implementation")
                decision = self._execute(provider, {
                    "outcome": "decision_required", "summary": "Need a choice.",
                    "decision_request": {
                        "question": "Use A or B?", "evidence": "Both match different requirements.",
                        "checkpoint": "No edits made.", "options": ["A", "B"],
                    },
                })
                self.assertEqual(decision["failure"]["code"], "decision_required")
                self.assertEqual(decision["decision_request"]["question"], "Use A or B?")
                validate_result(decision)
                prose = self._execute(provider, "Please confirm before I proceed.")
                self.assertEqual(prose["failure"]["code"], "schema")

    def test_completed_requires_explicit_outcome_and_rejects_failed_summary(self) -> None:
        for provider in ("claude", "cursor"):
            with self.subTest(provider=provider):
                missing = self._execute(provider, {"summary": "Tests pass."})
                self.assertEqual(missing["failure"]["code"], "schema")
                contradictory = self._execute(provider, {
                    "outcome": "completed", "summary": "TASK-14 RESULT: FAILED. Render parity is broken.",
                })
                self.assertEqual(contradictory["failure"]["code"], "agent_implementation")
                no_op = self._execute(provider, {
                    "outcome": "completed", "summary": "Already present.",
                    "no_op_reason": "The requested value is already in file.txt and its assertion passes.",
                })
                self.assertEqual(no_op["status"], "completed")
                self.assertIn("file.txt", no_op["no_op_reason"])

    def test_malformed_decision_request_fails_closed(self) -> None:
        with self.assertRaises(WorkflowError):
            parse_worker_response({"outcome": "decision_required", "summary": "Need choice."})

    def test_verification_worker_structured_failure_is_not_completion(self) -> None:
        self.task["type"] = "verification"
        self.task["verification"] = [["python3", "-c", "print('pass')"]]
        for provider in ("claude", "cursor"):
            with self.subTest(provider=provider):
                result = self._execute(provider, {"outcome": "failed", "summary": "Acceptance still fails."})
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure"]["code"], "agent_implementation")


class DecisionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-decisions-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.graph = _graph(decisions=["CLR-1"])
        self.delegation = route_graph(
            self.graph, self.bundle["profiles"], self.bundle["routing_matrix"],
            self.bundle["execution_strategy"]["allowed_worker_providers"],
        )
        self.change = self.workspace / "specs" / "decision-fixture"
        self.orchestration = self.change / "orchestration"
        self.change.mkdir(parents=True)
        for name in ("spec.md", "plan.md", "tasks.md"):
            (self.change / name).write_text(f"# Synthetic {name}\n", encoding="utf-8")
        atomic_write_json(self.orchestration / "task-graph.yaml", self.graph)
        atomic_write_json(self.orchestration / "delegation.yaml", self.delegation)

    def _cli(self, command: str, *extra: str) -> dict:
        output = StringIO()
        args = [command, "--change", str(self.change)]
        if command == "run":
            args.extend(["--workspace", str(self.workspace), "--dry-run"])
        args.extend(extra)
        with redirect_stdout(output):
            self.assertEqual(cli_main(args), 0)
        return json.loads(output.getvalue())

    def test_validate_and_run_reject_missing_open_and_scheduled_dependencies(self) -> None:
        for status in (None, "open", "scheduled"):
            with self.subTest(status=status):
                if status is not None:
                    atomic_write_json(self.orchestration / "decisions.yaml", _index(status))
                for command in ("validate", "run"):
                    with self.assertRaisesRegex(ValidationError, "decisions"):
                        self._cli(command)
        atomic_write_json(self.orchestration / "decisions.yaml", _index())
        self.assertEqual(self._cli("validate")["status"], "valid")
        self.assertEqual(self._cli("run")["status"], "dry_run")

    def test_old_graph_without_decisions_remains_valid(self) -> None:
        graph = _graph()
        delegation = route_graph(graph, self.bundle["profiles"], self.bundle["routing_matrix"],
                                 self.bundle["execution_strategy"]["allowed_worker_providers"])
        atomic_write_json(self.orchestration / "task-graph.yaml", graph)
        atomic_write_json(self.orchestration / "delegation.yaml", delegation)
        self.assertEqual(self._cli("validate")["status"], "valid")

    def test_unknown_or_duplicate_decisions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown decisions"):
            validate_decision_dependencies(self.graph, {"schema_version": "1.0", "decisions": []})
        duplicated = _index()
        duplicated["decisions"].append(dict(duplicated["decisions"][0]))
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_decision_dependencies(self.graph, duplicated)

    def test_decision_source_is_hashed_and_successor_is_visible(self) -> None:
        decision_path = self.orchestration / "decisions.yaml"
        atomic_write_json(decision_path, _index())
        prior_id = str(uuid.uuid4())
        prior_runtime = RuntimeStore(self.workspace / ".ai-runtime" / "decision-fixture" / prior_id)
        prior_runtime.write_manifest(create_run_manifest(
            prior_id, "decision-fixture", self.workspace, self.change,
            self.orchestration / "task-graph.yaml", self.orchestration / "delegation.yaml",
            parallel=False, use_worktrees=False, review=False, provider_binaries={},
            decisions_path=decision_path,
        ))
        prior_runtime.write_inputs(self.graph, self.delegation)
        prior_state = create_state(self.graph, prior_id)
        prior_state["status"] = "failed"
        prior_state["tasks"]["TASK-1"]["status"] = "failed"
        prior_runtime.write_state(prior_state, self.graph)
        self.assertEqual(resolve_run(self.workspace, ROOT / ".ai/orchestration/schemas", prior_id)["classification"], "failed")
        preview = self._cli("run", "--supersedes", prior_id)
        self.assertEqual(preview["supersedes"], prior_id)
        with patch("orchestration.runs._lock_is_active", return_value=True):
            with self.assertRaisesRegex(ValueError, "cannot supersede an active run"):
                self._cli("run", "--supersedes", prior_id)
        with self.assertRaisesRegex(ValueError, "same workflow and change directory"):
            other = self.workspace / "specs" / "other"
            atomic_write_json(other / "orchestration" / "task-graph.yaml", self.graph)
            atomic_write_json(other / "orchestration" / "delegation.yaml", self.delegation)
            with patch.object(self, "change", other):
                self._cli("run", "--supersedes", prior_id)
        manifest = create_run_manifest(
            str(uuid.uuid4()), "decision-fixture", self.workspace, self.change,
            self.orchestration / "task-graph.yaml", self.orchestration / "delegation.yaml",
            parallel=False, use_worktrees=False, review=False, provider_binaries={},
            decisions_path=decision_path, supersedes=prior_id,
        )
        self.assertTrue(sources_match(manifest))
        atomic_write_json(decision_path, _index("open"))
        self.assertFalse(sources_match(manifest))
        future_runtime = RuntimeStore(self.workspace / ".ai-runtime" / "decision-fixture" / manifest["run_id"])
        future_runtime.write_manifest(manifest)
        future_runtime.write_inputs(self.graph, self.delegation)
        future_state = create_state(self.graph, manifest["run_id"])
        future_runtime.write_state(future_state, self.graph)
        self.assertEqual(resolve_run(self.workspace, ROOT / ".ai/orchestration/schemas", manifest["run_id"])["classification"], "source_changed")
        generate_report(future_runtime.root)
        self.assertIn(prior_id, (future_runtime.root / "run-report.md").read_text(encoding="utf-8"))


class NoOpGuardTests(unittest.TestCase):
    def test_unexplained_zero_edit_fails_but_explained_noop_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mission-control-noop-") as directory:
            workspace = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            (workspace / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=workspace, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base"], cwd=workspace, check=True)
            bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
            graph = _graph()
            delegation = route_graph(graph, bundle["profiles"], bundle["routing_matrix"],
                                     bundle["execution_strategy"]["allowed_worker_providers"])

            class Stub:
                def __init__(self, reason: str | None) -> None:
                    self.reason = reason

                def execute(self, task, route, profile, workspace, attempt):
                    result = task_result(task["id"], "completed", "Claimed complete", "claude", route["profile"], attempt, 1)
                    if self.reason:
                        result["no_op_reason"] = self.reason
                    return result

            for reason, expected in ((None, "failed"), ("Already contains the requested value; checked file.txt.", "completed")):
                with self.subTest(reason=reason):
                    runtime = RuntimeStore(workspace / ".ai-runtime" / "decision-fixture" / str(uuid.uuid4()))
                    engine = WorkflowEngine(
                        ROOT, graph, delegation, bundle["profiles"], workspace, runtime,
                        {"claude": Stub(reason)}, use_worktrees=False, reviewer=None,
                        allowed_executors=bundle["execution_strategy"]["allowed_worker_providers"],
                    )
                    state = engine.run(run_id=runtime.root.name)
                    self.assertEqual(state["status"], expected)


if __name__ == "__main__":
    unittest.main()
