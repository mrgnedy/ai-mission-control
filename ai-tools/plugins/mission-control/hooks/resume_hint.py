#!/usr/bin/env python3
"""Read-only SessionStart hint for unfinished Mission Control runs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _package_root() -> Path:
    hook = Path(__file__).resolve()
    for candidate in (hook.parents[2], hook.parents[1]):
        if (candidate / "src/orchestration").is_dir():
            return candidate
    raise RuntimeError("Mission Control package root was not found")


def main() -> int:
    root = _package_root()
    sys.path.insert(0, str(root / "src"))
    from orchestration.runs import discover_runs

    workspace = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()
    schemas = root / ".ai/orchestration/schemas"
    records = discover_runs(workspace, schemas)
    if not records:
        return 0

    summaries = [
        f"- {record['run_id']} ({record['workflow_id']}): {record['classification']} — {record['reason']}"
        for record in records
    ]
    context = "\n".join([
        "Mission Control found unfinished local runs:",
        *summaries,
        "Do not recover or resume automatically. Mention the relevant run to the user; continue only after explicit user intent.",
        "Recovery-required runs need a reviewed quota fallback draft and applied route revision before resume. Other failed, blocked, source-changed, active, or corrupt runs must not be blindly resumed.",
    ])
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
