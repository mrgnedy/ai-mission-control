from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def render_task_prompt(
    template_path: Path,
    discipline_path: Path,
    task: Mapping[str, Any],
    delegation: Mapping[str, Any] | None = None,
) -> str:
    template = template_path.read_text(encoding="utf-8")
    discipline = discipline_path.read_text(encoding="utf-8")
    body = discipline.split("---", 2)[-1].strip() if discipline.startswith("---") else discipline.strip()
    values = {
        "task_id": task["id"],
        "title": task["title"],
        "objective": task["objective"],
        "why": task.get("why", "Not separately stated."),
        "dependencies": json.dumps(task["depends_on"]),
        "context": json.dumps(task.get("context", [])),
        "allowed_scope": "\n".join(f"- {item}" for item in task["scope"]["allowed"]),
        "forbidden_scope": "\n".join(f"- {item}" for item in task["scope"]["forbidden"]) or "- none",
        "acceptance_criteria": "\n".join(f"- {item}" for item in task["acceptance_criteria"]),
        "verification": "\n".join(f"- {json.dumps(item)}" for item in task["verification"]) or "- none",
        "code_discipline": body,
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    if delegation:
        decisions = delegation.get("resolved_decisions", [])
        if decisions:
            template += "\n\nFrozen resolved decisions for this task:\n" + "\n".join(f"- {item}" for item in decisions)
        if delegation.get("continuation"):
            template += (
                "\n\nContinuation checkpoint from the prior failed attempt. Inspect retained edits, "
                "finish only remaining work, and rerun the task's required checks:\n"
                + delegation["continuation"]
            )
        if delegation.get("decision_resolution"):
            template += "\n\nRecorded in-bounds execution-time decision:\n" + delegation["decision_resolution"]
    return template
