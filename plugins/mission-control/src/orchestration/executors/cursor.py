from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ..failures import FailureCode, WorkflowError
from ..models import task_result
from ..prompts import render_task_prompt
from ..scope import changed_snapshots, git_head, git_snapshot, validate_scope
from .base import parse_worker_response, run_process


class CursorExecutor:
    def __init__(self, root: Path, binary: str = "cursor-agent", name: str = "cursor") -> None:
        self.root = root
        self.binary = binary
        self.name = name

    def execute(
        self,
        task: Mapping[str, Any],
        delegation: Mapping[str, Any],
        profile: Mapping[str, Any],
        workspace: Path,
        attempt: int,
    ) -> Dict[str, Any]:
        prompt = render_task_prompt(
            self.root / ".ai/orchestration/prompts/cursor-task.md",
            self.root / "skills/code-discipline/SKILL.md",
            task,
            delegation,
        )
        command = [
            self.binary,
            "-p",
            "--force",
            "--sandbox",
            "enabled",
            "--output-format",
            "json",
        ]
        if delegation.get("model"):
            command.extend(["--model", delegation["model"]])
        command.append(prompt)
        try:
            before_head = git_head(workspace)
            before = git_snapshot(workspace)
        except WorkflowError as error:
            return self._failure(task, delegation, attempt, 0, error)
        duration = 0
        error: Optional[WorkflowError] = None
        summary = "Cursor task completed."
        session_id: Optional[str] = None
        decision_request: Optional[Dict[str, Any]] = None
        no_op_reason: Optional[str] = None
        try:
            process, duration = run_process(
                command,
                workspace,
                delegation["timeout_seconds"],
                {"AI_TASK_ID": task["id"], "AI_TASK_SCOPE": json.dumps(task["scope"], sort_keys=True)},
            )
            if process.returncode != 0:
                output = (process.stderr or process.stdout or "Cursor Agent exited non-zero").strip()
                unavailable = _quota_exhausted("\n".join((process.stderr or "", process.stdout or "")))
                raise WorkflowError(
                    FailureCode.PROVIDER_UNAVAILABLE if unavailable else FailureCode.TOOL,
                    output,
                    not unavailable,
                )
            envelope = json.loads(process.stdout)
            if envelope.get("type") != "result" or envelope.get("is_error", False):
                message = str(envelope.get("result") or "Cursor returned an error result")
                code = FailureCode.PROVIDER_UNAVAILABLE if _quota_exhausted(message) else FailureCode.AGENT_IMPLEMENTATION
                raise WorkflowError(code, message, False)
            response = parse_worker_response(envelope.get("result"))
            summary = response["summary"]
            no_op_reason = response.get("no_op_reason")
            if response["outcome"] == "failed":
                raise WorkflowError(FailureCode.AGENT_IMPLEMENTATION, summary, False)
            if response["outcome"] == "decision_required":
                decision_request = response["decision_request"]
                raise WorkflowError(FailureCode.DECISION_REQUIRED, decision_request["question"], False)
            if response["outcome"] == "permission_required":
                raise WorkflowError(FailureCode.SCHEMA, "Cursor cannot use the Claude permission handoff", False)
            session_id = envelope.get("session_id")
        except json.JSONDecodeError:
            error = WorkflowError(FailureCode.TOOL, "Cursor returned malformed JSON", False)
        except WorkflowError as caught:
            error = caught
        try:
            after_head = git_head(workspace)
            after = git_snapshot(workspace)
            changed = changed_snapshots(before, after)
        except WorkflowError as snapshot_error:
            after_head = before_head
            changed = []
            error = snapshot_error
        try:
            if before_head != after_head:
                raise WorkflowError(
                    FailureCode.SCOPE,
                    "provider changed Git HEAD; task-created commits are prohibited",
                    False,
                )
            validate_scope(changed, task["scope"]["allowed"], task["scope"]["forbidden"])
        except WorkflowError as scope_error:
            error = scope_error
        if error is not None:
            result = self._failure(task, delegation, attempt, duration, error, changed)
            if decision_request is not None and error.code == FailureCode.DECISION_REQUIRED:
                result["decision_request"] = decision_request
            return result
        result = task_result(
            task["id"], "completed", summary, self.name, delegation["profile"], attempt, duration,
            changed_files=changed, model=delegation.get("model"), provider_session_id=session_id,
            effort=delegation.get("effort"),
        )
        if no_op_reason is not None:
            result["no_op_reason"] = no_op_reason
        return result

    def _failure(
        self,
        task: Mapping[str, Any],
        delegation: Mapping[str, Any],
        attempt: int,
        duration: int,
        error: WorkflowError,
        changed: Optional[list] = None,
    ) -> Dict[str, Any]:
        return task_result(
            task["id"], "failed", error.message, self.name, delegation["profile"], attempt, duration,
            changed_files=changed or [], errors=[error.message], failure=error.as_dict(),
            model=delegation.get("model"), effort=delegation.get("effort"),
        )


def _quota_exhausted(message: str) -> bool:
    normalized = message.lower()
    return "monthly usage limit" in normalized or ("usage limit" in normalized and "group policy" in normalized)
