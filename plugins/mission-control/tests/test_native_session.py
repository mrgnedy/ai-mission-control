from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestration.configuration import load_configuration
from orchestration.continuation import reopen_task
from orchestration.failures import ValidationError
from orchestration.io import atomic_write_json, load_data
from orchestration.native import claim_task, finish_task, session_review, session_status, start_session
from orchestration.router import route_graph
from orchestration.runs import create_run_manifest, file_sha256
from orchestration.runtime import RuntimeStore


def task(name: str, path: str, *, reader: bool = False, profile: str = "") -> dict:
    return {"id": name, "title": name, "type": "analysis" if reader else "implementation",
            "objective": "Inspect" if reader else "Update", "scope": {"allowed": [path], "forbidden": []},
            "acceptance_criteria": ["Correct"], "depends_on": [],
            "capabilities": ["analysis" if reader else "implementation"], "complexity": "medium", "risk": "low",
            "verification": [], "requested_profile": profile or ("native-reader" if reader else "native-standard")}


class NativeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mission-control-native-test-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        for command in (["git", "init", "-q"],):
            subprocess.run(command, cwd=self.workspace, check=True, capture_output=True)
        for name in ("one.txt", "two.txt", "three.txt"):
            (self.workspace / name).write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base"],
                       cwd=self.workspace, check=True, capture_output=True)
        self.bundle = load_configuration(ROOT, ROOT / ".ai/orchestration/orchestration.yaml")
        self.bundle["orchestration"]["execution_strategy"] = "claude-native"
        self.bundle["orchestration"]["control_plane"].update(planning_profile="native-deep", review_profile="native-review", final_approval_profile="native-review")
        self.bundle["execution_strategy"] = load_data(ROOT / ".ai/orchestration/strategies/claude-native.yaml")
        self.bundle["routing_matrix"] = load_data(ROOT / ".ai/orchestration/routing-claude-native.yaml")

    def start(self, tasks: list[dict] | None = None, *, parallel: bool = True) -> RuntimeStore:
        graph = {"schema_version": "1.0", "workflow": {"id": "native-fixture", "objective": "Synthetic work"},
                 "parallelization": {"enabled": parallel, "max_workers": 3, "require_non_overlapping_scope": True},
                 "tasks": tasks or [task("TASK-1", "one.txt"), task("TASK-2", "two.txt")]}
        delegation = route_graph(graph, self.bundle["profiles"], self.bundle["routing_matrix"], ["claude"])
        run_id = str(uuid.uuid4())
        change = self.workspace / "specs" / run_id
        graph_path = change / "orchestration/task-graph.yaml"
        delegation_path = change / "orchestration/delegation.yaml"
        atomic_write_json(graph_path, graph)
        atomic_write_json(delegation_path, delegation)
        runtime = RuntimeStore(self.workspace / ".ai-runtime/native-fixture" / run_id)
        manifest = create_run_manifest(run_id, "native-fixture", self.workspace, change, graph_path, delegation_path,
                                       parallel=parallel, use_worktrees=False, review=True, provider_binaries={})
        start_session(ROOT, self.bundle, {"graph": graph_path, "delegation": delegation_path,
                                         "workspace": self.workspace, "runtime": runtime.root,
                                         "options": manifest["options"], "manifest": manifest})
        return runtime

    def finish(self, runtime: RuntimeStore, claim: dict, *, status: str = "completed", agent: str = "writer-1", **fields) -> dict:
        submission = {"claim_id": claim["claim_id"], "agent_id": agent, "status": status, "summary": "Terminal", **fields}
        return finish_task(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Agent returned terminal result")

    def complete_one(self) -> tuple[RuntimeStore, dict]:
        runtime = self.start([task("TASK-1", "one.txt")])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("complete\n", encoding="utf-8")
        self.finish(runtime, claim)
        return runtime, claim

    def test_claim_is_durable_and_duplicate_never_launches(self) -> None:
        runtime = self.start()
        first = claim_task(ROOT, runtime, "TASK-1")
        second = claim_task(ROOT, RuntimeStore(runtime.root), "TASK-1")
        self.assertTrue(first["launch_allowed"])
        self.assertFalse(second["launch_allowed"])
        self.assertEqual(first["claim_id"], second["claim_id"])
        self.assertEqual(load_data(runtime.state_path)["tasks"]["TASK-1"]["attempts"], 1)
        with self.assertRaisesRegex(ValueError, "writer"):
            claim_task(ROOT, runtime, "TASK-2")
        with self.assertRaisesRegex(ValueError, "worker-stopped"):
            finish_task(ROOT, runtime, {"claim_id": first["claim_id"], "agent_id": "actual", "status": "completed", "summary": "Done"})

    def test_completed_claim_is_idempotent_and_dependency_is_not_ready(self) -> None:
        runtime, claim = self.complete_one()
        duplicate = claim_task(ROOT, runtime, "TASK-1")
        self.assertEqual(duplicate["claim_id"], claim["claim_id"])
        self.assertFalse(duplicate["launch_allowed"])
        dependent = task("TASK-2", "two.txt")
        dependent["depends_on"] = ["TASK-1"]
        next_run = self.start([task("TASK-1", "one.txt"), dependent])
        with self.assertRaisesRegex(ValueError, "not ready"):
            claim_task(ROOT, next_run, "TASK-2")

    def test_frozen_source_drift_refuses_claim(self) -> None:
        runtime = self.start()
        source = Path(load_data(runtime.manifest_path)["sources"]["task_graph"]["path"])
        graph = load_data(source)
        graph["tasks"][0]["objective"] = "External amendment"
        atomic_write_json(source, graph)
        with self.assertRaisesRegex(ValueError, "sources changed"):
            claim_task(ROOT, runtime, "TASK-1")

    def test_permission_and_decision_failures_preserve_compact_requests(self) -> None:
        for code, name, request in (
            ("permission_required", "permission_request", {"command": "safe-command", "reason": "Need permission", "checkpoint": "No edits"}),
            ("decision_required", "decision_request", {"question": "Which?", "evidence": "Two choices", "checkpoint": "No edits"}),
        ):
            with self.subTest(code=code):
                runtime = self.start([task("TASK-1", "one.txt")])
                claim = claim_task(ROOT, runtime, "TASK-1")
                result = self.finish(runtime, claim, status="failed", failure={"code": code, "message": "Stopped", "retryable": False}, **{name: request})
                self.assertEqual(result["result"][name], request)

    def test_receipt_crash_replays_without_verification_and_result_is_immutable(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt")])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("complete\n", encoding="utf-8")
        submission = {"claim_id": claim["claim_id"], "agent_id": "actual", "status": "completed", "summary": "Done"}
        with patch.object(runtime, "write_state", side_effect=RuntimeError("simulated crash")):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                finish_task(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        with patch("orchestration.verification.collect_task_evidence", side_effect=AssertionError("must not rerun")):
            result = finish_task(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned again")
        self.assertEqual(result["status"], "already_finished")
        self.assertEqual(load_data(runtime.state_path)["tasks"]["TASK-1"]["status"], "completed")
        saved = runtime.results / "TASK-1-attempt-1.json"
        before = file_sha256(saved)
        with self.assertRaisesRegex(ValueError, "immutable"):
            finish_task(ROOT, runtime, dict(submission, summary="Different"), worker_stopped=True, stop_evidence="Returned")
        self.assertEqual(file_sha256(saved), before)

    def test_check_crash_requires_reconciliation_without_rerunning_commands(self) -> None:
        runtime = self.start()
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("partial\n", encoding="utf-8")
        with patch("orchestration.verification.collect_task_evidence", side_effect=RuntimeError("check interrupted")):
            with self.assertRaisesRegex(RuntimeError, "check interrupted"):
                self.finish(runtime, claim)
        self.assertEqual(session_status(runtime.root)["claims"][0]["status"], "validating")
        with self.assertRaisesRegex(ValueError, "outcome is unknown"):
            self.finish(runtime, claim)
        with self.assertRaises(ValueError):
            claim_task(ROOT, runtime, "TASK-2")
        submission = {"claim_id": claim["claim_id"], "agent_id": "writer-1", "status": "failed", "summary": "Interrupted checks inspected"}
        with patch("orchestration.verification.run_commands", side_effect=AssertionError("must not rerun")):
            result = finish_task(ROOT, runtime, submission, worker_stopped=True,
                                 stop_evidence="reconciled: worker stopped; command effects inspected")
        self.assertEqual(result["result"]["changed_files"], ["one.txt"])
        self.assertEqual(session_status(runtime.root)["partial_block"], ["TASK-1"])

    def test_partial_reopen_stays_exclusive_and_retained_files_are_verified(self) -> None:
        runtime = self.start()
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("retained correct edit\n", encoding="utf-8")
        self.finish(runtime, claim, status="failed")
        with self.assertRaisesRegex(ValueError, "partial"):
            claim_task(ROOT, runtime, "TASK-2")
        reopen_task(runtime, ROOT / ".ai/orchestration/schemas", "TASK-1", "Inspected", checkpoint="Retained edit correct; verify", partial_reviewed=True)
        self.assertEqual(session_status(runtime.root)["ready_tasks"], ["TASK-1"])
        with self.assertRaisesRegex(ValueError, "partial"):
            claim_task(ROOT, runtime, "TASK-2")
        second = claim_task(ROOT, runtime, "TASK-1")
        result = self.finish(runtime, second, agent="writer-2")
        self.assertEqual(result["result"]["changed_files"], ["one.txt"])
        self.assertEqual(result["result"]["status"], "completed")
        self.assertEqual(session_status(runtime.root)["ready_tasks"], ["TASK-2"])

    def test_workspace_exclusion_spans_native_runs(self) -> None:
        first = self.start()
        second = self.start()
        claim_task(ROOT, first, "TASK-1")
        with self.assertRaisesRegex(ValueError, "owned"):
            claim_task(ROOT, second, "TASK-1")
        with self.assertRaisesRegex(ValueError, "owned"):
            self.start()

    def test_readers_share_stable_workspace_but_obey_parallel_and_expense_budget(self) -> None:
        self.bundle["profiles"]["budgets"].update(max_parallel_agents=2, max_expensive_tasks=1)
        runtime = self.start([task("TASK-1", "one.txt", reader=True, profile="native-review"),
                              task("TASK-2", "two.txt", reader=True), task("TASK-3", "three.txt", reader=True, profile="native-review")])
        claim_task(ROOT, runtime, "TASK-1")
        self.assertEqual(session_status(runtime.root)["ready_tasks"], ["TASK-2"])
        with self.assertRaisesRegex(Exception, "expensive"):
            claim_task(ROOT, runtime, "TASK-3")
        claim_task(ROOT, runtime, "TASK-2")
        with self.assertRaisesRegex(ValueError, "budget"):
            claim_task(ROOT, runtime, "TASK-3")

    def test_readers_refuse_dirty_batch_and_disabled_graph_parallelism(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt", reader=True), task("TASK-2", "two.txt", reader=True)])
        claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "three.txt").write_text("external editor\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stable"):
            claim_task(ROOT, runtime, "TASK-2")

    def test_scope_and_command_mutations_are_observed_not_worker_claims(self) -> None:
        item = task("TASK-1", "one.txt")
        item["verification"] = [[sys.executable, "-c", "from pathlib import Path; Path('two.txt').write_text('bad')"]]
        runtime = self.start([item])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "untrusted"):
            self.finish(runtime, claim, changed_files=["one.txt"])
        result = self.finish(runtime, claim)
        self.assertEqual(result["result"]["failure"]["code"], "scope")
        self.assertEqual(result["result"]["changed_files"], ["one.txt", "two.txt"])
        self.assertEqual(result["result"]["verification"][0]["status"], "passed")

    def test_launch_failure_is_terminal_without_inventing_agent_identity(self) -> None:
        runtime = self.start()
        claim = claim_task(ROOT, runtime, "TASK-1")
        result = finish_task(ROOT, runtime, {"claim_id": claim["claim_id"], "status": "failed", "summary": "Launch rejected",
                                             "failure": {"code": "tool", "message": "Launch rejected", "retryable": False}},
                             worker_stopped=True, stop_evidence="Tool rejected launch before Agent creation")
        self.assertIsNone(result["result"]["provider_session_id"])
        self.assertEqual(session_status(runtime.root)["active_claims"], [])

    def test_fresh_review_and_parent_approval_are_required(self) -> None:
        runtime, _ = self.complete_one()
        self.assertEqual(session_status(runtime.root)["status"], "review_required")
        review_claim = session_review(ROOT, runtime)
        duplicate = session_review(ROOT, runtime)
        self.assertFalse(duplicate["launch_allowed"])
        review = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed", "issues": []}
        with self.assertRaisesRegex(ValueError, "fresh"):
            session_review(ROOT, runtime, {"claim_id": review_claim["claim_id"], "agent_id": "writer-1", "review": review},
                           worker_stopped=True, stop_evidence="Returned")
        outcome = session_review(ROOT, runtime, {"claim_id": review_claim["claim_id"], "agent_id": "reviewer-1", "review": review},
                                 worker_stopped=True, stop_evidence="Returned")
        self.assertEqual(outcome["status"], "approval_required")
        self.assertEqual(session_status(runtime.root)["status"], "review_required")
        approved = session_review(ROOT, runtime, approval="Parent inspected evidence and accepts")
        self.assertEqual(approved["status"], "completed")

    def test_workspace_drift_refuses_review_and_approval(self) -> None:
        runtime, _ = self.complete_one()
        review_claim = session_review(ROOT, runtime)
        review = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed", "issues": []}
        session_review(ROOT, runtime, {"claim_id": review_claim["claim_id"], "agent_id": "reviewer", "review": review},
                       worker_stopped=True, stop_evidence="Returned")
        (self.workspace / "two.txt").write_text("external edit\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "workspace changed"):
            session_review(ROOT, runtime, approval="Accepted")

    def test_failed_review_launch_closes_and_retry_budget_is_bounded(self) -> None:
        self.bundle["profiles"]["profiles"]["native-review"]["max_attempts"] = 2
        runtime, _ = self.complete_one()
        first = session_review(ROOT, runtime)
        submission = {"claim_id": first["claim_id"], "status": "failed", "summary": "Launch denied",
                      "failure": {"code": "tool", "message": "Launch denied", "retryable": False}}
        with patch.object(runtime, "write_state", side_effect=RuntimeError("review failure crash")):
            with self.assertRaisesRegex(RuntimeError, "review failure crash"):
                session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Agent was never created")
        outcome = session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Agent was never created")
        self.assertEqual(outcome["status"], "review_failed")
        self.assertEqual(session_status(runtime.root)["active_claims"], [])
        second = session_review(ROOT, runtime)
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(first["claim_id"], second["claim_id"])
        session_review(ROOT, runtime, dict(submission, claim_id=second["claim_id"]), worker_stopped=True, stop_evidence="Launch denied again")
        with self.assertRaisesRegex(ValueError, "review attempt budget"):
            session_review(ROOT, runtime)

    def test_failed_reader_and_reviewer_edits_remain_blocked(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt", reader=True), task("TASK-2", "two.txt")])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("unexpected reader edit\n", encoding="utf-8")
        result = self.finish(runtime, claim)
        self.assertEqual(result["result"]["failure"]["code"], "scope")
        with self.assertRaisesRegex(ValueError, "partial"):
            claim_task(ROOT, runtime, "TASK-2")

    def test_review_failure_mutation_refuses_new_review_until_reconciled(self) -> None:
        self.bundle["profiles"]["profiles"]["native-review"]["max_attempts"] = 2
        runtime, _ = self.complete_one()
        claim = session_review(ROOT, runtime)
        (self.workspace / "two.txt").write_text("reviewer edit\n", encoding="utf-8")
        session_review(ROOT, runtime, {"claim_id": claim["claim_id"], "agent_id": "reviewer", "status": "failed", "summary": "Unexpected write"},
                       worker_stopped=True, stop_evidence="Stopped after wrong write")
        with self.assertRaisesRegex(ValueError, "reconcile"):
            session_review(ROOT, runtime)
        (self.workspace / "two.txt").write_text("base\n", encoding="utf-8")
        self.assertTrue(session_review(ROOT, runtime)["launch_allowed"])

    def test_disabled_parallelism_refuses_second_reader(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt", reader=True), task("TASK-2", "two.txt", reader=True)], parallel=False)
        claim_task(ROOT, runtime, "TASK-1")
        self.assertEqual(session_status(runtime.root)["ready_tasks"], [])
        with self.assertRaisesRegex(ValueError, "budget"):
            claim_task(ROOT, runtime, "TASK-2")

    def test_parent_approval_receipt_survives_state_write_crash(self) -> None:
        runtime, _ = self.complete_one()
        claim = session_review(ROOT, runtime)
        review = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed", "issues": []}
        session_review(ROOT, runtime, {"claim_id": claim["claim_id"], "agent_id": "reviewer", "review": review},
                       worker_stopped=True, stop_evidence="Returned")
        with patch.object(runtime, "write_state", side_effect=RuntimeError("approval crash")):
            with self.assertRaisesRegex(RuntimeError, "approval crash"):
                session_review(ROOT, runtime, approval="Accepted actual evidence")
        approved = session_review(ROOT, runtime, approval="Accepted actual evidence")
        self.assertEqual(approved["status"], "completed")
        with self.assertRaisesRegex(ValueError, "immutable"):
            session_review(ROOT, runtime, approval="Different reason")

    def test_remediation_preserves_completed_siblings_and_replays_after_crash(self) -> None:
        runtime, _ = self.complete_one()
        sibling = file_sha256(runtime.results / "TASK-1-attempt-1.json")
        claim = session_review(ROOT, runtime)
        review = {"schema_version": "1.0", "status": "remediation_required", "summary": "Missing edge",
                  "issues": [{"id": "REVIEW-EDGE-1", "affected_task": "TASK-1", "severity": "medium", "description": "Fix edge case",
                              "acceptance_criteria": ["Edge covered"]}]}
        submission = {"claim_id": claim["claim_id"], "agent_id": "reviewer", "review": review}
        with patch.object(runtime, "write_state", side_effect=RuntimeError("remediation crash")):
            with self.assertRaisesRegex(RuntimeError, "remediation crash"):
                session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        outcome = session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        self.assertEqual(outcome["status"], "remediation_required")
        self.assertEqual(outcome["ready_tasks"], ["TASK-REMEDIATION-REVIEW-EDGE-1"])
        state = load_data(runtime.state_path)
        self.assertEqual(state["tasks"]["TASK-1"]["status"], "completed")
        self.assertEqual(state["remediation_cycles"], 1)
        self.assertEqual(file_sha256(runtime.results / "TASK-1-attempt-1.json"), sibling)

    def test_formal_review_schema_refuses_malformed_submission_and_receipt_replay(self) -> None:
        runtime, _ = self.complete_one()
        claim = session_review(ROOT, runtime)
        review = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed", "issues": []}
        for invalid in (dict(review, summary=None), dict(review, extra="injected"),
                        {"schema_version": "1.0", "status": "remediation_required", "summary": "Issue",
                         "issues": [{"id": "EDGE-1", "affected_task": "TASK-1", "severity": "medium",
                                     "description": "Fix", "acceptance_criteria": ["Fixed"]}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                session_review(ROOT, runtime, {"claim_id": claim["claim_id"], "agent_id": "reviewer", "review": invalid},
                               worker_stopped=True, stop_evidence="Returned")
        self.assertFalse((runtime.root / "native-receipts" / f"{claim['claim_id']}.json").exists())
        submission = {"claim_id": claim["claim_id"], "agent_id": "reviewer", "review": review}
        with patch.object(runtime, "write_state", side_effect=RuntimeError("review receipt crash")):
            with self.assertRaisesRegex(RuntimeError, "review receipt crash"):
                session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        receipt_path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
        receipt = load_data(receipt_path)
        receipt["submission"]["review"]["summary"] = None
        atomic_write_json(receipt_path, receipt)
        with self.assertRaises(ValidationError):
            session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        self.assertEqual(load_data(runtime.state_path)["review_status"], "pending")

    def test_second_review_cannot_reuse_prior_reviewer_after_remediation(self) -> None:
        runtime, _ = self.complete_one()
        claim = session_review(ROOT, runtime)
        review = {"schema_version": "1.0", "status": "remediation_required", "summary": "Fix edge",
                  "issues": [{"id": "REVIEW-EDGE-1", "affected_task": "TASK-1", "severity": "medium",
                              "description": "Fix edge", "acceptance_criteria": ["Fixed"]}]}
        submission = {"claim_id": claim["claim_id"], "agent_id": "reviewer-1", "review": review}
        session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")
        # Replaying the same receipt is idempotent even when work awaits remediation.
        self.assertEqual(session_review(ROOT, runtime, submission, worker_stopped=True, stop_evidence="Returned")["status"], "remediation_required")
        remediation = claim_task(ROOT, runtime, "TASK-REMEDIATION-REVIEW-EDGE-1")
        (self.workspace / "one.txt").write_text("remediated\n", encoding="utf-8")
        self.finish(runtime, remediation, agent="writer-2")
        second = session_review(ROOT, runtime)
        passing = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed fresh", "issues": []}
        with self.assertRaisesRegex(ValueError, "every other claim"):
            session_review(ROOT, runtime, {"claim_id": second["claim_id"], "agent_id": "reviewer-1", "review": passing},
                           worker_stopped=True, stop_evidence="Returned")
        fresh = {"claim_id": second["claim_id"], "agent_id": "reviewer-2", "review": passing}
        session_review(ROOT, runtime, fresh, worker_stopped=True, stop_evidence="Returned")
        self.assertEqual(session_review(ROOT, runtime, fresh, worker_stopped=True, stop_evidence="Returned")["status"], "approval_required")

    def test_reviewer_cannot_reuse_completed_reader_identity(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt", reader=True)])
        reader = claim_task(ROOT, runtime, "TASK-1")
        self.finish(runtime, reader, agent="prior-reader")
        reviewer = session_review(ROOT, runtime)
        passing = {"schema_version": "1.0", "status": "pass", "summary": "Reviewed", "issues": []}
        with self.assertRaisesRegex(ValueError, "every other claim"):
            session_review(ROOT, runtime, {"claim_id": reviewer["claim_id"], "agent_id": "prior-reader", "review": passing},
                           worker_stopped=True, stop_evidence="Returned")

    def test_formal_result_schema_refuses_malformed_receipt_replay(self) -> None:
        runtime = self.start([task("TASK-1", "one.txt")])
        claim = claim_task(ROOT, runtime, "TASK-1")
        (self.workspace / "one.txt").write_text("done\n", encoding="utf-8")
        with patch.object(runtime, "write_state", side_effect=RuntimeError("result receipt crash")):
            with self.assertRaisesRegex(RuntimeError, "result receipt crash"):
                self.finish(runtime, claim)
        receipt_path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
        receipt = load_data(receipt_path)
        receipt["result"]["summary"] = None
        atomic_write_json(receipt_path, receipt)
        with self.assertRaises(ValidationError):
            self.finish(runtime, claim)
        self.assertEqual(load_data(runtime.state_path)["tasks"]["TASK-1"]["status"], "running")

    def test_receipt_identity_tampering_cannot_write_result_or_complete_wrong_task(self) -> None:
        mutations = (
            ("result", "task_id", "TASK-2"), ("result", "attempt", 2),
            ("result", "executor", "cursor"), ("result", "profile", "native-deep"),
            ("result", "provider_session_id", "another-agent"),
            (None, "run_id", "another-run"), (None, "claim_id", "another-claim"),
            ("submission", "claim_id", "another-claim"),
        )
        for section, key, value in mutations:
            with self.subTest(section=section, key=key):
                runtime = self.start()
                claim = claim_task(ROOT, runtime, "TASK-1")
                (self.workspace / "one.txt").write_text("done\n", encoding="utf-8")
                with patch.object(runtime, "write_result", side_effect=RuntimeError("receipt committed")):
                    with self.assertRaisesRegex(RuntimeError, "receipt committed"):
                        self.finish(runtime, claim)
                receipt_path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
                original = load_data(receipt_path)
                mutated = load_data(receipt_path)
                (mutated[section] if section else mutated)[key] = value
                atomic_write_json(receipt_path, mutated)
                with self.assertRaisesRegex(ValueError, "identity disagrees"):
                    self.finish(runtime, claim)
                self.assertEqual(list(runtime.results.glob("*.json")), [])
                self.assertEqual(load_data(runtime.state_path)["tasks"]["TASK-1"]["status"], "running")
                # Restore the trusted receipt and close this claim before the next isolated case.
                atomic_write_json(receipt_path, original)
                self.finish(runtime, claim)

    def test_runtime_configuration_drift_and_attempt_budget_refuse(self) -> None:
        self.bundle["profiles"]["budgets"]["max_attempts_per_task"] = 1
        runtime = self.start()
        claim = claim_task(ROOT, runtime, "TASK-1")
        self.finish(runtime, claim, status="failed")
        reopen_task(runtime, ROOT / ".ai/orchestration/schemas", "TASK-1", "Inspected", checkpoint="Retry launch")
        with self.assertRaisesRegex(ValueError, "attempt budget"):
            claim_task(ROOT, runtime, "TASK-1")
        config = load_data(runtime.configuration_path)
        config["profiles"]["budgets"]["max_parallel_agents"] = 99
        atomic_write_json(runtime.configuration_path, config)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            claim_task(ROOT, runtime, "TASK-2")


if __name__ == "__main__":
    unittest.main()
