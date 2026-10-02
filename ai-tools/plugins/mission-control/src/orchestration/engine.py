from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
import time

from .budget import effective_parallel_limit, enforce_expensive_budget
from .continuation import task_continuation_context, task_decision_context
from .failures import FailureCode, WorkflowError
from .graph import ready_tasks, safe_parallel_batch, task_map
from .json_schema import validate_with_schema
from .models import task_result
from .permissions import task_permission_context
from .review import ReviewRunner, apply_review, load_results
from .retry import retry_delay_seconds, should_retry
from .runtime import RuntimeStore
from .scope import git_changed_files, git_head, validate_scope
from .state import create_state, extend_for_graph, recover_stale_running, refresh, transition
from .validator import validate_delegation, validate_result
from .verification import run_commands, verification_passed
from .worktree import WorktreeManager


class WorkflowEngine:
    def __init__(
        self,
        root: Path,
        graph: Dict[str, Any],
        delegation: Dict[str, Any],
        profiles: Dict[str, Any],
        workspace: Path,
        runtime: RuntimeStore,
        executors: Mapping[str, Any],
        *,
        parallel: bool = False,
        use_worktrees: bool = True,
        reviewer: Optional[ReviewRunner] = None,
        routing_matrix: Optional[Dict[str, Any]] = None,
        allowed_executors: Optional[list] = None,
        parallel_executors: Optional[list] = None,
        configuration_snapshot: Optional[Dict[str, Any]] = None,
        review_provider: str = "claude",
        review_profile: str = "claude-review",
    ) -> None:
        schemas = root / ".ai/orchestration/schemas"
        validate_with_schema(graph, schemas / "task-graph.schema.json")
        validate_with_schema(delegation, schemas / "delegation.schema.json")
        validate_delegation(graph, delegation, profiles, allowed_executors=allowed_executors)
        self.root = root
        self.graph = graph
        self.delegation = delegation
        self.profiles = profiles
        self.workspace = workspace
        self.runtime = runtime
        self.executors = executors
        self.parallel = parallel
        self.use_worktrees = use_worktrees
        self.reviewer = reviewer
        self.routing_matrix = routing_matrix
        self.allowed_executors = allowed_executors
        self.parallel_executors = parallel_executors
        self.configuration_snapshot = configuration_snapshot
        self.review_provider = review_provider
        self.review_profile = review_profile
        self.tasks = task_map(graph)
        self.worktrees: Optional[WorktreeManager] = None

    def run(self, *, resume: bool = False, run_id: str = "") -> Dict[str, Any]:
        with self.runtime:
            if resume and self.runtime.state_path.exists():
                state = self.runtime.load_state(self.graph, extend=True)
                recover_stale_running(state)
            else:
                state = create_state(self.graph, run_id)
            events = self.runtime.event_log(state["run_id"])
            if self.configuration_snapshot is not None and not resume:
                self.runtime.write_configuration(self.configuration_snapshot)
            if not resume:
                self.runtime.write_inputs(self.graph, self.delegation)
            if self.use_worktrees and _is_git_repository(self.workspace):
                self.worktrees = WorktreeManager(self.workspace, self.runtime.root, state["run_id"])
            self.runtime.write_state(state, self.graph)
            events.emit("workflow_started" if not resume else "workflow_resumed", status=state["status"])
            while True:
                refresh(state, self.graph)
                self.runtime.write_state(state, self.graph)
                unavailable = set(state.get("unavailable_providers", []))
                if unavailable and any(
                    self.delegation["execution_plan"][task_id]["executor"] in unavailable
                    for task_id in ready_tasks(self.graph, state)
                ):
                    state["status"] = "failed"
                    self.runtime.write_state(state, self.graph)
                    events.emit("workflow_finished", status="failed", reason="provider_unavailable")
                    return state
                if self._global_permission_pause(state):
                    state["status"] = "failed"
                    self.runtime.write_state(state, self.graph)
                    events.emit("workflow_finished", status="failed", reason="permission_required")
                    return state
                if state["status"] == "completed" and not ready_tasks(self.graph, state):
                    if self.reviewer is None:
                        if state["review_status"] == "pending":
                            state["review_status"] = "skipped"
                            self.runtime.write_state(state, self.graph)
                        events.emit("workflow_finished", status=state["status"], review_status=state["review_status"])
                        return state
                    review_outcome = self._review_completed_workflow(state, events)
                    if review_outcome in {"passed", "failed"}:
                        events.emit("workflow_finished", status=state["status"], review_status=state["review_status"])
                        return state
                    continue
                if state["status"] in {"failed", "blocked", "review_required"} and not ready_tasks(self.graph, state):
                    events.emit("workflow_finished", status=state["status"])
                    return state
                batch = self._select_batch(state)
                if not batch:
                    state["status"] = "blocked"
                    self.runtime.write_state(state, self.graph)
                    events.emit("workflow_finished", status="blocked", reason="no runnable tasks")
                    return state
                enforce_expensive_budget(batch, self.delegation, self.profiles)
                for task_id in batch:
                    transition(state, task_id, "running")
                    events.emit("task_started", task_id=task_id, attempt=state["tasks"][task_id]["attempts"], **self._event_route(task_id))
                self.runtime.write_state(state, self.graph)
                results = self._execute_batch(batch, state)
                for task_id in sorted(results):
                    self._record_result(task_id, results[task_id], state, events)
                refresh(state, self.graph)
                if self._global_permission_pause(state):
                    state["status"] = "failed"
                    self.runtime.write_state(state, self.graph)
                    events.emit("workflow_finished", status="failed", reason="permission_required")
                    return state
                if any(
                    route["executor"] in set(state.get("unavailable_providers", []))
                    and state["tasks"][task_id]["status"] != "completed"
                    for task_id, route in self.delegation["execution_plan"].items()
                ):
                    state["status"] = "failed"
                    self.runtime.write_state(state, self.graph)
                    events.emit("workflow_finished", status="failed", reason="provider_unavailable")
                    return state

    def _review_completed_workflow(self, state: Dict[str, Any], events: Any) -> str:
        if state["review_status"] == "passed":
            return "passed"
        review_cycle = state["remediation_cycles"] + 1
        review_workspace = self.workspace
        if self.worktrees is not None:
            review_workspace = self.worktrees.ensure_integration()
        events.emit(
            "review_started",
            cycle=review_cycle,
            executor=self.review_provider,
            profile=self.review_profile,
        )
        try:
            review = self.reviewer.review(
                self.graph,
                load_results(self.runtime.results),
                review_workspace,
                review_cycle,
            )
            review_path = self.runtime.write_review(review, review_cycle)
            state["last_review_file"] = str(review_path)
            if review["status"] == "pass":
                state["review_status"] = "passed"
                self.runtime.write_state(state, self.graph)
                events.emit("review_finished", cycle=review_cycle, status="pass", issues=0)
                return "passed"
            if self.routing_matrix is None:
                raise WorkflowError(
                    FailureCode.CONFIGURATION,
                    "a routing matrix is required to create remediation tasks",
                    False,
                )
            updated_graph, updated_delegation = apply_review(
                self.graph,
                review,
                self.profiles,
                self.routing_matrix,
                state["remediation_cycles"],
                self.allowed_executors,
            )
            state["remediation_cycles"] += 1
            state["review_status"] = "remediation_required"
            self.graph = updated_graph
            self.delegation = updated_delegation
            self.tasks = task_map(updated_graph)
            extend_for_graph(state, updated_graph)
            self.runtime.write_inputs(updated_graph, updated_delegation)
            self.runtime.write_state(state, updated_graph)
            events.emit(
                "review_finished",
                cycle=review_cycle,
                status="remediation_required",
                issues=len(review["issues"]),
            )
            return "remediation"
        except (WorkflowError, ValueError) as error:
            if not isinstance(error, WorkflowError):
                error = WorkflowError(FailureCode.REVIEW, str(error), False)
            state["status"] = "failed"
            state["review_status"] = "failed"
            self.runtime.write_state(state, self.graph)
            events.emit("review_failed", cycle=review_cycle, failure_code=error.code.value, error=error.message)
            return "failed"

    def _select_batch(self, state: Mapping[str, Any]) -> list:
        if self.parallel and self.graph["parallelization"]["enabled"]:
            limit = effective_parallel_limit(self.graph, self.profiles)
            candidates = safe_parallel_batch(
                self.graph, state, self.delegation, limit, self.parallel_executors
            )
            maximum_expensive = self.profiles["budgets"]["max_expensive_tasks"]
            selected = []
            expensive = 0
            for task_id in candidates:
                route = self.delegation["execution_plan"][task_id]
                profile = self.profiles["profiles"][route["profile"]]
                if profile["expense"] == "expensive":
                    if expensive >= maximum_expensive:
                        continue
                    expensive += 1
                selected.append(task_id)
            return selected
        ready = ready_tasks(self.graph, state)
        return ready[:1]

    def _global_permission_pause(self, state: Mapping[str, Any]) -> bool:
        for task_id, item in state["tasks"].items():
            if item["status"] != "failed" or item.get("failure_code") != FailureCode.PERMISSION_REQUIRED.value:
                continue
            route = self.delegation["execution_plan"][task_id]
            if self.worktrees is None or route["isolation"] != "worktree":
                return True
        return False

    def _execute_batch(self, batch: list, state: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        if len(batch) == 1:
            task_id = batch[0]
            try:
                result = self._execute_one(task_id, state)
            except Exception as error:
                result = self._uncaught_executor_result(task_id, state, error)
            return {task_id: result}
        if self.worktrees is not None:
            for task_id in batch:
                route = self.delegation["execution_plan"][task_id]
                if route["isolation"] == "worktree":
                    self.worktrees.prepare_task(task_id)
        results: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {
                pool.submit(self._execute_one, task_id, state, integrate_worktree=False): task_id
                for task_id in batch
            }
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    results[task_id] = future.result()
                except Exception as error:
                    results[task_id] = self._uncaught_executor_result(task_id, state, error)
        # Git operations against the shared integration branch must be serialized.
        # Provider execution remains parallel in each task's isolated worktree.
        for task_id in batch:
            result = results[task_id]
            if result["status"] == "completed":
                self._integrate_worktree_result(task_id, result)
                validate_result(result)
        return results

    def _uncaught_executor_result(
        self,
        task_id: str,
        state: Mapping[str, Any],
        error: Exception,
    ) -> Dict[str, Any]:
        route = self.delegation["execution_plan"][task_id]
        attempt = state["tasks"][task_id]["attempts"]
        failure = error if isinstance(error, WorkflowError) else WorkflowError(
            FailureCode.TOOL, str(error), False
        )
        return task_result(
            task_id,
            "failed",
            failure.message,
            route["executor"],
            route["profile"],
            attempt,
            0,
            errors=[failure.message],
            failure=failure.as_dict(),
            model=route.get("model"),
        )

    def _execute_one(
        self,
        task_id: str,
        state: Mapping[str, Any],
        *,
        integrate_worktree: bool = True,
    ) -> Dict[str, Any]:
        task = self.tasks[task_id]
        route = self.delegation["execution_plan"][task_id]
        profile = self.profiles["profiles"][route["profile"]]
        effective_route = dict(route)
        effective_route.setdefault("timeout_seconds", profile["timeout_seconds"])
        effective_route.setdefault("max_attempts", profile["max_attempts"])
        effective_route.setdefault("model", profile.get("model"))
        attempt = state["tasks"][task_id]["attempts"]
        workspace = self.worktrees.ensure_integration() if self.worktrees is not None else self.workspace
        carried_changes = []
        if route["isolation"] == "worktree" and self.worktrees is not None:
            workspace = self.worktrees.prepare_task(task_id)
            carried_changes = git_changed_files(workspace)
            validate_scope(carried_changes, task["scope"]["allowed"], task["scope"]["forbidden"])
        effective_route.update(task_continuation_context(
            self.runtime.root, state["run_id"], task_id, attempt, workspace,
        ))
        decisions = task_decision_context(self.runtime.root, task)
        if decisions:
            effective_route["resolved_decisions"] = decisions
        if route["executor"] == "claude":
            effective_route.update(task_permission_context(
                self.runtime.root, state["run_id"], task_id, attempt, workspace,
            ))
        try:
            execution_head = git_head(workspace)
        except WorkflowError:
            execution_head = None
        executor = self.executors[route["executor"]]
        result = executor.execute(task, effective_route, profile, workspace, attempt)
        validate_result(result)
        if carried_changes:
            result["changed_files"] = sorted(set(result["changed_files"]) | set(carried_changes))
        if (
            result["status"] == "completed"
            and route["executor"] != "local"
            and route["isolation"] != "read_only"
            and task["type"] in {"implementation", "business_logic", "codemod", "unit_testing", "integration_testing"}
            and not result["changed_files"]
            and not result.get("no_op_reason")
        ):
            result["status"] = "failed"
            result["summary"] = "writing task reported completion without edits or a no-op reason"
            result["errors"].append(result["summary"])
            result["failure"] = {
                "code": FailureCode.AGENT_IMPLEMENTATION.value,
                "message": result["summary"],
                "retryable": False,
            }
        if result["status"] == "completed" and task["verification"] and not result["verification"]:
            records = run_commands(task["verification"], workspace, effective_route["timeout_seconds"])
            result["verification"] = records
            if not verification_passed(records):
                result["status"] = "failed"
                result["summary"] = "Post-execution verification failed."
                result["errors"].append("post-execution verification failed")
                result["failure"] = {
                    "code": FailureCode.TEST.value,
                    "message": "post-execution verification failed",
                    "retryable": False,
                }
        if execution_head is not None and git_head(workspace) != execution_head:
            error = WorkflowError(
                FailureCode.SCOPE,
                "task execution changed Git HEAD; task-created commits are prohibited",
                False,
            )
            result["status"] = "failed"
            result["summary"] = error.message
            result["errors"].append(error.message)
            result["failure"] = error.as_dict()
        if integrate_worktree and result["status"] == "completed":
            self._integrate_worktree_result(task_id, result)
        validate_result(result)
        return result

    def _integrate_worktree_result(self, task_id: str, result: Dict[str, Any]) -> None:
        route = self.delegation["execution_plan"][task_id]
        if route["isolation"] != "worktree" or self.worktrees is None:
            return
        try:
            commit = self.worktrees.commit_task(task_id)
            integration = self.worktrees.integrate(commit)
            result["warnings"].append(f"integrated in manager worktree: {integration}")
            self.worktrees.cleanup_task(task_id)
        except WorkflowError as error:
            result["status"] = "failed"
            result["summary"] = error.message
            result["errors"].append(error.message)
            result["failure"] = error.as_dict()

    def _record_result(self, task_id: str, result: Dict[str, Any], state: Dict[str, Any], events: Any) -> None:
        path = self.runtime.write_result(result)
        route = self.delegation["execution_plan"][task_id]
        profile = self.profiles["profiles"][route["profile"]]
        if result["status"] == "completed":
            transition(state, task_id, "completed", result_file=str(path))
        elif result["status"] == "review_required":
            transition(state, task_id, "review_required", result_file=str(path), failure_code="review")
        else:
            failure_code = (result.get("failure") or {}).get("code", "agent_implementation")
            transition(state, task_id, "failed", result_file=str(path), failure_code=failure_code)
            if failure_code == FailureCode.PROVIDER_UNAVAILABLE.value:
                unavailable = set(state.setdefault("unavailable_providers", []))
                unavailable.add(route["executor"])
                state["unavailable_providers"] = sorted(unavailable)
            if should_retry(
                result,
                profile,
                state["tasks"][task_id]["attempts"],
                route,
                self.profiles["budgets"],
            ):
                delay = retry_delay_seconds(profile, state["tasks"][task_id]["attempts"])
                transition(state, task_id, "ready")
                events.emit(
                    "task_retry_scheduled",
                    task_id=task_id,
                    attempt=state["tasks"][task_id]["attempts"],
                    retry_reason=failure_code,
                    delay_seconds=delay,
                )
                if delay:
                    time.sleep(delay)
        self.runtime.write_state(state, self.graph)
        events.emit(
            "task_finished",
            task_id=task_id,
            status=result["status"],
            duration_ms=result["duration_ms"],
            attempt=result["attempt"],
            verification=[item["status"] for item in result["verification"]],
            **self._event_route(task_id),
        )

    def _event_route(self, task_id: str) -> Dict[str, Any]:
        route = self.delegation["execution_plan"][task_id]
        return {
            "executor": route["executor"],
            "profile": route["profile"],
            "model": route.get("model"),
            "effort": route.get("effort"),
        }


def _is_git_repository(path: Path) -> bool:
    import subprocess

    process = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(path), capture_output=True, text=True, check=False)
    return process.returncode == 0 and process.stdout.strip() == "true"
