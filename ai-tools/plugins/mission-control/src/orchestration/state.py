from __future__ import annotations

import uuid
from typing import Any, Dict, Mapping

from .graph import descendants, ready_tasks
from .io import utc_now


ALLOWED_TRANSITIONS = {
    "pending": {"ready", "blocked"},
    "ready": {"running", "blocked"},
    "running": {"completed", "failed", "review_required", "ready"},
    "failed": {"ready", "blocked"},
    "review_required": {"ready", "completed", "failed"},
    "blocked": {"ready"},
    "completed": set(),
}


def create_state(graph: Mapping[str, Any], run_id: str = "") -> Dict[str, Any]:
    now = utc_now()
    state: Dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id or str(uuid.uuid4()),
        "workflow_id": graph["workflow"]["id"],
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "remediation_cycles": 0,
        "review_status": "pending",
        "last_review_file": None,
        "unavailable_providers": [],
        "tasks": {},
    }
    for task in graph["tasks"]:
        state["tasks"][task["id"]] = {
            "status": "pending",
            "attempts": 0,
            "updated_at": now,
            "result_file": None,
            "failure_code": None,
            "blocked_by": [],
        }
    refresh(state, graph)
    return state


def transition(
    state: Dict[str, Any],
    task_id: str,
    new_status: str,
    *,
    result_file: str = "",
    failure_code: str = "",
) -> None:
    task_state = state["tasks"][task_id]
    old_status = task_state["status"]
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ValueError(f"illegal task transition {task_id}: {old_status} -> {new_status}")
    task_state["status"] = new_status
    task_state["updated_at"] = utc_now()
    if new_status == "running":
        task_state["attempts"] += 1
    if result_file:
        task_state["result_file"] = result_file
    if failure_code:
        task_state["failure_code"] = failure_code
    state["updated_at"] = utc_now()


def refresh(state: Dict[str, Any], graph: Mapping[str, Any]) -> None:
    failed = [task_id for task_id, item in state["tasks"].items() if item["status"] == "failed"]
    blocked = descendants(graph, failed)
    for task_id in blocked:
        item = state["tasks"][task_id]
        causes = sorted(dependency for dependency in failed if task_id in descendants(graph, [dependency]))
        if item["status"] in {"pending", "ready", "blocked"} and (
            item["status"] != "blocked" or item.get("blocked_by") != causes
        ):
            item["status"] = "blocked"
            item["blocked_by"] = causes
            item["updated_at"] = utc_now()
    for task_id, item in state["tasks"].items():
        if item["status"] == "blocked" and item.get("blocked_by") and task_id not in blocked:
            item["status"] = "pending"
            item["blocked_by"] = []
            item["updated_at"] = utc_now()
    for task_id in ready_tasks(graph, state):
        item = state["tasks"][task_id]
        if item["status"] == "pending":
            item["status"] = "ready"
            item["blocked_by"] = []
            item["updated_at"] = utc_now()
    statuses = {item["status"] for item in state["tasks"].values()}
    if statuses == {"completed"}:
        state["status"] = "completed"
    elif "review_required" in statuses:
        state["status"] = "review_required"
    elif "running" in statuses or "ready" in statuses:
        state["status"] = "running"
    elif "failed" in statuses:
        state["status"] = "failed"
    elif "blocked" in statuses:
        state["status"] = "blocked"
    else:
        state["status"] = "pending"
    state["updated_at"] = utc_now()


def recover_stale_running(state: Dict[str, Any]) -> None:
    for item in state["tasks"].values():
        previous = item["status"]
        if previous in {"running", "review_required"}:
            item["status"] = "ready"
            if previous == "running" and item["failure_code"] is None:
                item["failure_code"] = "tool"
            item["updated_at"] = utc_now()
    state["updated_at"] = utc_now()


def extend_for_graph(state: Dict[str, Any], graph: Mapping[str, Any]) -> None:
    now = utc_now()
    graph_ids = {task["id"] for task in graph["tasks"]}
    stale = set(state["tasks"]) - graph_ids
    if stale:
        raise ValueError(f"state contains tasks not present in graph: {sorted(stale)}")
    for task_id in sorted(graph_ids - set(state["tasks"])):
        state["tasks"][task_id] = {
            "status": "pending",
            "attempts": 0,
            "updated_at": now,
            "result_file": None,
            "failure_code": None,
            "blocked_by": [],
        }
    state["updated_at"] = now
    refresh(state, graph)
