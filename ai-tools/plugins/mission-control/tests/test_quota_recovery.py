from __future__ import annotations

import copy
import os
import json
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.configuration import configuration_snapshot, load_configuration
from orchestration.cli import main as cli_main
from orchestration.engine import WorkflowEngine
from orchestration.executors.claude import ClaudeExecutor
from orchestration.failures import ValidationError
from orchestration.executors.cursor import CursorExecutor, _quota_exhausted
from orchestration.io import atomic_write_json, load_data
from orchestration.models import task_result
from orchestration.recovery import apply_recovery, draft_recovery, effective_delegation, write_recovery_draft
from orchestration.router import route_graph
from orchestration.runs import create_run_manifest, resolve_run
from orchestration.runtime import RuntimeStore
from orchestration.state import transition
from orchestration.validator import validate_delegation


SCHEMAS = ROOT / ".ai/orchestration/schemas"
QUOTA = (
    "ActionRequiredError: You've reached your monthly usage limit "
    "This limit is managed by your organization's group policy."
)


def _task(task_id: str, task_type: str = "implementation", *, risk: str = "low", complexity: str = "medium", depends: list[str] | None = None, requested_profile: str | None = None) -> dict:
    value = {
        "id": task_id,
        "title": task_id,
        "type": task_type,
        "objective": "Check a synthetic task without changing files.",
        "scope": {"allowed": ["file.txt"], "forbidden": []},
        "acceptance_criteria": ["No file changes are needed."],
        "depends_on": depends or [],
        "capabilities": ["schema_validation"] if task_type == "verification" else ["implementation"],
        "complexity": complexity,
        "risk": risk,
        "verification": [],
    }
    if requested_profile:
        value["requested_profile"] = requested_profile
    return value


def _graph(tasks: list[dict]) -> dict:
    return {
        "schema_version": "1.0",
        "workflow": {"id": "quota-fixture", "objective": "Exercise quota recovery in isolation."},
        "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
        "tasks": tasks,
    }


def _git(*args: str, cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)


class StubExecutor:
    def __init__(self, *, quota: bool = False) -> None:
        self.quota = quota
        self.calls: list[str] = []

    def execute(self, task: dict, route: dict, profile: dict, workspace: Path, attempt: int) -> dict:
        self.calls.append(task["id"])
        if self.quota:
            return task_result(
                task["id"], "failed", QUOTA, route["executor"], route["profile"], attempt, 1,
                failure={"code": "provider_unavailable", "message": QUOTA, "retryable": False},
            )
        result = task_result(task["id"], "completed", "Synthetic success", route["executor"], route["profile"], attempt, 1)
        result["no_op_reason"] = "Synthetic task explicitly requires no file changes."
        return result


class PartialCursor(StubExecutor):
    def execute(self, task: dict, route: dict, profile: dict, workspace: Path, attempt: int) -> dict:
        (workspace / "file.txt").write_text("unfinished\n", encoding="utf-8")
        result = super().execute(task, route, profile, workspace, attempt)
        result["changed_files"] = ["file.txt"]
        return result


class RemediationReview:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, graph: dict, results: list[dict], workspace: Path, cycle: int) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "schema_version": "1.0", "status": "remediation_required", "summary": "Synthetic finding",
                "issues": [{
                    "id": "quota-regression", "severity": "medium", "affected_task": "TASK-1",
                    "description": "Check the synthetic fallback output.",
                    "acceptance_criteria": ["Synthetic check passes."],
                }],
            }
        return {"schema_version": "1.0", "status": "pass", "summary": "Synthetic pass", "issues": []}


class QuotaRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mission-control-recovery-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        _git("init", "-q", cwd=self.workspace)
        (self.workspace / "file.txt").write_text("base\n", encoding="utf-8")
        _git("add", "file.txt", cwd=self.workspace)
        _git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base", cwd=self.workspace)
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.profiles = self.bundle["profiles"]
        self.allowed = self.bundle["execution_strategy"]["allowed_worker_providers"]

    def _start(self) -> tuple[dict, dict, RuntimeStore, dict, StubExecutor]:
        graph = _graph([
            _task("TASK-0", "verification"),
            _task("TASK-1"),
            _task("TASK-2", depends=["TASK-1"]),
            _task("TASK-3"),
        ])
        delegation = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)
        change = self.workspace / "specs" / "quota-fixture"
        graph_path = change / "orchestration" / "task-graph.yaml"
        delegation_path = change / "orchestration" / "delegation.yaml"
        atomic_write_json(graph_path, graph)
        atomic_write_json(delegation_path, delegation)
        run_id = str(uuid.uuid4())
        runtime = RuntimeStore(self.workspace / ".ai-runtime" / graph["workflow"]["id"] / run_id)
        manifest = create_run_manifest(
            run_id, graph["workflow"]["id"], self.workspace, change, graph_path, delegation_path,
            parallel=False, use_worktrees=False, review=False, provider_binaries={},
        )
        runtime.write_manifest(manifest)
        cursor = StubExecutor(quota=True)
        engine = self._engine(graph, delegation, runtime, cursor, StubExecutor(), StubExecutor())
        state = engine.run(run_id=run_id)
        return graph, delegation, runtime, state, cursor

    def _engine(self, graph: dict, delegation: dict, runtime: RuntimeStore, cursor: StubExecutor, claude: StubExecutor, local: StubExecutor) -> WorkflowEngine:
        return WorkflowEngine(
            ROOT, graph, delegation, self.profiles, self.workspace, runtime,
            {"cursor": cursor, "claude": claude, "local": local},
            use_worktrees=False, reviewer=None, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        )

    def test_cost_first_routes_and_pinned_profile(self) -> None:
        template = load_configuration(ROOT, ROOT / "templates/.ai/orchestration/orchestration.yaml")
        self.assertEqual(template["routing_matrix"]["fallback_rules"], self.bundle["routing_matrix"]["fallback_rules"])
        graph = _graph([
            _task("TASK-A", "codemod"),
            _task("TASK-B"),
            _task("TASK-C", complexity="high", risk="medium"),
            _task("TASK-D", complexity="high", risk="high"),
            _task("TASK-E", "analysis", risk="medium"),
            _task("TASK-F", "analysis", risk="high"),
            _task("TASK-G", requested_profile="cursor-implementation"),
        ])
        delegation = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)
        plan = delegation["execution_plan"]
        expected = {
            "TASK-A": "claude-mechanical", "TASK-B": "claude-implementation",
            "TASK-C": "claude-implementation", "TASK-D": "claude-reasoning",
            "TASK-E": "claude-analysis", "TASK-F": "claude-review",
        }
        for task_id, profile in expected.items():
            self.assertEqual(plan[task_id]["fallbacks"][0]["profile"], profile)
        self.assertNotIn("fallbacks", plan["TASK-G"])
        validate_delegation(graph, delegation, self.profiles, self.bundle["providers"], self.allowed)
        invalid = copy.deepcopy(delegation)
        invalid["execution_plan"]["TASK-B"]["fallbacks"][0]["profile"] = "claude-analysis"
        with self.assertRaisesRegex(ValidationError, "changes isolation"):
            validate_delegation(graph, invalid, self.profiles, self.bundle["providers"], self.allowed)

    def test_cursor_quota_is_nonretryable_and_unknown_error_is_not_quota(self) -> None:
        self.assertTrue(_quota_exhausted(QUOTA))
        self.assertFalse(_quota_exhausted("Temporary server error; please retry"))
        graph = _graph([_task("TASK-1")])
        route = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)["execution_plan"]["TASK-1"]
        with patch("orchestration.executors.cursor.run_process") as process:
            process.return_value = (subprocess.CompletedProcess([], 1, "", QUOTA), 1)
            result = CursorExecutor(ROOT).execute(graph["tasks"][0], route, self.profiles["profiles"][route["profile"]], self.workspace, 1)
        self.assertEqual(result["failure"]["code"], "provider_unavailable")
        self.assertFalse(result["failure"]["retryable"])

    def test_claude_session_limit_is_nonretryable(self) -> None:
        task = _task("TASK-CLAUDE", "analysis")
        profile = self.profiles["profiles"]["claude-analysis"]
        route = {
            "profile": "claude-analysis", "executor": "claude", "isolation": "read_only",
            "model": profile["model"], "effort": profile["effort"], "timeout_seconds": 120,
        }
        envelope = {"is_error": True, "api_error_status": 429, "result": "You've hit your session limit · resets 3am"}
        with patch("orchestration.executors.claude.run_process") as process:
            process.return_value = (subprocess.CompletedProcess([], 1, json.dumps(envelope), ""), 1)
            result = ClaudeExecutor(ROOT).execute(task, route, profile, self.workspace, 1)
        self.assertEqual(result["failure"]["code"], "provider_unavailable")
        self.assertFalse(result["failure"]["retryable"])

    def test_recover_and_resume_preserves_completed_work(self) -> None:
        graph, original, runtime, state, cursor = self._start()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(cursor.calls, ["TASK-1"])
        self.assertEqual(state["tasks"]["TASK-0"]["status"], "completed")
        self.assertEqual(state["tasks"]["TASK-1"]["attempts"], 1)
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "recovery_required")

        manifest_before = load_data(runtime.manifest_path)
        delegation_before = load_data(runtime.root / "delegation.yaml")
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(set(plan["routes"]), {"TASK-1", "TASK-2", "TASK-3"})
        path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", plan)
        applied = apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(applied["status"], "recovered")
        self.assertEqual(load_data(runtime.manifest_path), manifest_before)
        self.assertEqual(load_data(runtime.root / "delegation.yaml"), delegation_before)
        effective = effective_delegation(runtime.root, SCHEMAS)
        self.assertEqual(effective["execution_plan"]["TASK-1"]["profile"], "claude-implementation")
        self.assertEqual(original["execution_plan"]["TASK-1"]["profile"], "cursor-implementation")
        state = runtime.load_state(graph)
        self.assertEqual(state["tasks"]["TASK-0"]["status"], "completed")
        self.assertEqual(state["tasks"]["TASK-1"]["attempts"], 1)
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "resumable")
        replacement = StubExecutor()
        resumed = self._engine(graph, effective, runtime, StubExecutor(quota=True), replacement, StubExecutor()).run(resume=True, run_id=state["run_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(set(replacement.calls), {"TASK-1", "TASK-2", "TASK-3"})
        self.assertEqual(resumed["tasks"]["TASK-0"]["attempts"], 1)
        self.assertEqual(resumed["tasks"]["TASK-1"]["attempts"], 2)
        self.assertEqual(load_data(runtime.root / "delegation.yaml"), delegation_before)

    def test_report_command_reads_a_discovered_failed_run(self) -> None:
        _, _, _, state, _ = self._start()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["report", state["run_id"], "--workspace", str(self.workspace)]), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["provisional"])
        self.assertEqual(report["run_id"], state["run_id"])
        text = Path(report["report"]).read_text(encoding="utf-8")
        self.assertIn("provider_unavailable", text)
        self.assertIn("TASK-1", text)

    def test_partial_edits_require_review_and_stale_plan_is_rejected(self) -> None:
        graph, _, runtime, state, _ = self._start()
        partial = runtime.worktrees / "task-1"
        _git("worktree", "add", "-qb", "partial-test", str(partial), cwd=self.workspace)
        (partial / "file.txt").write_text("unfinished\n", encoding="utf-8")
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(plan["routes"]["TASK-1"]["changed_files"], ["file.txt"])
        path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", plan)
        (partial / "file.txt").write_text("changed again\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence.*changed"):
            apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        (partial / "file.txt").write_text("unfinished\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "partial edits"):
            apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        plan["routes"]["TASK-1"]["partial_reviewed"] = True
        atomic_write_json(path, plan)
        apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual((partial / "file.txt").read_text(encoding="utf-8"), "unfinished\n")
        again = apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(again["revision"], str(runtime.root / "route-revisions" / "0001.json"))
        self.assertEqual(len(list((runtime.root / "route-revisions").glob("*.json"))), 1)

    def test_real_worktree_partial_changes_survive_fallback(self) -> None:
        graph = _graph([_task("TASK-1")])
        delegation = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)
        change = self.workspace / "specs" / "quota-fixture"
        graph_path = change / "orchestration" / "task-graph.yaml"
        delegation_path = change / "orchestration" / "delegation.yaml"
        atomic_write_json(graph_path, graph)
        atomic_write_json(delegation_path, delegation)
        run_id = str(uuid.uuid4())
        runtime = RuntimeStore(self.workspace / ".ai-runtime" / "quota-fixture" / run_id)
        runtime.write_manifest(create_run_manifest(
            run_id, "quota-fixture", self.workspace, change, graph_path, delegation_path,
            parallel=False, use_worktrees=True, review=False, provider_binaries={},
        ))
        cursor = PartialCursor(quota=True)
        engine = WorkflowEngine(
            ROOT, graph, delegation, self.profiles, self.workspace, runtime,
            {"cursor": cursor, "claude": StubExecutor(), "local": StubExecutor()},
            use_worktrees=True, reviewer=None, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        )
        self.assertEqual(engine.run(run_id=run_id)["status"], "failed")
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(plan["routes"]["TASK-1"]["changed_files"], ["file.txt"])
        plan["routes"]["TASK-1"]["partial_reviewed"] = True
        path = write_recovery_draft(change, plan)
        apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        effective = effective_delegation(runtime.root, SCHEMAS)
        replacement = StubExecutor()
        resumed = WorkflowEngine(
            ROOT, graph, effective, self.profiles, self.workspace, runtime,
            {"cursor": StubExecutor(quota=True), "claude": replacement, "local": StubExecutor()},
            use_worktrees=True, reviewer=None, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        ).run(resume=True, run_id=run_id)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual((runtime.worktrees / "integration" / "file.txt").read_text(encoding="utf-8"), "unfinished\n")
        self.assertEqual((self.workspace / "file.txt").read_text(encoding="utf-8"), "base\n")
        result = load_data(runtime.results / "TASK-1-attempt-2.json")
        self.assertEqual(result["changed_files"], ["file.txt"])

    def test_cli_recovery_preview_and_resume_dry_run(self) -> None:
        _, _, runtime, state, _ = self._start()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["recover", state["run_id"], "--workspace", str(self.workspace)]), 0)
        draft = json.loads(output.getvalue())
        self.assertEqual(draft["status"], "drafted")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["recover", state["run_id"], "--workspace", str(self.workspace), "--apply", draft["plan"]]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "recovered")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["resume", state["run_id"], "--workspace", str(self.workspace), "--dry-run"]), 0)
        preview = json.loads(output.getvalue())
        self.assertEqual(preview["execution_plan"]["TASK-1"]["profile"], "claude-implementation")

    def test_interrupted_fallback_resumes_from_saved_route(self) -> None:
        graph, _, runtime, state, _ = self._start()
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", plan)
        apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        saved = runtime.load_state(graph)
        transition(saved, "TASK-1", "running")
        runtime.write_state(saved, graph)
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "interrupted")
        replacement = StubExecutor()
        resumed = self._engine(
            graph, effective_delegation(runtime.root, SCHEMAS), runtime,
            StubExecutor(quota=True), replacement, StubExecutor(),
        ).run(resume=True, run_id=state["run_id"])
        self.assertEqual(resumed["status"], "completed")
        self.assertIn("TASK-1", replacement.calls)
        self.assertEqual(resumed["tasks"]["TASK-1"]["attempts"], 3)

    def test_new_review_task_stops_before_reusing_exhausted_cursor(self) -> None:
        graph, _, runtime, state, _ = self._start()
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", plan)
        apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        reviewer = RemediationReview()
        claude = StubExecutor()
        first = WorkflowEngine(
            ROOT, graph, effective_delegation(runtime.root, SCHEMAS), self.profiles, self.workspace, runtime,
            {"cursor": StubExecutor(quota=True), "claude": claude, "local": StubExecutor()},
            use_worktrees=False, reviewer=reviewer, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        ).run(resume=True, run_id=state["run_id"])
        self.assertEqual(first["status"], "failed")
        self.assertEqual(reviewer.calls, 1)
        record = resolve_run(self.workspace, SCHEMAS, state["run_id"])
        self.assertEqual(record["classification"], "recovery_required")
        second_plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        self.assertEqual(set(second_plan["routes"]), {"TASK-REMEDIATION-QUOTA-REGRESSION"})
        second_path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", second_plan)
        apply_recovery(runtime, second_path, SCHEMAS, self.profiles, self.allowed)
        updated_graph = load_data(runtime.root / "task-graph.yaml")
        final = WorkflowEngine(
            ROOT, updated_graph, effective_delegation(runtime.root, SCHEMAS), self.profiles, self.workspace, runtime,
            {"cursor": StubExecutor(quota=True), "claude": claude, "local": StubExecutor()},
            use_worktrees=False, reviewer=reviewer, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        ).run(resume=True, run_id=state["run_id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["review_status"], "passed")
        self.assertEqual(reviewer.calls, 2)

    def test_confirmed_provider_restoration_reopens_without_replanning(self) -> None:
        graph, _, runtime, state, _ = self._start()
        first_plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        first_path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", first_plan)
        apply_recovery(runtime, first_path, SCHEMAS, self.profiles, self.allowed)
        failed_again = self._engine(
            graph, effective_delegation(runtime.root, SCHEMAS), runtime,
            StubExecutor(quota=True), StubExecutor(quota=True), StubExecutor(),
        ).run(resume=True, run_id=state["run_id"])
        self.assertEqual(failed_again["status"], "failed")
        self.assertEqual(failed_again["unavailable_providers"], ["claude", "cursor"])
        with self.assertRaisesRegex(ValueError, "no approved available fallback"):
            draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["recover", state["run_id"], "--workspace", str(self.workspace), "--restore-provider", "claude"]), 0)
        restore_path = Path(json.loads(output.getvalue())["plan"])
        restoration = load_data(restore_path)
        self.assertEqual(restoration["routes"]["TASK-1"]["to_profile"], "claude-implementation")
        with self.assertRaisesRegex(ValueError, "confirm restored provider availability"):
            apply_recovery(runtime, restore_path, SCHEMAS, self.profiles, self.allowed)
        restoration["availability_confirmed"] = True
        restoration["availability_evidence"] = "A read-only Claude CLI probe returned a successful response after reset."
        atomic_write_json(restore_path, restoration)
        with patch.object(runtime, "write_state", side_effect=RuntimeError("simulated restoration interruption")):
            with self.assertRaisesRegex(RuntimeError, "simulated restoration interruption"):
                apply_recovery(runtime, restore_path, SCHEMAS, self.profiles, self.allowed)
        pending = resolve_run(self.workspace, SCHEMAS, state["run_id"])
        self.assertEqual(pending["pending_recovery_plan"], str(restore_path.resolve()))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["recover", state["run_id"], "--workspace", str(self.workspace), "--apply", str(restore_path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "recovered")
        restored = runtime.load_state(graph)
        self.assertEqual(restored["unavailable_providers"], ["cursor"])
        self.assertEqual(restored["tasks"]["TASK-1"]["attempts"], 2)
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "resumable")
        completed = self._engine(
            graph, effective_delegation(runtime.root, SCHEMAS), runtime,
            StubExecutor(quota=True), StubExecutor(), StubExecutor(),
        ).run(resume=True, run_id=state["run_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["tasks"]["TASK-1"]["attempts"], 3)

    def test_parallel_quota_drains_in_flight_batch_without_dispatching_next(self) -> None:
        tasks = [_task("TASK-1"), _task("TASK-2"), _task("TASK-3")]
        for index, task in enumerate(tasks, 1):
            task["scope"]["allowed"] = [f"independent-{index}.txt"]
        graph = _graph(tasks)
        graph["parallelization"].update({"enabled": True, "max_workers": 2})
        delegation = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)
        runtime = RuntimeStore(self.workspace / ".ai-runtime" / "quota-fixture" / str(uuid.uuid4()))
        cursor = StubExecutor(quota=True)
        engine = WorkflowEngine(
            ROOT, graph, delegation, self.profiles, self.workspace, runtime,
            {"cursor": cursor, "claude": StubExecutor(), "local": StubExecutor()},
            parallel=True, use_worktrees=True, reviewer=None,
            routing_matrix=self.bundle["routing_matrix"], allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        )
        state = engine.run(run_id=runtime.root.name)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(set(cursor.calls), {"TASK-1", "TASK-2"})
        self.assertEqual(state["tasks"]["TASK-3"]["status"], "ready")
        self.assertEqual(state["unavailable_providers"], ["cursor"])

    def test_interrupted_apply_can_finish_from_saved_revision(self) -> None:
        _, _, runtime, state, _ = self._start()
        plan = draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        path = write_recovery_draft(self.workspace / "specs" / "quota-fixture", plan)
        with patch.object(runtime, "write_state", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)
        record = resolve_run(self.workspace, SCHEMAS, state["run_id"])
        self.assertEqual(record["classification"], "recovery_required")
        self.assertEqual(record["pending_recovery_plan"], str(path.resolve()))
        self.assertEqual(apply_recovery(runtime, path, SCHEMAS, self.profiles, self.allowed)["status"], "recovered")

    def test_missing_approved_fallback_and_changed_source_stop_recovery(self) -> None:
        _, _, runtime, state, _ = self._start()
        delegation = load_data(runtime.root / "delegation.yaml")
        delegation["execution_plan"]["TASK-1"].pop("fallbacks")
        atomic_write_json(runtime.root / "delegation.yaml", delegation)
        with self.assertRaisesRegex(ValueError, "no approved available fallback"):
            draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)
        source = self.workspace / "specs" / "quota-fixture" / "orchestration" / "delegation.yaml"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "source_changed")
        with self.assertRaisesRegex(ValueError, "sources changed"):
            draft_recovery(runtime.root, SCHEMAS, self.profiles, self.allowed)

    def test_legacy_run_without_fallback_fields_remains_readable(self) -> None:
        graph, _, runtime, state, _ = self._start()
        delegation = load_data(runtime.root / "delegation.yaml")
        for route in delegation["execution_plan"].values():
            route.pop("fallbacks", None)
        atomic_write_json(runtime.root / "delegation.yaml", delegation)
        old_state = runtime.load_state(graph)
        old_state.pop("unavailable_providers")
        old_state["tasks"]["TASK-1"]["failure_code"] = "tool"
        runtime.write_state(old_state, graph)
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, state["run_id"])["classification"], "failed")

    @unittest.skipUnless(os.environ.get("MC_LIVE_CURSOR") == "1", "requires current Cursor quota and explicit opt-in")
    def test_live_exhausted_cursor_stops_after_one_attempt(self) -> None:
        graph = _graph([_task("TASK-1")])
        delegation = route_graph(graph, self.profiles, self.bundle["routing_matrix"], self.allowed)
        delegation["execution_plan"]["TASK-1"]["timeout_seconds"] = 90
        change = self.workspace / "specs" / "live-quota"
        graph_path = change / "orchestration" / "task-graph.yaml"
        delegation_path = change / "orchestration" / "delegation.yaml"
        atomic_write_json(graph_path, graph)
        atomic_write_json(delegation_path, delegation)
        run_id = str(uuid.uuid4())
        runtime = RuntimeStore(self.workspace / ".ai-runtime" / "quota-fixture" / run_id)
        runtime.write_manifest(create_run_manifest(
            run_id, "quota-fixture", self.workspace, change, graph_path, delegation_path,
            parallel=False, use_worktrees=False, review=False, provider_binaries={},
        ))
        engine = WorkflowEngine(
            ROOT, graph, delegation, self.profiles, self.workspace, runtime,
            {"cursor": CursorExecutor(ROOT), "claude": StubExecutor(), "local": StubExecutor()},
            use_worktrees=False, reviewer=None, routing_matrix=self.bundle["routing_matrix"],
            allowed_executors=self.allowed,
            parallel_executors=self.bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(self.bundle),
        )
        state = engine.run(run_id=run_id)
        self.assertEqual(state["tasks"]["TASK-1"]["failure_code"], "provider_unavailable")
        self.assertEqual(state["tasks"]["TASK-1"]["attempts"], 1)
        self.assertEqual(state["unavailable_providers"], ["cursor"])
        self.assertEqual(resolve_run(self.workspace, SCHEMAS, run_id)["classification"], "recovery_required")

    @unittest.skipUnless(os.environ.get("MC_LIVE_CLAUDE") == "1", "requires authenticated Claude and explicit opt-in")
    def test_live_claude_profile_or_session_limit(self) -> None:
        task = _task("TASK-CLAUDE", "analysis")
        task["objective"] = "Read file.txt, report that it contains base, and make no edits."
        profile = self.profiles["profiles"]["claude-analysis"]
        route = {
            "profile": "claude-analysis", "executor": "claude", "isolation": "read_only",
            "model": profile["model"], "effort": profile["effort"], "timeout_seconds": 120,
        }
        result = ClaudeExecutor(ROOT).execute(task, route, profile, self.workspace, 1)
        if result["status"] == "failed":
            self.assertEqual(result["failure"]["code"], "provider_unavailable", result.get("errors"))
            self.assertIn("session limit", result["summary"].lower())
        else:
            self.assertEqual(result["status"], "completed")
        self.assertEqual(result["changed_files"], [])


if __name__ == "__main__":
    unittest.main()
