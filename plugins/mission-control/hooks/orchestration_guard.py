#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


def matches(path: str, pattern: str) -> bool:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from orchestration.scope import _match
    return _match(path, pattern)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, TypeError):
        return 0
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") not in {"Edit", "Write"}:
        return 0
    scope_text = os.environ.get("AI_TASK_SCOPE")
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", event.get("cwd", "."))).resolve()
    if not scope_text and event.get("agent_type") == "mission-control:native-writer":
        # Native Agents do not inherit task-specific environment variables. There
        # can only be one writing claim in this shared workspace.
        try:
            claims = []
            for path in (project / ".ai-runtime").glob("*/*/native-session.json"):
                session = json.loads(path.read_text(encoding="utf-8"))
                claims.extend(claim for claim in session["claims"].values()
                              if claim["kind"] == "task" and claim["status"] in {"active", "validating"}
                              and claim["route"]["isolation"] != "read_only")
            if len(claims) != 1:
                raise ValueError("native writer needs exactly one active writing claim")
            scope_text = json.dumps(claims[0]["task"]["scope"])
        except (ValueError, KeyError, OSError) as error:
            print(f"Blocked: {error}", file=sys.stderr)
            return 2
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
    target = Path(raw_path)
    resolved = target.resolve() if target.is_absolute() else (project / target).resolve()
    try:
        relative = resolved.relative_to(project).as_posix()
    except ValueError:
        print(f"Blocked: {target} is outside the task workspace", file=sys.stderr)
        return 2
    forbidden = scope.get("forbidden", [])
    allowed = scope.get("allowed", [])
    if relative == ".ai-runtime" or relative.startswith(".ai-runtime/"):
        print("Blocked: workers cannot edit orchestration runtime records", file=sys.stderr)
        return 2
    if any(matches(relative, pattern) for pattern in forbidden) or not any(matches(relative, pattern) for pattern in allowed):
        task_id = os.environ.get("AI_TASK_ID", "current task")
        print(f"Blocked: {relative} is outside the declared scope for {task_id}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
