from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .io import atomic_write_json, load_data, utc_now
from .recovery import effective_delegation
from .runs import failed_attempt, file_sha256, sources_match
from .scope import git_head, git_snapshot, validate_scope
from .state import refresh, transition


MAX_GRANTS_PER_TASK = 3
RULE = re.compile(r"^Bash\((.+)\)$", re.DOTALL)


def validate_bash_rule(rule: str) -> None:
    match = RULE.fullmatch(rule) if isinstance(rule, str) else None
    if match is None or not match.group(1).strip() or match.group(1) in {"*", ":*"} or "," in rule:
        raise ValueError(f"invalid or overbroad Claude Bash rule: {rule!r}")


def _grant_files(runtime: Path) -> List[Path]:
    directory = runtime / "permission-grants"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def effective_grants(runtime: Path, run_id: str, task_id: str) -> List[Dict[str, Any]]:
    grants: List[Dict[str, Any]] = []
    for index, path in enumerate(_grant_files(runtime), start=1):
        grant = load_data(path)
        if not isinstance(grant, dict) or grant.get("schema_version") != "1.0" or grant.get("run_id") != run_id or grant.get("sequence") != index:
            raise ValueError(f"invalid permission grant: {path}")
        if path.name != f"{index:04d}.json":
            raise ValueError(f"permission grant sequence is not contiguous: {path}")
        validate_bash_rule(grant.get("rule"))
        if grant.get("rule") != _exact_command_rule(grant.get("command")):
            raise ValueError(f"permission grant rule disagrees with its command: {path}")
        if not isinstance(grant.get("result_attempt"), int) or grant["result_attempt"] < 1:
            raise ValueError(f"permission grant has invalid attempt: {path}")
        result_path = runtime / "results" / f"{grant.get('task_id')}-attempt-{grant['result_attempt']}.json"
        if not result_path.is_file() or file_sha256(result_path) != grant.get("result_sha256"):
            raise ValueError(f"permission grant source result changed: {path}")
        result = load_data(result_path)
        if (result.get("permission_request") or {}).get("command") != grant["command"]:
            raise ValueError(f"permission grant command disagrees with source result: {path}")
        if grant.get("task_id") == task_id:
            grants.append(grant)
    return grants


def _exact_command_rule(command: str) -> str:
    if not isinstance(command, str) or not command or command != command.strip() or len(command) > 500:
        raise ValueError("permission request needs one exact, non-empty command")
    if any(token in command for token in ("\n", "\r", ";", "|", "&", "`", "$(", ">", "<", ",", "*", "?")):
        raise ValueError("permission request must name one simple exact command, not a compound or wildcard")
    rule = f"Bash({command})"
    validate_bash_rule(rule)
    return rule


def task_permission_context(runtime: Path, run_id: str, task_id: str, attempt: int, workspace: Path) -> Dict[str, Any]:
    grants = effective_grants(runtime, run_id, task_id)
    if not grants:
        return {}
    context: Dict[str, Any] = {"extra_allowed_bash_rules": [grant["rule"] for grant in grants]}
    latest = grants[-1]
    if latest["result_attempt"] == attempt - 1:
        if git_head(workspace) != latest["git_head"] or _source_snapshot(workspace, runtime) != latest["git_snapshot"]:
            raise ValueError(f"task {task_id} files changed since permission was granted")
        result_path = runtime / "results" / f"{task_id}-attempt-{latest['result_attempt']}.json"
        if file_sha256(result_path) != latest["result_sha256"]:
            raise ValueError(f"task {task_id} permission request result changed")
        result = load_data(result_path)
        context["continuation"] = result["permission_request"]["checkpoint"]
    return context


