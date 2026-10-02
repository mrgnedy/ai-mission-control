from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.configuration import load_configuration
from orchestration.cli import main as cli_main
from orchestration.continuation import reopen_task, task_continuation_context, task_decision_context
from orchestration.engine import WorkflowEngine
from orchestration.io import atomic_write_json, load_data
from orchestration.models import task_result
from orchestration.prompts import render_task_prompt
from orchestration.report import generate_report
from orchestration.router import route_graph
from orchestration.runs import create_run_manifest, file_sha256, resolve_run
from orchestration.runtime import RuntimeStore


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _task(task_id: str, path: str, depends: list[str] | None = None) -> dict:
    return {
        "id": task_id, "title": task_id, "type": "implementation", "objective": f"Update {path}",
        "scope": {"allowed": [path], "forbidden": []}, "acceptance_criteria": [f"{path} is correct"],
        "depends_on": depends or [], "capabilities": ["implementation"],
        "complexity": "medium", "risk": "low", "verification": [],
        "requested_profile": "claude-implementation",
    }


class SyntheticWorker:
    def __init__(self, failure: str = "agent_implementation", overlap: bool = False) -> None:
        self.failure = failure
        self.overlap = overlap
        self.calls: list[tuple[str, int, dict]] = []

    def execute(self, task, route, profile, workspace, attempt):
        task_id = task["id"]
        self.calls.append((task_id, attempt, dict(route)))
        if task_id == "TASK-1" and attempt == 1:
            if self.failure == "decision_required":
                result = task_result(task_id, "failed", "Need a choice", "claude", route["profile"], attempt, 1,
                                     failure={"code": self.failure, "message": "Need a choice", "retryable": False})
                result["decision_request"] = {
                    "question": "A or B?", "evidence": "Both appear possible", "checkpoint": "No edit yet.",
                }
                return result
            (workspace / "one.txt").write_text("partial\n", encoding="utf-8")
            result = task_result(task_id, "failed", "Stopped after partial edit", "claude", route["profile"], attempt, 1,
                                 changed_files=["one.txt"],
                                 failure={"code": self.failure, "message": "Stopped", "retryable": False})
            if self.failure == "permission_required":
                result["permission_request"] = {
                    "command": "flutter test test/widget_test.dart", "reason": "Need test",
                    "checkpoint": "Partial edit in one.txt; run test.",
                }
            return result
        if task_id == "TASK-1":
            (workspace / "one.txt").write_text("complete\n", encoding="utf-8")
            return task_result(task_id, "completed", "Finished retained work", "claude", route["profile"], attempt, 1,
                               changed_files=["one.txt"])
        path = "one.txt" if task_id == "TASK-2" and self.overlap else {"TASK-0": "zero.txt", "TASK-2": "two.txt", "TASK-3": "three.txt"}[task_id]
        (workspace / path).write_text(task_id + "\n", encoding="utf-8")
        return task_result(task_id, "completed", "Done", "claude", route["profile"], attempt, 1,
                           changed_files=[path])


class ContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-continuation-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        _git(self.workspace, "init", "-q")
        for name in ("zero.txt", "one.txt", "two.txt", "three.txt"):
            (self.workspace / name).write_text("base\n", encoding="utf-8")
        _git(self.workspace, "add", ".")
        _git(self.workspace, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.schemas = ROOT / ".ai/orchestration/schemas"

    def _start(self, *, worktrees: bool, failure: str = "agent_implementation", overlap: bool = False, decisions: bool = False):
        graph = {
            "schema_version": "1.0", "workflow": {"id": "continuation-fixture", "objective": "Synthetic continuation"},
            "parallelization": {"enabled": False, "max_workers": 1, "require_non_overlapping_scope": True},
            "tasks": [_task("TASK-0", "zero.txt"), _task("TASK-1", "one.txt"),
                      _task("TASK-2", "one.txt" if overlap else "two.txt"),
                      _task("TASK-3", "three.txt", ["TASK-1"])],
        }
        if decisions:
            graph["tasks"][1]["decisions"] = ["CLR-1"]
        delegation = route_graph(graph, self.bundle["profiles"], self.bundle["routing_matrix"],
                                 self.bundle["execution_strategy"]["allowed_worker_providers"])
        change = self.workspace / "specs" / "continuation-fixture"
        graph_path = change / "orchestration" / "task-graph.yaml"
        delegation_path = change / "orchestration" / "delegation.yaml"
        atomic_write_json(graph_path, graph)
        atomic_write_json(delegation_path, delegation)
        decision_path = change / "orchestration" / "decisions.yaml"
        if decisions:
            atomic_write_json(decision_path, {"schema_version": "1.0", "decisions": [
                {"id": "CLR-1", "status": "resolved", "summary": "Approved API", "authority": "user",
                 "resolution": "Use API A."},
                {"id": "CLR-2", "status": "resolved", "summary": "Unrelated", "authority": "user",
                 "resolution": "Do not pass this to TASK-1."},
            ]})
        run_id = str(uuid.uuid4())
        runtime = RuntimeStore(self.workspace / ".ai-runtime" / "continuation-fixture" / run_id)
        runtime.write_manifest(create_run_manifest(
            run_id, "continuation-fixture", self.workspace, change, graph_path, delegation_path,
            parallel=False, use_worktrees=worktrees, review=False, provider_binaries={},
            decisions_path=decision_path if decisions else None,
        ))
        worker = SyntheticWorker(failure, overlap)

        def engine():
            return WorkflowEngine(ROOT, graph, delegation, self.bundle["profiles"], self.workspace,
                                  runtime, {"claude": worker}, use_worktrees=worktrees, reviewer=None,
                                  allowed_executors=self.bundle["execution_strategy"]["allowed_worker_providers"])

        state = engine().run(run_id=run_id)
        return run_id, runtime, worker, engine, state

    def test_reopen_preserves_completed_siblings_and_partial_work_in_both_modes(self) -> None:
        for worktrees in (False, True):
            with self.subTest(worktrees=worktrees):
                run_id, runtime, worker, engine, state = self._start(worktrees=worktrees)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["tasks"]["TASK-0"]["status"], "completed")
                self.assertEqual(state["tasks"]["TASK-2"]["status"], "completed")
                sibling_path = runtime.results / "TASK-0-attempt-1.json"
                sibling_hash = file_sha256(sibling_path)
                old_result = runtime.results / "TASK-1-attempt-1.json"
                old_hash = file_sha256(old_result)
                with self.assertRaisesRegex(ValueError, "partial edits"):
                    reopen_task(runtime, self.schemas, "TASK-1", "Reviewed partial edit", checkpoint="Finish one.txt")
                preview = reopen_task(runtime, self.schemas, "TASK-1", "Reviewed partial edit",
                                      checkpoint="Finish one.txt", partial_reviewed=True, dry_run=True)
                self.assertEqual(preview["status"], "eligible")
                self.assertEqual(state["tasks"]["TASK-1"]["attempts"], 1)
                action = reopen_task(runtime, self.schemas, "TASK-1", "Reviewed partial edit",
                                     checkpoint="Finish one.txt", partial_reviewed=True)
                self.assertEqual(action["status"], "reopened")
                self.assertEqual(resolve_run(self.workspace, self.schemas, run_id)["task_reopens"], 1)
                resumed = engine().run(resume=True)
                self.assertEqual(resumed["status"], "completed")
                self.assertEqual(resumed["tasks"]["TASK-1"]["attempts"], 2)
                self.assertEqual(file_sha256(old_result), old_hash)
                self.assertEqual(file_sha256(sibling_path), sibling_hash)
                self.assertEqual(sum(task_id == "TASK-0" for task_id, _, _ in worker.calls), 1)
                self.assertEqual(sum(task_id == "TASK-2" for task_id, _, _ in worker.calls), 1)
                self.assertEqual(worker.calls[-2][2]["continuation"], "Finish one.txt")
                report = generate_report(runtime.root)
                body = Path(report["report"]).read_text(encoding="utf-8")
                self.assertIn("Task reopen 0001", body)
                self.assertNotIn("Finish one.txt", body)

    def test_shared_workspace_overlap_and_staleness_refuse(self) -> None:
        _, runtime, _, _, _ = self._start(worktrees=False, overlap=True)
        with self.assertRaisesRegex(ValueError, "overlap"):
            reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)

    def test_reopened_path_change_refuses_worker_dispatch(self) -> None:
        run_id, runtime, worker, engine, _ = self._start(worktrees=False)
        reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)
        (self.workspace / "one.txt").write_text("later user edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "files changed after reopen"):
            task_continuation_context(runtime.root, run_id, "TASK-1", 2, self.workspace)
        calls_before = len(worker.calls)
        resumed = engine().run(resume=True)
        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(len(worker.calls), calls_before)

    def test_unrelated_shared_edit_after_reopen_does_not_invalidate_checkpoint(self) -> None:
        run_id, runtime, _, _, _ = self._start(worktrees=False)
        reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)
        (self.workspace / "two.txt").write_text("unrelated later edit\n", encoding="utf-8")
        context = task_continuation_context(runtime.root, run_id, "TASK-1", 2, self.workspace)
        self.assertEqual(context["continuation"], "Finish")

    def test_unrecorded_task_edit_refuses_reopen(self) -> None:
        _, runtime, _, _, _ = self._start(worktrees=False)
        result_path = runtime.results / "TASK-1-attempt-1.json"
        result = load_data(result_path)
        result["changed_files"] = []
        atomic_write_json(result_path, result)
        with self.assertRaisesRegex(ValueError, "unattributed dirty task paths"):
            reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)

    def test_removed_partial_edit_refuses_reopen(self) -> None:
        _, runtime, _, _, _ = self._start(worktrees=False)
        (self.workspace / "one.txt").write_text("base\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no longer exist in the dirty diff"):
            reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)

    def test_changed_frozen_source_refuses_reopen(self) -> None:
        _, runtime, _, _, _ = self._start(worktrees=False)
        source = Path(load_data(runtime.manifest_path)["sources"]["task_graph"]["path"])
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "visible orchestration sources changed"):
            reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)

    def test_cli_preview_requires_real_context_and_reopen_is_idempotent(self) -> None:
        run_id, runtime, _, _, _ = self._start(worktrees=False)
        with self.assertRaisesRegex(ValueError, "reviewed checkpoint"):
            cli_main(["reopen", run_id, "TASK-1", "--workspace", str(self.workspace),
                      "--reason", "Reviewed", "--partial-reviewed", "--dry-run"])
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_main(["reopen", run_id, "TASK-1", "--workspace", str(self.workspace),
                                       "--reason", "Reviewed", "--checkpoint", "Finish",
                                       "--partial-reviewed"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "reopened")
        repeated = reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish")
        self.assertEqual(repeated["status"], "already_reopened")

    def test_saved_record_reconciles_crash_before_state_transition(self) -> None:
        _, runtime, _, _, _ = self._start(worktrees=False)
        reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish", partial_reviewed=True)
        state = load_data(runtime.state_path)
        state["tasks"]["TASK-1"]["status"] = "failed"
        state["tasks"]["TASK-1"]["failure_code"] = "agent_implementation"
        runtime.write_state(state, load_data(runtime.root / "task-graph.yaml"))
        record = reopen_task(runtime, self.schemas, "TASK-1", "Reviewed", checkpoint="Finish")
        self.assertEqual(record["status"], "already_reopened")
        self.assertEqual(load_data(runtime.state_path)["tasks"]["TASK-1"]["status"], "ready")

    def test_only_referenced_resolved_decisions_reach_task(self) -> None:
        _, runtime, worker, _, _ = self._start(worktrees=False, decisions=True)
        task = load_data(runtime.root / "task-graph.yaml")["tasks"][1]
        self.assertEqual(task_decision_context(runtime.root, task), ["CLR-1: Use API A."])
        route = next(route for task_id, _, route in worker.calls if task_id == "TASK-1")
        self.assertEqual(route["resolved_decisions"], ["CLR-1: Use API A."])
        for provider in ("claude", "cursor"):
            prompt = render_task_prompt(
                ROOT / f".ai/orchestration/prompts/{provider}-task.md",
                ROOT / "skills/code-discipline/SKILL.md", task, route,
            )
            self.assertIn("CLR-1: Use API A.", prompt)
            self.assertNotIn("Do not pass this to TASK-1.", prompt)

    def test_decision_requires_authority_and_reopens_only_affected_branch(self) -> None:
        _, runtime, worker, engine, state = self._start(worktrees=False, failure="decision_required")
        self.assertEqual(state["tasks"]["TASK-2"]["status"], "completed")
        with self.assertRaisesRegex(ValueError, "decision resolution"):
            reopen_task(runtime, self.schemas, "TASK-1", "Spec supports A")
        reopen_task(runtime, self.schemas, "TASK-1", "Spec supports A", resolution="Use A",
                    authority="control_plane", evidence="Approved spec section 2")
        self.assertEqual(engine().run(resume=True)["status"], "completed")
        self.assertEqual(worker.calls[-2][2]["continuation"], "No edit yet.")
        self.assertEqual(worker.calls[-2][2]["decision_resolution"], "Use A")
        body = Path(generate_report(runtime.root)["report"]).read_text(encoding="utf-8")
        self.assertNotIn("Use A", body)
        self.assertNotIn("Approved spec section 2", body)

    def test_permission_pause_is_local_only_in_isolated_worktrees(self) -> None:
        for worktrees, expected in ((False, "ready"), (True, "completed")):
            with self.subTest(worktrees=worktrees):
                _, _, _, _, state = self._start(worktrees=worktrees, failure="permission_required")
                self.assertEqual(state["tasks"]["TASK-2"]["status"], expected)


if __name__ == "__main__":
    unittest.main()
