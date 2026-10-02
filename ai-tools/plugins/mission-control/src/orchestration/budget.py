from __future__ import annotations

from typing import Any, Iterable, Mapping

from .failures import FailureCode, WorkflowError


def effective_parallel_limit(graph: Mapping[str, Any], profiles: Mapping[str, Any]) -> int:
    return min(graph["parallelization"]["max_workers"], profiles["budgets"]["max_parallel_agents"])


def enforce_expensive_budget(task_ids: Iterable[str], delegation: Mapping[str, Any], profiles: Mapping[str, Any]) -> None:
    expensive = sum(
        1
        for task_id in task_ids
        if profiles["profiles"][delegation["execution_plan"][task_id]["profile"]]["expense"] == "expensive"
    )
    maximum = profiles["budgets"]["max_expensive_tasks"]
    if expensive > maximum:
        raise WorkflowError(FailureCode.BUDGET, f"batch has {expensive} expensive tasks; limit is {maximum}")