def grant_permission(
    runtime: Any,
    schemas: Path,
    task_id: str,
    reason: str,
    *,
    partial_reviewed: bool = False,
) -> Dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("grant reason must explain why this command is within the approved task")
    with runtime:
        manifest = load_data(runtime.manifest_path)
        if not sources_match(manifest):
            raise ValueError("visible orchestration sources changed; permission grant is unsafe")
        graph = load_data(runtime.root / "task-graph.yaml")
        state = runtime.load_state(graph)
        if task_id not in state["tasks"] or state["tasks"][task_id]["status"] != "failed" or state["tasks"][task_id].get("failure_code") != "permission_required":
            raise ValueError(f"task {task_id} has no pending permission request")
        result_path, result, result_hash = failed_attempt(runtime.root, state, task_id)
        request = result.get("permission_request")
        if not isinstance(request, dict):
            raise ValueError("latest task result has no valid permission request")
        command = request["command"]
        rule = _exact_command_rule(command)
        if result["changed_files"] and not partial_reviewed:
            raise ValueError("task has partial edits; inspect them and pass --partial-reviewed")
        task = next(item for item in graph["tasks"] if item["id"] == task_id)
        route = effective_delegation(runtime.root, schemas)["execution_plan"][task_id]
        if route["executor"] != "claude" or route["isolation"] == "read_only":
            raise ValueError("permission grants require a writing Claude worker")
        validate_scope(result["changed_files"], task["scope"]["allowed"], task["scope"]["forbidden"])
        workspace = _task_workspace(runtime.root, manifest, task_id, route)
        head = git_head(workspace)
        snapshot = _source_snapshot(workspace, runtime.root)
        grants = effective_grants(runtime.root, manifest["run_id"], task_id)
        prior = next((item for item in grants if item["result_sha256"] == result_hash), None)
        if prior is None:
            if len(grants) >= MAX_GRANTS_PER_TASK:
                raise ValueError(f"task {task_id} exceeded its permission-grant limit")
            sequence = len(_grant_files(runtime.root)) + 1
            grant = {
                "schema_version": "1.0", "run_id": manifest["run_id"], "sequence": sequence,
                "task_id": task_id, "command": command, "rule": rule,
                "reason": reason.strip(), "created_at": utc_now(),
                "result_sha256": result_hash, "result_attempt": result["attempt"],
                "partial_reviewed": partial_reviewed,
                "git_head": head, "git_snapshot": snapshot,
            }
            directory = runtime.root / "permission-grants"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{sequence:04d}.json"
            if path.exists():
                raise ValueError(f"permission grant already exists: {path}")
            atomic_write_json(path, grant)
        else:
            if prior["git_head"] != head or prior["git_snapshot"] != snapshot:
                raise ValueError("task files changed since the saved permission grant")
            grant = prior
            path = runtime.root / "permission-grants" / f"{grant['sequence']:04d}.json"
        transition(state, task_id, "ready")
        state["tasks"][task_id]["failure_code"] = None
        refresh(state, graph)
        runtime.write_state(state, graph)
        runtime.event_log(manifest["run_id"]).emit(
            "permission_granted", task_id=task_id, command=command, rule=rule,
            grant_file=str(path),
        )
        return {"status": "granted", "run_id": manifest["run_id"], "task_id": task_id, "rule": rule, "grant_file": str(path)}


def _task_workspace(runtime: Path, manifest: Mapping[str, Any], task_id: str, route: Mapping[str, Any]) -> Path:
    if not manifest["options"]["use_worktrees"]:
        return Path(manifest["workspace"])
    if route["isolation"] == "worktree":
        return runtime / "worktrees" / task_id.lower()
    return runtime / "worktrees" / "integration"


def _source_snapshot(workspace: Path, runtime: Path) -> Dict[str, str]:
    snapshot = git_snapshot(workspace)
    try:
        runtime_prefix = runtime.resolve().relative_to(workspace.resolve()).as_posix() + "/"
    except ValueError:
        return snapshot
    return {path: digest for path, digest in snapshot.items() if not path.startswith(runtime_prefix)}
