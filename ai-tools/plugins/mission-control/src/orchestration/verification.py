from __future__ import annotations

import subprocess
import time
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .models import verification_record


COMMAND_ENVIRONMENT_KEYS = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "COMSPEC", "PATHEXT",
}


def command_environment() -> Dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in COMMAND_ENVIRONMENT_KEYS}


def run_commands(
    commands: Iterable[List[str]], workspace: Path, timeout_seconds: int
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        try:
            process = subprocess.run(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=command_environment(),
            )
            duration = int((time.monotonic() - started) * 1000)
            status = "passed" if process.returncode == 0 else "failed"
            results.append(
                verification_record(command, status, process.returncode, duration, process.stdout, process.stderr)
            )
            if status == "failed":
                break
        except subprocess.TimeoutExpired as error:
            duration = int((time.monotonic() - started) * 1000)
            stdout = error.stdout if isinstance(error.stdout, str) else ""
            stderr = error.stderr if isinstance(error.stderr, str) else ""
            results.append(verification_record(command, "timeout", None, duration, stdout, stderr))
            break
        except OSError as error:
            duration = int((time.monotonic() - started) * 1000)
            results.append(verification_record(command, "failed", None, duration, "", str(error)))
            break
    return results


def verification_passed(records: Iterable[Mapping[str, Any]]) -> bool:
    return all(record["status"] == "passed" for record in records)


def collect_task_evidence(
    task: Mapping[str, Any], route: Mapping[str, Any], workspace: Path,
    before_head: Optional[str], before_snapshot: Mapping[str, str], result: Dict[str, Any],
    *, timeout_seconds: int, force_commands: bool = False,
) -> Dict[str, Any]:
    """Collect file/check evidence from the checkout, never from a worker narrative.

    Native calls own their verification; legacy CLI calls retain executor checks.
    Both paths recheck the checkout after commands, which can themselves write.
    """
    from .failures import FailureCode, WorkflowError
    from .scope import changed_snapshots, git_head, git_snapshot, validate_scope

    def snapshot() -> Dict[str, str]:
        value = git_snapshot(workspace)
        return {path: digest for path, digest in value.items()
                if not (force_commands and (path == ".ai-runtime" or path.startswith(".ai-runtime/")))}

    def failed(code: str, message: str) -> None:
        result["status"] = "failed"
        result["summary"] = message
        result["errors"].append(message)
        result["failure"] = {"code": code, "message": message, "retryable": False}

    def check() -> None:
        changed = changed_snapshots(dict(before_snapshot), snapshot())
        result["changed_files"] = sorted(set(changed) | set(route.get("retained_changed_files", [])))
        if before_head is not None and git_head(workspace) != before_head:
            raise WorkflowError(FailureCode.SCOPE, "task execution changed Git HEAD; task-created commits are prohibited", False)
        if route["isolation"] == "read_only" and changed:
            raise WorkflowError(FailureCode.SCOPE, "read-only task modified files: " + ", ".join(changed), False)
        validate_scope(result["changed_files"], task["scope"]["allowed"], task["scope"]["forbidden"])

    try:
        check()
        if result["status"] == "completed":
            if (route["executor"] != "local" and route["isolation"] != "read_only" and
                    task["type"] in {"implementation", "business_logic", "codemod", "unit_testing", "integration_testing"} and
                    not result["changed_files"] and not result.get("no_op_reason")):
                failed("agent_implementation", "writing task reported completion without edits or a no-op reason")
            if result["status"] == "completed" and task["verification"] and (force_commands or not result["verification"]):
                result["verification"] = run_commands(task["verification"], workspace, timeout_seconds)
                if not verification_passed(result["verification"]):
                    failed("test", "Post-execution verification failed.")
        check()
    except WorkflowError as error:
        failed(error.code.value, error.message)
    return result
