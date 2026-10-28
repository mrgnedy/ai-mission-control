from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def verification_record(
    command: List[str],
    status: str,
    exit_code: Optional[int],
    duration_ms: int,
    stdout: str = "",
    stderr: str = "",
) -> Dict[str, Any]:
    return {
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
    }


def task_result(
    task_id: str,
    status: str,
    summary: str,
    executor: str,
    profile: str,
    attempt: int,
    duration_ms: int,
    *,
    changed_files: Iterable[str] = (),
    verification: Iterable[Dict[str, Any]] = (),
    warnings: Iterable[str] = (),
    errors: Iterable[str] = (),
    failure: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    provider_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "changed_files": sorted(set(changed_files)),
        "verification": list(verification),
        "warnings": list(warnings),
        "errors": list(errors),
        "failure": failure,
        "executor": executor,
        "profile": profile,
        "model": model,
        "effort": effort,
        "attempt": attempt,
        "duration_ms": max(0, duration_ms),
        "provider_session_id": provider_session_id,
    }
