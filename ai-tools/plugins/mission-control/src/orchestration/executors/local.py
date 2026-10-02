from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping

from ..failures import FailureCode
from ..models import task_result
from ..verification import run_commands, verification_passed


class LocalExecutor:
    def __init__(self, name: str = "local") -> None:
        self.name = name

    def execute(
        self,
        task: Mapping[str, Any],
        delegation: Mapping[str, Any],
        profile: Mapping[str, Any],
        workspace: Path,
        attempt: int,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        records = run_commands(task["verification"], workspace, delegation["timeout_seconds"])
        passed = verification_passed(records)
        duration = int((time.monotonic() - started) * 1000)
        failure = None if passed else {
            "code": FailureCode.TEST.value,
            "message": "one or more local verification commands failed",
            "retryable": False,
        }
        return task_result(
            task["id"],
            "completed" if passed else "failed",
            "Local verification passed." if passed else "Local verification failed.",
            self.name,
            delegation["profile"],
            attempt,
            duration,
            verification=records,
            errors=[] if passed else [failure["message"]],
            failure=failure,
            model=delegation.get("model"),
            effort=delegation.get("effort"),
        )
