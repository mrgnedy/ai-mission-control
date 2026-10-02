from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Protocol, Sequence, Tuple

from .failures import FailureCode, WorkflowError
from .executors.base import run_process
from .io import load_data
from .router import route_graph
from .scope import changed_snapshots, git_head, git_snapshot
from .validator import validate_graph, validate_review


class ReviewRunner(Protocol):
    def review(
        self,
        graph: Mapping[str, Any],
        results: Sequence[Mapping[str, Any]],
        workspace: Path,
        cycle: int,
    ) -> Dict[str, Any]: ...


class ClaudeReviewRunner:
    def __init__(
        self,
        root: Path,
        binary: str = "claude",
        model: str = "opus",
        effort: str = "",
        timeout_seconds: int = 2400,
    ) -> None:
        self.root = root
        self.binary = binary
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds

    def review(
        self,
        graph: Mapping[str, Any],
        results: Sequence[Mapping[str, Any]],
        workspace: Path,
        cycle: int,
    ) -> Dict[str, Any]:
        template = (self.root / ".ai/orchestration/prompts/execution-review.md").read_text(encoding="utf-8")
        prompt = template.replace("{{workflow_objective}}", graph["workflow"]["objective"])
        prompt = prompt.replace("{{task_graph}}", json.dumps(graph, indent=2, sort_keys=True))
        prompt = prompt.replace("{{results}}", json.dumps(list(results), indent=2, sort_keys=True))
        schema = load_data(self.root / ".ai/orchestration/schemas/review.schema.json")
        command = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--no-session-persistence",
            "--no-chrome",
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "Read,Grep,Glob",
            "--model",
            self.model,
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            prompt,
        ]
        if self.effort:
            prompt_value = command.pop()
            command.extend(["--effort", self.effort, prompt_value])
        before_head = git_head(workspace)
        before = git_snapshot(workspace)
        process, _ = run_process(command, workspace, self.timeout_seconds)
        after_head = git_head(workspace)
        after = git_snapshot(workspace)
        changed = changed_snapshots(before, after)
        if before_head != after_head:
            raise WorkflowError(
                FailureCode.SCOPE,
                "read-only review changed Git HEAD; commits are prohibited",
                False,
            )
        if changed:
            raise WorkflowError(
                FailureCode.SCOPE,
                f"read-only review modified files: {', '.join(changed)}",
                False,
            )
        if process.returncode != 0:
            raise WorkflowError(
                FailureCode.REVIEW,
                (process.stderr or "Claude review exited non-zero").strip(),
                False,
            )
        try:
            envelope = json.loads(process.stdout)
            review = _review_from_envelope(envelope)
            validate_review(review)
        except (json.JSONDecodeError, TypeError, ValueError, WorkflowError) as error:
            raise WorkflowError(FailureCode.REVIEW, f"invalid review output: {error}", False) from error
        return review


def _review_from_envelope(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    candidate: Any = envelope.get("structured_output", envelope.get("result", envelope))
    if isinstance(candidate, str):
        candidate = json.loads(candidate)
    if not isinstance(candidate, dict):
        raise ValueError("review output is not an object")
    return dict(candidate)


def load_results(directory: Path) -> list:
    return [load_data(path) for path in sorted(directory.glob("TASK-*-attempt-*.json"))]


def apply_review(
    graph: Mapping[str, Any],
    review: Mapping[str, Any],
    profiles: Mapping[str, Any],
    matrix: Mapping[str, Any],
    remediation_cycles: int = 0,
    allowed_executors: Any = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    validate_graph(graph)
    validate_review(review)
    updated = copy.deepcopy(graph)
    if review["status"] == "pass":
        return updated, route_graph(updated, profiles, matrix, allowed_executors)
    graph_limit = graph["workflow"].get("max_remediation_cycles", profiles["budgets"]["max_remediation_cycles"])
    maximum = min(graph_limit, profiles["budgets"]["max_remediation_cycles"])
    if remediation_cycles >= maximum:
        raise WorkflowError(
            FailureCode.BUDGET,
            f"remediation cycle limit reached ({maximum})",
            False,
        )
    existing = {task["id"] for task in updated["tasks"]}
    source_tasks = {task["id"]: task for task in updated["tasks"]}
    for issue in review["issues"]:
        base_id = "TASK-REMEDIATION-" + re.sub(r"[^A-Z0-9]+", "-", issue["id"].upper()).strip("-")
        task_id = base_id
        suffix = 2
        while task_id in existing:
            task_id = f"{base_id}-{suffix}"
            suffix += 1
        affected = issue["affected_task"]
        if affected not in source_tasks:
            raise ValueError(f"review issue {issue['id']} references unknown task {affected}")
        source = source_tasks[affected]
        scope = issue.get("scope") or copy.deepcopy(source["scope"])
        updated["tasks"].append(
            {
                "id": task_id,
                "title": f"Remediate {issue['id']}",
                "type": "implementation",
                "objective": issue["description"],
                "why": f"Semantic review finding {issue['id']} ({issue['severity']}).",
                "context": [affected, issue["id"]],
                "scope": scope,
                "acceptance_criteria": issue["acceptance_criteria"],
                "depends_on": [affected],
                "capabilities": ["implementation", "unit_testing"],
                "complexity": "medium" if issue["severity"] in {"low", "medium"} else "high",
                "risk": "medium" if issue["severity"] in {"low", "medium"} else "high",
                "verification": copy.deepcopy(source["verification"]),
            }
        )
        existing.add(task_id)
    validate_graph(updated)
    return updated, route_graph(updated, profiles, matrix, allowed_executors)
