from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .io import utc_now


SENSITIVE_KEYS = {"prompt", "stdout", "stderr", "environment", "api_key", "token", "secret"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class EventLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {"timestamp": utc_now(), "run_id": self.run_id, "event": event}
        record.update(redact(fields))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
