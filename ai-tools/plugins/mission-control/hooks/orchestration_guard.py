#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


def matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    if normalized.endswith("/**"):
        prefix = normalized[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatchcase(path, normalized)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, TypeError):
        return 0
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") not in {"Edit", "Write"}:
        return 0
    scope_text = os.environ.get("AI_TASK_SCOPE")
    if not scope_text:
        return 0
    try:
        scope: Mapping[str, Any] = json.loads(scope_text)
    except (ValueError, TypeError):
        print("Blocked: AI_TASK_SCOPE is invalid JSON", file=sys.stderr)
        return 2
    raw_path = event.get("tool_input", {}).get("file_path", "")
    if not raw_path:
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
    target = Path(raw_path)
    resolved = target.resolve() if target.is_absolute() else (project / target).resolve()
    try:
        relative = resolved.relative_to(project).as_posix()
    except ValueError:
        print(f"Blocked: {target} is outside the task workspace", file=sys.stderr)
        return 2
    forbidden = scope.get("forbidden", [])
    allowed = scope.get("allowed", [])
    if any(matches(relative, pattern) for pattern in forbidden) or not any(matches(relative, pattern) for pattern in allowed):
        task_id = os.environ.get("AI_TASK_ID", "current task")
        print(f"Blocked: {relative} is outside the declared scope for {task_id}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
