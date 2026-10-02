from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from ..failures import FailureCode, WorkflowError
from ..models import task_result
from ..prompts import render_task_prompt
from ..scope import changed_snapshots, git_head, git_snapshot, validate_scope
from .base import parse_worker_response, run_process


CLAUDE_TASK_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "outcome"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "outcome": {"enum": ["completed", "failed", "decision_required", "permission_required"]},
        "no_op_reason": {"type": "string", "minLength": 1},
        "decision_request": {
            "type": "object", "additionalProperties": False,
            "required": ["question", "evidence", "checkpoint"],
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "evidence": {"type": "string", "minLength": 1},
                "checkpoint": {"type": "string", "minLength": 1},
                "options": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
        },
        "permission_request": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "reason", "checkpoint"],
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "checkpoint": {"type": "string", "minLength": 1},
            },
        },
    },
}


class ClaudeExecutor:
    def __init__(
        self, root: Path, binary: str = "claude", name: str = "claude",
        allowed_bash_rules: Sequence[str] = (),
    ) -> None:
        self.root = root
        self.binary = binary
        self.name = name
        self.allowed_bash_rules = tuple(allowed_bash_rules)

    def execute(
        self,
        task: Mapping[str, Any],
        delegation: Mapping[str, Any],
        profile: Mapping[str, Any],
        workspace: Path,
        attempt: int,
    ) -> Dict[str, Any]:
        prompt = render_task_prompt(
            self.root / ".ai/orchestration/prompts/claude-task.md",
            self.root / "skills/code-discipline/SKILL.md",
            task,
            delegation,
        )
        read_only = delegation["isolation"] == "read_only"
        rules = list(dict.fromkeys([*self.allowed_bash_rules, *delegation.get("extra_allowed_bash_rules", [])]))
        if not read_only and rules:
            prompt += "\n\nPre-approved Bash rules for this worker:\n" + "\n".join(
                f"- {rule}" for rule in rules
            )
        permission_mode = "plan" if read_only else "acceptEdits"
        command = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            permission_mode,
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--no-chrome",
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]
        if read_only:
            command.extend(["--tools", "Read,Grep,Glob"])
        else:
            command.extend(["--tools", "Read,Edit,Write,Glob,Grep,Bash"])
            if rules:
                command.extend(["--allowedTools", *rules])
        command.extend([
            "--json-schema",
            json.dumps(CLAUDE_TASK_RESPONSE_SCHEMA, separators=(",", ":")),
        ])
        if delegation.get("model"):
            command.extend(["--model", delegation["model"]])
        if delegation.get("effort"):
            command.extend(["--effort", delegation["effort"]])
        command.append(prompt)
        try:
            before_head = git_head(workspace)
            before = git_snapshot(workspace)
        except WorkflowError as error:
            return self._failure(task, delegation, attempt, 0, error)
        duration = 0
        error: Optional[WorkflowError] = None
        summary = "Claude task completed."
        session_id: Optional[str] = None
        permission_request: Optional[Dict[str, str]] = None
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
                try:
                    error_envelope = json.loads(process.stdout)
                except json.JSONDecodeError:
                    error_envelope = {}
                message = str(error_envelope.get("result") or process.stderr or "Claude exited non-zero").strip()
                if _session_limit(error_envelope, message):
                    raise WorkflowError(FailureCode.PROVIDER_UNAVAILABLE, message, False)
                raise WorkflowError(FailureCode.TOOL, message, True)
            envelope = json.loads(process.stdout)
            if envelope.get("is_error"):
                message = str(envelope.get("result") or "Claude returned an error result")
                code = FailureCode.PROVIDER_UNAVAILABLE if _session_limit(envelope, message) else FailureCode.TOOL
                raise WorkflowError(code, message, code != FailureCode.PROVIDER_UNAVAILABLE)
            response, session_id = _parse_envelope(envelope)
            response = parse_worker_response(response)
            summary = response["summary"]
            no_op_reason = response.get("no_op_reason")
            if response["outcome"] == "failed":
                raise WorkflowError(FailureCode.AGENT_IMPLEMENTATION, summary, False)
            if response["outcome"] == "decision_required":
                decision_request = response["decision_request"]
                raise WorkflowError(FailureCode.DECISION_REQUIRED, response["decision_request"]["question"], False)
            if response.get("outcome") == "permission_required":
                if read_only:
                    raise WorkflowError(FailureCode.CONFIGURATION, "read-only Claude worker cannot request Bash permission", False)
                request = response.get("permission_request")
                if not isinstance(request, dict) or any(
                    not isinstance(request.get(key), str) or not request[key].strip()
                    for key in ("command", "reason", "checkpoint")
                ):
                    raise WorkflowError(FailureCode.SCHEMA, "Claude returned an incomplete permission request", False)
                permission_request = request
                raise WorkflowError(FailureCode.PERMISSION_REQUIRED, request["reason"], False)
        except json.JSONDecodeError:
            error = WorkflowError(FailureCode.TOOL, "Claude returned malformed JSON", False)
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
            if read_only and changed:
                raise WorkflowError(
                    FailureCode.SCOPE,
                    f"read-only task modified files: {', '.join(changed)}",
                    False,
                )
            validate_scope(changed, task["scope"]["allowed"], task["scope"]["forbidden"])
        except WorkflowError as scope_error:
            error = scope_error
        if error is not None:
            result = self._failure(task, delegation, attempt, duration, error, changed, permission_request)
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
        permission_request: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        result = task_result(
            task["id"], "failed", error.message, self.name, delegation["profile"], attempt, duration,
            changed_files=changed or [], errors=[error.message], failure=error.as_dict(),
            model=delegation.get("model"), effort=delegation.get("effort"),
        )
        if permission_request is not None and error.code == FailureCode.PERMISSION_REQUIRED:
            result["permission_request"] = permission_request
        return result


def _parse_envelope(envelope: Mapping[str, Any]) -> tuple:
    session_id = envelope.get("session_id")
    return envelope.get("structured_output", envelope.get("result")), session_id


def _session_limit(envelope: Mapping[str, Any], message: str) -> bool:
    normalized = message.lower()
    return "session limit" in normalized or (
        envelope.get("api_error_status") == 429 and "resets" in normalized
    )
