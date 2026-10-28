from __future__ import annotations

import fnmatch
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set


def task_map(graph: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {task["id"]: task for task in graph["tasks"]}


def ready_tasks(graph: Mapping[str, Any], state: Mapping[str, Any]) -> List[str]:
    tasks = task_map(graph)
    ready: List[str] = []
    for task_id, task in tasks.items():
        task_state = state["tasks"][task_id]["status"]
        if task_state not in {"pending", "ready"}:
            continue
        if all(state["tasks"][dependency]["status"] == "completed" for dependency in task["depends_on"]):
            ready.append(task_id)
    return sorted(ready)


def descendants(graph: Mapping[str, Any], roots: Iterable[str]) -> Set[str]:
    children: Dict[str, Set[str]] = {task["id"]: set() for task in graph["tasks"]}
    for task in graph["tasks"]:
        for dependency in task["depends_on"]:
            children[dependency].add(task["id"])
    found: Set[str] = set()
    frontier = list(roots)
    while frontier:
        current = frontier.pop()
        for child in children.get(current, set()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def scopes_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    for left_pattern in left:
        for right_pattern in right:
            if _patterns_may_overlap(left_pattern, right_pattern):
                return True
    return False


def _patterns_may_overlap(left: str, right: str) -> bool:
    left_prefix = _literal_prefix(left)
    right_prefix = _literal_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    if left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix):
        return True
    return fnmatch.fnmatch(left_prefix, right) or fnmatch.fnmatch(right_prefix, left)


def _literal_prefix(pattern: str) -> str:
    wildcard_indexes = [index for index in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if index >= 0]
    stop = min(wildcard_indexes) if wildcard_indexes else len(pattern)
    return pattern[:stop].rstrip("/")


def safe_parallel_batch(
    graph: Mapping[str, Any],
    state: Mapping[str, Any],
    delegation: Mapping[str, Any],
    limit: int,
    parallel_executors: Any = None,
) -> List[str]:
    tasks = task_map(graph)
    selected: List[str] = []
    parallel = set(["cursor"] if parallel_executors is None else parallel_executors)
    for task_id in ready_tasks(graph, state):
        entry = delegation["execution_plan"][task_id]
        if entry["isolation"] != "worktree" or entry["executor"] not in parallel:
            if not selected:
                return [task_id]
            continue
        candidate_scope = tasks[task_id]["scope"]["allowed"]
        if any(scopes_overlap(candidate_scope, tasks[other]["scope"]["allowed"]) for other in selected):
            continue
        selected.append(task_id)
        if len(selected) >= limit:
            break
    return selected
