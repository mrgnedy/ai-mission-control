from __future__ import annotations

import subprocess
import time
import os
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from ..failures import FailureCode, WorkflowError


class Executor(Protocol):
    name: str

    def execute(
        self,
        task: Mapping[str, Any],
        delegation: Mapping[str, Any],
        profile: Mapping[str, Any],
        workspace: Path,
        attempt: int,
    ) -> Dict[str, Any]: ...


def parse_worker_response(value: Any) -> Dict[str, Any]:
    """Require an explicit worker outcome; a successful CLI call is not task success."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise WorkflowError(FailureCode.SCHEMA, "worker returned prose instead of a JSON outcome", False) from error
    if not isinstance(value, dict):
        raise WorkflowError(FailureCode.SCHEMA, "worker outcome must be an object", False)
    outcome = value.get("outcome")
    summary = value.get("summary")
    if outcome not in {"completed", "failed", "decision_required", "permission_required"}:
        raise WorkflowError(FailureCode.SCHEMA, "worker outcome is missing or unsupported", False)
    if not isinstance(summary, str) or not summary.strip():
        raise WorkflowError(FailureCode.SCHEMA, "worker summary must be non-empty", False)
    if outcome == "completed" and re.match(r"^(?:TASK-\S+\s+)?(?:RESULT:\s*)?FAILED\b", summary.strip(), re.I):
        raise WorkflowError(FailureCode.AGENT_IMPLEMENTATION, "worker reported failure while claiming completion", False)
    no_op_reason = value.get("no_op_reason")
    if no_op_reason is not None and (not isinstance(no_op_reason, str) or not no_op_reason.strip()):
        raise WorkflowError(FailureCode.SCHEMA, "worker no_op_reason must be non-empty", False)
    if outcome == "decision_required":
        request = value.get("decision_request")
        if not isinstance(request, dict) or any(
            not isinstance(request.get(key), str) or not request[key].strip()
            for key in ("question", "evidence", "checkpoint")
        ):
            raise WorkflowError(FailureCode.SCHEMA, "worker decision_request is incomplete", False)
        options = request.get("options", [])
        if not isinstance(options, list) or any(not isinstance(item, str) or not item.strip() for item in options):
            raise WorkflowError(FailureCode.SCHEMA, "worker decision_request options are invalid", False)
    return value


def run_process(
    command: Sequence[str],
    workspace: Path,
    timeout_seconds: int,
    environment: Optional[Mapping[str, str]] = None,
) -> Tuple[subprocess.CompletedProcess, int]:
    started = time.monotonic()
    try:
        process = subprocess.run(
            list(command),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, **dict(environment or {})},
        )
    except subprocess.TimeoutExpired as error:
        raise WorkflowError(FailureCode.TIMEOUT, f"provider timed out after {timeout_seconds}s", True) from error
    except OSError as error:
        raise WorkflowError(FailureCode.TOOL, f"provider could not start: {error}", True) from error
    return process, int((time.monotonic() - started) * 1000)
