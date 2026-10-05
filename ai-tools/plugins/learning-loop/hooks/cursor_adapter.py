#!/usr/bin/env python3
"""Adapt Cursor's postToolUse payload to the shared advisory checker."""

import json
import subprocess
import sys
from pathlib import Path


EDIT_TOOLS = {"Write", "Edit", "ApplyPatch"}


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        return
    if not isinstance(event, dict) or event.get("tool_name") not in EDIT_TOOLS:
        return
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(path, str) or not path:
        return
    cwd = event.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return
    changed = Path(path)
    if not changed.is_absolute():
        changed = Path(cwd) / changed
    if not changed.is_file():
        return
    checker = Path(__file__).with_name("rule_check.sh")
    payload = json.dumps({"tool_name": event["tool_name"], "file_path": str(changed)})
    try:
        result = subprocess.run(
            ["sh", str(checker)], input=payload, text=True, capture_output=True,
            timeout=10, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
        response = json.loads(result.stdout)
        message = response["hookSpecificOutput"]["additionalContext"]
        if isinstance(message, str) and message:
            print(json.dumps({"additional_context": message}))
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return


if __name__ == "__main__":
    main()
