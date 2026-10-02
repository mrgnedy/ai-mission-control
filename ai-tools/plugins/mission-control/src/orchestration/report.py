from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from .io import atomic_write_text, load_data, utc_now


REPORT_NAME = "run-report.md"


def generate_report(runtime: Path) -> Dict[str, Any]:
    """Write a derived, evidence-only snapshot without changing execution state."""
    state = load_data(runtime / "state.json")
    graph = load_data(runtime / "task-graph.yaml")
    if not isinstance(state, dict) or not isinstance(graph, dict):
        raise ValueError("run state and task graph must be objects")
    report = build_report(runtime, state, graph)
    path = runtime / REPORT_NAME
    atomic_write_text(path, report)
    return {
        "run_id": state["run_id"],
        "status": state["status"],
        "provisional": state["status"] != "completed" or (runtime / ".lock").exists(),
        "report": str(path),
    }


def build_report(runtime: Path, state: Mapping[str, Any], graph: Mapping[str, Any]) -> str:
    tasks = state["tasks"]
    task_definitions = {task["id"]: task for task in graph["tasks"]}
    if set(task_definitions) != set(tasks):
        raise ValueError("run state and task graph have different task IDs")
    attempts: dict[str, list[tuple[Path, Dict[str, Any]]]] = defaultdict(list)
    for path in sorted((runtime / "results").glob("*.json")):
        result = load_data(path)
        task_id = result["task_id"]
        attempt = result["attempt"]
        if task_id not in tasks or path.name != f"{task_id}-attempt-{attempt}.json":
            raise ValueError(f"result does not match its task or filename: {path}")
        attempts[task_id].append((path, result))
    for entries in attempts.values():
        entries.sort(key=lambda entry: entry[1]["attempt"])

    reviews = [(path, load_data(path)) for path in sorted((runtime / "reviews").glob("review-*.json"))]
    revisions = [(path, load_data(path)) for path in sorted((runtime / "route-revisions").glob("*.json"))]
    grants = [(path, load_data(path)) for path in sorted((runtime / "permission-grants").glob("*.json"))]
    reopens = [(path, load_data(path)) for path in sorted((runtime / "task-reopens").glob("*.json"))]
    events_path = runtime / "logs" / "events.jsonl"
    events = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue  # An active run may have a partial final event line.
    draft_paths = []
    manifest_path = runtime / "run.json"
    manifest = {}
    if manifest_path.is_file():
        manifest = load_data(manifest_path)
        change_dir = Path(manifest["change_dir"])
        for path in sorted((change_dir / "orchestration").glob(f"recovery-{state['run_id']}-*-draft-*.yaml")):
            draft_paths.append(path)
    recorded = [result for entries in attempts.values() for _, result in entries]
    verification = Counter(item["status"] for result in recorded for item in result.get("verification", []))
    failures = Counter((result.get("failure") or {}).get("code", "unclassified") for result in recorded if result["status"] != "completed")
    completed = sum(item["status"] == "completed" for item in tasks.values())
    declared_checks = sum(bool(task.get("verification")) for task in task_definitions.values())
    recorded_checks = sum(
        bool(task_definitions[task_id].get("verification"))
        and any(result.get("verification") for _, result in attempts[task_id])
        for task_id in tasks
    )
    first_pass = sum(
        item["status"] == "completed" and len(attempts[task_id]) == 1
        and attempts[task_id][0][1]["attempt"] == 1
        and attempts[task_id][0][1]["status"] == "completed"
        for task_id, item in tasks.items()
    )
    failed_attempts = [result for result in recorded if result["status"] != "completed"]
    provisional = state["status"] != "completed" or (runtime / ".lock").exists()
    lines = [
        "# Mission Control run report — observed facts",
        "",
        f"- Run: `{_inline(state['run_id'])}`; workflow: `{_inline(state['workflow_id'])}`",
        f"- Snapshot: `{utc_now()}`; state: `{_inline(state['status'])}`; **{'PROVISIONAL' if provisional else 'FINAL'}**",
        "- This is a generated snapshot, not an orchestrator diagnosis. Regeneration replaces this file.",
        "- Worker stdout/stderr, prompts, permission commands, and free-text failure messages are intentionally omitted. Follow the evidence links locally if needed.",
        "",
        "## Metrics",
        "",
        f"- Tasks completed: {completed}/{len(tasks)}; first-attempt completions: {first_pass}/{len(tasks)}.",
        f"- Recorded attempts: {len(recorded)}; non-completing attempts: {len(failed_attempts)}; repeat attempts: {sum(max(0, len(entries) - 1) for entries in attempts.values())}.",
        f"- Automatic retry decisions: {sum(event.get('event') == 'task_retry_scheduled' for event in events)}; resume events: {sum(event.get('event') == 'workflow_resumed' for event in events)}.",
        f"- Recorded worker time: {sum(result.get('duration_ms', 0) for result in recorded) / 1000:.1f}s; non-completing attempt time: {sum(result.get('duration_ms', 0) for result in failed_attempts) / 1000:.1f}s.",
        f"- Verification records: {_counts(verification)}. Empty records do not prove checks were run.",
        f"- Tasks with declared checks and at least one recorded check: {recorded_checks}/{declared_checks}. This is evidence coverage, not a pass rate.",
        f"- Failure codes: {_counts(failures)}.",
        f"- Semantic reviews: {len(reviews)}; review status: {_inline(state.get('review_status', 'unknown'))}; review failures: {sum(event.get('event') == 'review_failed' for event in events)}; review issues: {sum(len(review.get('issues', [])) for _, review in reviews)}; remediation cycles: {state.get('remediation_cycles', 0)}.",
        f"- Applied route revisions: {len(revisions)}; permission grants: {len(grants)}; task reopens: {len(reopens)}; unavailable providers at snapshot: {', '.join(sorted(state.get('unavailable_providers', []))) or 'none'}.",
        "- Token usage, actual cost, human-intervention count, and time-to-unblock: **not recorded**. A grant or reroute alone does not prove human involvement or autonomous recovery.",
        "",
        "## Tasks and workers",
        "",
        "| Task | Type / risk | Current state | Recorded attempts | Workers (provider / profile / model) | Verification | Evidence |",
        "|---|---|---|---:|---|---|---|",
    ]
    if manifest.get("supersedes"):
        lines.insert(4, f"- Supersedes prior run: `{_inline(manifest['supersedes'])}`. This link does not copy task state or approvals.")
    for task_id in sorted(tasks):
        definition = task_definitions[task_id]
        entries = attempts[task_id]
        workers = ", ".join(dict.fromkeys(
            f"{result.get('executor', '?')} / {result.get('profile', '?')} / {result.get('model') or '?'}"
            for _, result in entries
        )) or "not started"
        checks = Counter(item["status"] for _, result in entries for item in result.get("verification", []))
        evidence = ", ".join(f"[attempt {result['attempt']}](results/{path.name})" for path, result in entries) or "—"
        lines.append(
            f"| `{_cell(task_id)}` | {_cell(definition.get('type', '?'))} / {_cell(definition.get('risk', '?'))} "
            f"| {_cell(tasks[task_id]['status'])} | {len(entries)} | {_cell(workers)} | {_cell(_counts(checks))} | {evidence} |"
        )
    lines.extend(["", "## Failed or interrupted attempts", ""])
    lines.extend([
        "| Task | Attempt | Worker | Stage | Outcome / failure code | Verification | Evidence |",
        "|---|---:|---|---|---|---|---|",
    ])
    for task_id in sorted(attempts):
        for path, result in attempts[task_id]:
            if result["status"] == "completed":
                continue
            code = (result.get("failure") or {}).get("code", "unclassified")
            checks = Counter(item["status"] for item in result.get("verification", []))
            lines.append(
                f"| `{_cell(task_id)}` | {result['attempt']} | {_cell(result.get('executor', '?'))} / {_cell(result.get('profile', '?'))} / {_cell(result.get('model') or '?')} "
                f"| {_failure_stage(result)} | {_cell(result['status'])} / `{_cell(code)}` | {_cell(_counts(checks))} | [result](results/{path.name}) |"
            )
    if not failed_attempts:
        lines.append("No non-completing attempt was recorded.")
    unrecorded = [task_id for task_id, item in tasks.items() if item["attempts"] > len(attempts[task_id])]
    if unrecorded:
        lines.append(f"\nStarted attempts without a result file (possibly interrupted): {', '.join(sorted(unrecorded))}.")

    lines.extend(["", "## Review and interventions", ""])
    for path, review in reviews:
        severities = Counter(issue["severity"] for issue in review.get("issues", []))
        lines.append(f"- [Review {path.stem}](reviews/{path.name}): {_inline(review['status'])}; issues {_counts(severities)}.")
    for event in events:
        if event.get("event") == "review_failed":
            lines.append(f"- Semantic review failed at cycle {event.get('cycle', '?')}; failure code `{_inline(event.get('failure_code', 'unknown'))}`. Error text omitted.")
    applied_drafts = {str(Path(revision.get("plan_path", "")).resolve()) for _, revision in revisions if revision.get("plan_path")}
    for path in draft_paths:
        try:
            draft = load_data(path)
        except Exception:
            lines.append(f"- Recovery draft `{_inline(path.name)}` could not be read; inspect it locally.")
            continue
        if not isinstance(draft, dict):
            lines.append(f"- Recovery draft `{_inline(path.name)}` is not an object; inspect it locally.")
            continue
        if draft.get("run_id") != state["run_id"]:
            continue
        routes = draft.get("routes")
        if not isinstance(routes, dict):
            lines.append(f"- Recovery draft `{_inline(path.name)}` has no valid routes; inspect it locally.")
            continue
        for task_id, choice in sorted(routes.items()):
            if not isinstance(choice, dict) or not all(key in choice for key in ("from_profile", "to_profile")):
                lines.append(f"- Recovery draft `{_inline(path.name)}` has an invalid route for `{_inline(task_id)}`.")
                continue
            status = "applied" if str(path.resolve()) in applied_drafts else "proposed; not recorded as applied"
            lines.append(
                f"- Recovery draft `{_inline(path.name)}` ({status}): `{_inline(task_id)}` "
                f"`{_inline(choice['from_profile'])}` → `{_inline(choice['to_profile'])}`. "
                "The proposal's reason and partial-work review remain in the draft."
            )
    for path, revision in revisions:
        for task_id, change in sorted(revision["routes"].items()):
            lines.append(
                f"- [Applied route revision {path.stem}](route-revisions/{path.name}): `{_inline(task_id)}` "
                f"`{_inline(change['from_profile'])}` → `{_inline(change['to_route']['profile'])}`."
            )
    for path, grant in grants:
        lines.append(f"- [Permission grant {path.stem}](permission-grants/{path.name}): `{_inline(grant['task_id'])}`; command omitted here.")
    for path, reopen in reopens:
        lines.append(
            f"- [Task reopen {path.stem}](task-reopens/{path.name}): `{_inline(reopen['task_id'])}` "
            f"after failed attempt {reopen['result_attempt']}; reason and checkpoint omitted here."
        )
    if not reviews and not revisions and not grants and not reopens and not draft_paths:
        lines.append("No review, recovery draft, route revision, or permission grant was recorded.")
    lines.extend([
        "",
        "## Orchestrator interpretation",
        "",
        "Not generated here. The orchestrator must review this evidence and separately report: incident, likely cause (hypothesis), immediate action proposed/taken, outcome after verification, and only justified workflow improvements. Do not treat a proposed fix as proven or edit this generated file.",
        "",
    ])
    return "\n".join(lines)


def _counts(values: Mapping[str, int]) -> str:
    return ", ".join(f"{key} {values[key]}" for key in sorted(values)) or "none recorded"


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _inline(value: Any) -> str:
    return str(value).replace("`", "'").replace("\n", " ").replace("\r", " ")


def _failure_stage(result: Mapping[str, Any]) -> str:
    if any(item["status"] in {"failed", "timeout"} for item in result.get("verification", [])):
        return "verification"
    if (result.get("failure") or {}).get("code") == "review":
        return "review"
    return "execution / unknown"
