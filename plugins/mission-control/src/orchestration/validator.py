from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Set

from .failures import ValidationError


TASK_ID = re.compile(r"^TASK-[A-Z0-9][A-Z0-9-]*$")
DECISION_ID = re.compile(r"^CLR-[A-Z0-9][A-Z0-9-]*$")
TASK_TYPES = {
    "research", "analysis", "planning", "codebase_scan", "implementation", "business_logic",
    "codemod", "unit_testing", "integration_testing", "review", "verification",
    "visual_validation", "simulator", "orchestration",
}
LEVELS = {"low", "medium", "high"}
ISOLATIONS = {"workspace", "worktree", "read_only"}
TASK_STATUSES = {"pending", "ready", "running", "completed", "failed", "blocked", "review_required"}
FAILURE_CODES = {
    "agent_implementation", "tool", "timeout", "schema", "test", "scope", "dependency",
    "merge_conflict", "review", "configuration", "budget", "provider_unavailable", "permission_required", "decision_required",
}


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} must be an object")
    return value


def _list(value: Any, path: str, *, nonempty: bool = False) -> List[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    if nonempty and not value:
        raise ValidationError(f"{path} must not be empty")
    return value


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValidationError(f"{path} must be a non-empty string")
    return value


def _require(value: Mapping[str, Any], keys: Iterable[str], path: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValidationError(f"{path} is missing required fields: {', '.join(missing)}")


def validate_task(task: Any, path: str = "task") -> None:
    value = _mapping(task, path)
    _require(value, ["id", "title", "type", "objective", "scope", "acceptance_criteria", "depends_on", "capabilities", "complexity", "risk", "verification"], path)
    task_id = _string(value["id"], f"{path}.id")
    if not TASK_ID.match(task_id):
        raise ValidationError(f"{path}.id has invalid format: {task_id}")
    _string(value["title"], f"{path}.title")
    _string(value["objective"], f"{path}.objective")
    if value["type"] not in TASK_TYPES:
        raise ValidationError(f"{path}.type is unsupported: {value['type']}")
    if value["complexity"] not in LEVELS or value["risk"] not in LEVELS:
        raise ValidationError(f"{path}.complexity and risk must be low, medium, or high")
    scope = _mapping(value["scope"], f"{path}.scope")
    _require(scope, ["allowed", "forbidden"], f"{path}.scope")
    allowed = _list(scope["allowed"], f"{path}.scope.allowed", nonempty=True)
    forbidden = _list(scope["forbidden"], f"{path}.scope.forbidden")
    for index, item in enumerate(allowed + forbidden):
        _string(item, f"{path}.scope[{index}]")
    criteria = _list(value["acceptance_criteria"], f"{path}.acceptance_criteria", nonempty=True)
    for index, item in enumerate(criteria):
        _string(item, f"{path}.acceptance_criteria[{index}]")
    dependencies = _list(value["depends_on"], f"{path}.depends_on")
    if len(set(dependencies)) != len(dependencies):
        raise ValidationError(f"{path}.depends_on contains duplicates")
    for dependency in dependencies:
        if not isinstance(dependency, str) or not TASK_ID.match(dependency):
            raise ValidationError(f"{path}.depends_on contains invalid task ID")
    decisions = _list(value.get("decisions", []), f"{path}.decisions")
    if any(not isinstance(decision, str) or not DECISION_ID.match(decision) for decision in decisions):
        raise ValidationError(f"{path}.decisions contains an invalid CLR ID")
    if len(decisions) != len(set(decisions)):
        raise ValidationError(f"{path}.decisions contains duplicate or invalid CLR IDs")
    capabilities = _list(value["capabilities"], f"{path}.capabilities", nonempty=True)
    if not all(isinstance(item, str) and item for item in capabilities):
        raise ValidationError(f"{path}.capabilities must contain non-empty strings")
    commands = _list(value["verification"], f"{path}.verification")
    for index, command in enumerate(commands):
        parts = _list(command, f"{path}.verification[{index}]", nonempty=True)
        if not all(isinstance(part, str) and part for part in parts):
            raise ValidationError(f"{path}.verification[{index}] must be an argument array")


def validate_graph(graph: Any) -> None:
    value = _mapping(graph, "graph")
    _require(value, ["schema_version", "workflow", "parallelization", "tasks"], "graph")
    if value["schema_version"] != "1.0":
        raise ValidationError("graph.schema_version must be 1.0")
    workflow = _mapping(value["workflow"], "graph.workflow")
    _require(workflow, ["id", "objective"], "graph.workflow")
    _string(workflow["id"], "graph.workflow.id")
    _string(workflow["objective"], "graph.workflow.objective")
    parallel = _mapping(value["parallelization"], "graph.parallelization")
    _require(parallel, ["enabled", "max_workers", "require_non_overlapping_scope"], "graph.parallelization")
    if not isinstance(parallel["enabled"], bool) or not isinstance(parallel["require_non_overlapping_scope"], bool):
        raise ValidationError("graph.parallelization flags must be booleans")
    if not isinstance(parallel["max_workers"], int) or parallel["max_workers"] < 1:
        raise ValidationError("graph.parallelization.max_workers must be at least 1")
    tasks = _list(value["tasks"], "graph.tasks", nonempty=True)
    for index, task in enumerate(tasks):
        validate_task(task, f"graph.tasks[{index}]")
    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids):
        raise ValidationError("graph task IDs must be unique")
    known = set(ids)
    for task in tasks:
        unknown = set(task["depends_on"]) - known
        if unknown:
            raise ValidationError(f"{task['id']} depends on unknown tasks: {sorted(unknown)}")
        if task["id"] in task["depends_on"]:
            raise ValidationError(f"{task['id']} cannot depend on itself")
    _assert_acyclic(tasks)


def validate_decision_dependencies(graph: Any, index: Any, *, require_all_resolved: bool = False) -> None:
    """Gate known prerequisites without claiming to discover omitted decisions."""
    references = {decision for task in graph["tasks"] for decision in task.get("decisions", [])}
    if index is None:
        if references:
            raise ValidationError("task decision references require orchestration/decisions.yaml")
        return
    value = _mapping(index, "decisions")
    _require(value, ["schema_version", "decisions"], "decisions")
    if value["schema_version"] != "1.0":
        raise ValidationError("decisions.schema_version must be 1.0")
    entries = _list(value["decisions"], "decisions.decisions")
    by_id = {}
    for entry_value in entries:
        entry = _mapping(entry_value, "decisions.entry")
        _require(entry, ["id", "status", "summary", "authority"], "decisions.entry")
        decision_id = _string(entry["id"], "decisions.entry.id")
        if not DECISION_ID.match(decision_id) or decision_id in by_id:
            raise ValidationError(f"duplicate or invalid decision ID: {decision_id}")
        if entry["status"] not in {"resolved", "open", "scheduled"}:
            raise ValidationError(f"invalid decision status: {decision_id}")
        if entry["authority"] not in {"user", "control_plane", "repository_evidence"}:
            raise ValidationError(f"invalid decision authority: {decision_id}")
        _string(entry["summary"], f"decisions.{decision_id}.summary")
        if entry["status"] == "resolved":
            _string(entry.get("resolution"), f"decisions.{decision_id}.resolution")
        by_id[decision_id] = entry
    missing = references - set(by_id)
    if missing:
        raise ValidationError(f"tasks reference unknown decisions: {sorted(missing)}")
    checked = set(by_id) if require_all_resolved else references
    unresolved = sorted(decision for decision in checked if by_id[decision]["status"] != "resolved")
    if unresolved:
        message = "unresolved decisions block readiness" if require_all_resolved else "tasks depend on unresolved decisions"
        raise ValidationError(f"{message}: {unresolved}")


def validate_required_artifacts(change_dir: Path, required_artifacts: Any) -> None:
    """Require declared files without letting methodology paths escape the change directory."""
    root = change_dir.resolve()
    if not root.is_dir():
        raise ValidationError(f"change directory does not exist: {change_dir}")
    for name in _list(required_artifacts, "methodology.required_artifacts"):
        relative = Path(_string(name, "methodology.required_artifacts[]"))
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValidationError(f"required artifact must be a relative file under the change directory: {name}")
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValidationError(f"required artifact must not traverse a symlink: {name}")
        if not path.is_file():
            raise ValidationError(f"required artifact is missing or not a regular file: {name}")


def _assert_acyclic(tasks: List[Mapping[str, Any]]) -> None:
    dependencies = {task["id"]: set(task["depends_on"]) for task in tasks}
    remaining = set(dependencies)
    while remaining:
        ready = {task_id for task_id in remaining if not (dependencies[task_id] & remaining)}
        if not ready:
            raise ValidationError(f"task graph contains a cycle among: {sorted(remaining)}")
        remaining -= ready


def validate_profiles(config: Any, providers: Any = None) -> None:
    value = _mapping(config, "profiles")
    _require(value, ["schema_version", "budgets", "profiles"], "profiles")
    if value["schema_version"] != "1.0":
        raise ValidationError("profiles.schema_version must be 1.0")
    budgets = _mapping(value["budgets"], "profiles.budgets")
    _require(
        budgets,
        ["max_parallel_agents", "max_expensive_tasks", "max_attempts_per_task", "max_remediation_cycles"],
        "profiles.budgets",
    )
    for name in ("max_parallel_agents", "max_expensive_tasks", "max_attempts_per_task"):
        if not isinstance(budgets[name], int) or isinstance(budgets[name], bool) or budgets[name] < 1:
            raise ValidationError(f"profiles.budgets.{name} must be at least 1")
    if not isinstance(budgets["max_remediation_cycles"], int) or isinstance(budgets["max_remediation_cycles"], bool) or budgets["max_remediation_cycles"] < 0:
        raise ValidationError("profiles.budgets.max_remediation_cycles must be non-negative")
    profiles = _mapping(value["profiles"], "profiles.profiles")
    known_providers = _mapping(providers, "providers") if providers is not None else None
    if not profiles:
        raise ValidationError("profiles.profiles must not be empty")
    for name, profile_value in profiles.items():
        profile = _mapping(profile_value, f"profiles.{name}")
        _require(profile, ["executor", "isolation", "timeout_seconds", "expense", "max_attempts", "retry_on"], f"profiles.{name}")
        executor = _string(profile["executor"], f"profiles.{name}.executor")
        if profile["isolation"] not in ISOLATIONS:
            raise ValidationError(f"profile {name} has invalid isolation")
        if known_providers is not None:
            if executor not in known_providers:
                raise ValidationError(f"profile {name} references unknown provider {executor}")
            capabilities = known_providers[executor]["capabilities"]
            if profile.get("model") is not None and not capabilities["model_selection"]:
                raise ValidationError(f"profile {name} selects a model unsupported by {executor}")
            if profile.get("effort") is not None and not capabilities["effort_selection"]:
                raise ValidationError(f"profile {name} selects effort unsupported by {executor}")
            if profile["isolation"] == "read_only" and not capabilities["read_only"]:
                raise ValidationError(f"profile {name} requires read-only support from {executor}")
            if profile["isolation"] == "worktree" and not capabilities["file_editing"]:
                raise ValidationError(f"profile {name} requires file-editing support from {executor}")
        if not isinstance(profile["timeout_seconds"], int) or profile["timeout_seconds"] < 1:
            raise ValidationError(f"profile {name} has invalid timeout")
        if not isinstance(profile["max_attempts"], int) or profile["max_attempts"] < 1:
            raise ValidationError(f"profile {name} has invalid max_attempts")
        if profile.get("expense") not in {"free", "low", "standard", "expensive"}:
            raise ValidationError(f"profile {name} has invalid expense")
        if profile.get("effort") is not None:
            _string(profile["effort"], f"profiles.{name}.effort")
        backoff = profile.get("retry_backoff_seconds", 0)
        if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or backoff < 0:
            raise ValidationError(f"profile {name} has invalid retry_backoff_seconds")
        invalid = set(_list(profile["retry_on"], f"profiles.{name}.retry_on")) - FAILURE_CODES
        if invalid:
            raise ValidationError(f"profile {name} has invalid retry codes: {sorted(invalid)}")


def validate_delegation(
    graph: Any,
    delegation: Any,
    profiles: Any,
    providers: Any = None,
    allowed_executors: Any = None,
) -> None:
    validate_graph(graph)
    validate_profiles(profiles, providers)
    value = _mapping(delegation, "delegation")
    _require(value, ["schema_version", "workflow_id", "execution_plan"], "delegation")
    if value["schema_version"] != "1.0":
        raise ValidationError("delegation.schema_version must be 1.0")
    if value["workflow_id"] != graph["workflow"]["id"]:
        raise ValidationError("delegation.workflow_id does not match graph")
    plan = _mapping(value["execution_plan"], "delegation.execution_plan")
    graph_ids = {task["id"] for task in graph["tasks"]}
    if set(plan) != graph_ids:
        missing = sorted(graph_ids - set(plan))
        extra = sorted(set(plan) - graph_ids)
        raise ValidationError(f"delegation coverage mismatch; missing={missing}, extra={extra}")
    known_profiles = profiles["profiles"]
    for task_id, entry_value in plan.items():
        entry = _mapping(entry_value, f"delegation.execution_plan.{task_id}")
        _require(entry, ["profile", "executor", "isolation", "reason"], f"delegation.execution_plan.{task_id}")
        if entry["profile"] not in known_profiles:
            raise ValidationError(f"{task_id} references unknown profile {entry['profile']}")
        executor = _string(entry["executor"], f"delegation.execution_plan.{task_id}.executor")
        if entry["isolation"] not in ISOLATIONS:
            raise ValidationError(f"{task_id} has invalid isolation")
        if allowed_executors is not None and executor not in set(allowed_executors):
            raise ValidationError(f"{task_id} uses provider {executor} outside the selected strategy")
        selected_profile = known_profiles[entry["profile"]]
        if entry["executor"] != selected_profile["executor"]:
            raise ValidationError(f"{task_id} executor disagrees with profile")
        if entry["isolation"] != selected_profile["isolation"]:
            raise ValidationError(f"{task_id} isolation disagrees with profile")
        if "timeout_seconds" in entry and (
            not isinstance(entry["timeout_seconds"], int)
            or isinstance(entry["timeout_seconds"], bool)
            or entry["timeout_seconds"] < 1
        ):
            raise ValidationError(f"{task_id} has invalid timeout_seconds")
        if "max_attempts" in entry and (
            not isinstance(entry["max_attempts"], int)
            or isinstance(entry["max_attempts"], bool)
            or entry["max_attempts"] < 1
        ):
            raise ValidationError(f"{task_id} has invalid max_attempts")
        if "model" in entry and entry["model"] is not None and not isinstance(entry["model"], str):
            raise ValidationError(f"{task_id} has invalid model")
        if "effort" in entry and entry["effort"] is not None and not isinstance(entry["effort"], str):
            raise ValidationError(f"{task_id} has invalid effort")
        _string(entry["reason"], f"delegation.execution_plan.{task_id}.reason")
        fallbacks = _list(entry.get("fallbacks", []), f"delegation.execution_plan.{task_id}.fallbacks")
        fallback_names = []
        for index, fallback_value in enumerate(fallbacks):
            fallback = _mapping(fallback_value, f"{task_id}.fallbacks[{index}]")
            _require(fallback, ["profile", "reason"], f"{task_id}.fallbacks[{index}]")
            candidate = fallback["profile"]
            if candidate not in known_profiles or candidate == entry["profile"]:
                raise ValidationError(f"{task_id} has an unknown or identical fallback profile")
            candidate_profile = known_profiles[candidate]
            if candidate_profile["isolation"] != entry["isolation"]:
                raise ValidationError(f"{task_id} fallback {candidate} changes isolation")
            if allowed_executors is not None and candidate_profile["executor"] not in set(allowed_executors):
                raise ValidationError(f"{task_id} fallback {candidate} uses a provider outside the strategy")
            _string(fallback["reason"], f"{task_id}.fallbacks[{index}].reason")
            fallback_names.append(candidate)
        if len(set(fallback_names)) != len(fallback_names):
            raise ValidationError(f"{task_id} has duplicate fallback profiles")


def validate_result(result: Any) -> None:
    value = _mapping(result, "result")
    _require(value, ["schema_version", "task_id", "status", "summary", "changed_files", "verification", "warnings", "errors", "executor", "profile", "attempt", "duration_ms"], "result")
    if value["schema_version"] != "1.0" or value["status"] not in {"completed", "failed", "blocked", "review_required"}:
        raise ValidationError("result has invalid version or status")
    _string(value["executor"], "result.executor")
    if not isinstance(value["attempt"], int) or value["attempt"] < 1:
        raise ValidationError("result.attempt must be at least 1")
    if value.get("failure") is not None:
        failure = _mapping(value["failure"], "result.failure")
        _require(failure, ["code", "message", "retryable"], "result.failure")
        if failure["code"] not in FAILURE_CODES:
            raise ValidationError("result.failure.code is invalid")
        if failure["code"] == "permission_required":
            request = _mapping(value.get("permission_request"), "result.permission_request")
            _require(request, ["command", "reason", "checkpoint"], "result.permission_request")
            for field in ("command", "reason", "checkpoint"):
                _string(request[field], f"result.permission_request.{field}")
        if failure["code"] == "decision_required":
            request = _mapping(value.get("decision_request"), "result.decision_request")
            _require(request, ["question", "evidence", "checkpoint"], "result.decision_request")
            for field in ("question", "evidence", "checkpoint"):
                _string(request[field], f"result.decision_request.{field}")
    if "no_op_reason" in value:
        _string(value["no_op_reason"], "result.no_op_reason")


def validate_state(state: Any, graph: Any) -> None:
    value = _mapping(state, "state")
    _require(value, ["schema_version", "run_id", "workflow_id", "status", "created_at", "updated_at", "remediation_cycles", "review_status", "last_review_file", "tasks"], "state")
    if value["schema_version"] != "1.0" or value["workflow_id"] != graph["workflow"]["id"]:
        raise ValidationError("state version or workflow ID is invalid")
    if value["status"] not in {"pending", "running", "completed", "failed", "blocked", "review_required"}:
        raise ValidationError("state.status is invalid")
    if value["review_status"] not in {"pending", "passed", "remediation_required", "skipped", "failed"}:
        raise ValidationError("state.review_status is invalid")
    unavailable = _list(value.get("unavailable_providers", []), "state.unavailable_providers")
    if not all(isinstance(provider, str) and provider for provider in unavailable) or len(set(unavailable)) != len(unavailable):
        raise ValidationError("state.unavailable_providers must contain unique provider names")
    tasks = _mapping(value["tasks"], "state.tasks")
    graph_ids = {task["id"] for task in graph["tasks"]}
    if set(tasks) != graph_ids:
        raise ValidationError("state task IDs do not match graph")
    for task_id, task_state_value in tasks.items():
        task_state = _mapping(task_state_value, f"state.tasks.{task_id}")
        _require(task_state, ["status", "attempts", "updated_at"], f"state.tasks.{task_id}")
        if task_state["status"] not in TASK_STATUSES or not isinstance(task_state["attempts"], int):
            raise ValidationError(f"state for {task_id} is invalid")


def validate_review(review: Any) -> None:
    value = _mapping(review, "review")
    _require(value, ["schema_version", "status", "summary", "issues"], "review")
    if value["schema_version"] != "1.0" or value["status"] not in {"pass", "remediation_required"}:
        raise ValidationError("review version or status is invalid")
    issues = _list(value["issues"], "review.issues")
    if value["status"] == "pass" and issues:
        raise ValidationError("a passing review must not contain issues")
    if value["status"] == "remediation_required" and not issues:
        raise ValidationError("remediation_required must contain issues")
    for index, issue_value in enumerate(issues):
        issue = _mapping(issue_value, f"review.issues[{index}]")
        _require(issue, ["id", "severity", "affected_task", "description", "acceptance_criteria"], f"review.issues[{index}]")
        if issue["severity"] not in {"low", "medium", "high", "critical"}:
            raise ValidationError("review issue severity is invalid")
        _list(issue["acceptance_criteria"], f"review.issues[{index}].acceptance_criteria", nonempty=True)
