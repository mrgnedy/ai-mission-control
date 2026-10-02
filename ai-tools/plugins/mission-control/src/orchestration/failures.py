from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class FailureCode(str, Enum):
    AGENT_IMPLEMENTATION = "agent_implementation"
    TOOL = "tool"
    TIMEOUT = "timeout"
    SCHEMA = "schema"
    TEST = "test"
    SCOPE = "scope"
    DEPENDENCY = "dependency"
    MERGE_CONFLICT = "merge_conflict"
    REVIEW = "review"
    CONFIGURATION = "configuration"
    BUDGET = "budget"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PERMISSION_REQUIRED = "permission_required"
    DECISION_REQUIRED = "decision_required"


@dataclass
class WorkflowError(Exception):
    code: FailureCode
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}


class ValidationError(WorkflowError):
    def __init__(self, message: str) -> None:
        super().__init__(FailureCode.SCHEMA, message, False)
