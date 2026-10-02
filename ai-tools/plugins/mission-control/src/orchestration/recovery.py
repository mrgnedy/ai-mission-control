from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from .events import EventLog
from .io import atomic_write_json, load_data, utc_now
from .json_schema import validate_with_schema
from .runtime import RuntimeStore
from .scope import git_changed_files, git_head, git_snapshot, validate_scope
from .state import refresh, transition
from .validator import validate_delegation, validate_state


def _revision_paths(runtime: Path) -> list[Path]:
    directory = runtime / "route-revisions"
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    for index, path in enumerate(paths, 1):
        if path.name != f"{index:04d}.json":
            raise ValueError(f"route revisions are not contiguous: {path}")
    return paths


def effective_delegation(runtime: Path, schemas: Path) -> Dict[str, Any]:
    delegation = load_data(runtime / "delegation.yaml")
    run_id = load_data(runtime / "run.json")["run_id"]
    validate_with_schema(delegation, schemas / "delegation.schema.json")
    delegation = copy.deepcopy(delegation)
    for index, path in enumerate(_revision_paths(runtime), 1):
        revision = load_data(path)
        validate_with_schema(revision, schemas / "route-revision.schema.json")
        if revision["sequence"] != index or revision["run_id"] != run_id:
            raise ValueError(f"route revision sequence disagrees with filename: {path}")
        for task_id, change in revision["routes"].items():
            current = delegation["execution_plan"].get(task_id)
            if current is None or current["profile"] not in {change["from_profile"], change["to_route"]["profile"]}:
                raise ValueError(f"route revision {index} does not match task {task_id}")
            delegation["execution_plan"][task_id] = copy.deepcopy(change["to_route"])
    return delegation


def revision_count(runtime: Path) -> int:
    return len(_revision_paths(runtime))


def draft_recovery(
    runtime: Path,
    schemas: Path,
    profiles: Mapping[str, Any],
    allowed_providers: list[str],
    *,
    restore_provider: str | None = None,
) -> Dict[str, Any]:
    manifest = load_data(runtime / "run.json")
    from .runs import sources_match

    if not sources_match(manifest):
        raise ValueError("visible orchestration sources changed; recovery is unsafe")
    graph = load_data(runtime / "task-graph.yaml")
    state = load_data(runtime / "state.json")
    validate_state(state, graph)
    if state["status"] not in {"failed", "blocked"}:
        raise ValueError("recovery requires a stopped failed or blocked run")
    unavailable = set(state.get("unavailable_providers", []))
    if not unavailable:
        raise ValueError("run has no confirmed unavailable provider")
    if restore_provider is not None and restore_provider not in unavailable:
        raise ValueError(f"provider {restore_provider} is not marked unavailable in this run")
    for task_id, item in state["tasks"].items():
        if item["status"] == "running":
            raise ValueError(f"task {task_id} is still running; inspect interruption before recovery")
        if item["status"] == "failed" and item.get("failure_code") != "provider_unavailable":
            raise ValueError(f"task {task_id} failed for another reason; do not auto-recover it")
    delegation = effective_delegation(runtime, schemas)
    validate_delegation(graph, delegation, profiles, allowed_executors=allowed_providers)
    tasks = {task["id"]: task for task in graph["tasks"]}
    routes: Dict[str, Any] = {}
    for task_id, route in delegation["execution_plan"].items():
        item = state["tasks"][task_id]
        if item["status"] == "completed" or route["executor"] not in unavailable:
            continue
        if restore_provider is not None and route["executor"] != restore_provider:
            continue
        if restore_provider is None:
            candidate = next(
                (
                    option for option in route.get("fallbacks", [])
                    if profiles["profiles"][option["profile"]]["executor"] not in unavailable
                    and profiles["profiles"][option["profile"]]["executor"] in allowed_providers
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"task {task_id} has no approved available fallback; ask for a routing decision")
        else:
            candidate = {"profile": route["profile"], "reason": "Provider availability was verified after its limit reset."}
        changed: list[str] = []
        worktree_head = None
        worktree_snapshot: dict[str, str] = {}
        if route["isolation"] == "worktree":
            path = runtime / "worktrees" / _slug(task_id)
            if path.exists():
                worktree_head = git_head(path)
                changed = git_changed_files(path)
                worktree_snapshot = git_snapshot(path)
                validate_scope(changed, tasks[task_id]["scope"]["allowed"], tasks[task_id]["scope"]["forbidden"])
        routes[task_id] = {
            "from_profile": route["profile"],
            "from_status": item["status"],
            "attempts": item["attempts"],
            "to_profile": candidate["profile"],
            "reason": candidate["reason"],
            "changed_files": changed,
            "worktree_head": worktree_head,
            "worktree_snapshot": worktree_snapshot,
            "partial_reviewed": not changed,
        }
    if not routes:
        raise ValueError("no unfinished task uses the unavailable provider")
    plan = {
        "schema_version": "1.0",
        "run_id": manifest["run_id"],
        "workflow_id": manifest["workflow_id"],
        "expected_revision_count": revision_count(runtime),
        "state_updated_at": state["updated_at"],
        "unavailable_providers": sorted(unavailable),
        "routes": routes,
    }
    if restore_provider is not None:
        plan["restore_provider"] = restore_provider
        plan["availability_confirmed"] = False
        plan["availability_evidence"] = None
    return plan


def write_recovery_draft(change_dir: Path, plan: Mapping[str, Any]) -> Path:
    directory = change_dir / "orchestration"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"recovery-{plan['run_id']}-{plan['expected_revision_count'] + 1:04d}"
    index = 1
    while (directory / f"{stem}-draft-{index}.yaml").exists():
        index += 1
    path = directory / f"{stem}-draft-{index}.yaml"
    atomic_write_json(path, plan)
    return path


def apply_recovery(
    runtime: RuntimeStore,
    plan_path: Path,
    schemas: Path,
    profiles: Mapping[str, Any],
    allowed_providers: list[str],
) -> Dict[str, Any]:
    plan = load_data(plan_path)
    validate_with_schema(plan, schemas / "recovery-plan.schema.json")
    with runtime:
        manifest = load_data(runtime.root / "run.json")
        from .runs import sources_match

        if not sources_match(manifest):
            raise ValueError("visible orchestration sources changed; recovery is unsafe")
        if plan["run_id"] != manifest["run_id"] or plan["workflow_id"] != manifest["workflow_id"]:
            raise ValueError("recovery plan does not belong to this run")
        graph = load_data(runtime.root / "task-graph.yaml")
        state = runtime.load_state(graph)
        sequence = plan["expected_revision_count"] + 1
        revision_path = runtime.root / "route-revisions" / f"{sequence:04d}.json"
        if revision_path.exists():
            revision = load_data(revision_path)
            if revision.get("plan") != plan:
                raise ValueError("recovery revision already exists with a different plan")
            if state["status"] not in {"failed", "blocked"}:
                return {"status": "already_recovered", "run_id": manifest["run_id"], "revision": str(revision_path), "tasks": sorted(revision["routes"])}
            if state["updated_at"] != plan["state_updated_at"]:
                raise ValueError("recovery plan is stale; create a new draft")
            for task_id, choice in plan["routes"].items():
                if choice["worktree_head"] is not None:
                    path = runtime.worktrees / _slug(task_id)
                    if not path.exists() or git_head(path) != choice["worktree_head"] or git_snapshot(path) != choice["worktree_snapshot"]:
                        raise ValueError(f"partial edits changed for {task_id}; inspect before recovery")
        else:
            restoring = plan.get("restore_provider")
            if restoring is not None and (
                not plan.get("availability_confirmed")
                or not isinstance(plan.get("availability_evidence"), str)
                or not plan["availability_evidence"].strip()
            ):
                raise ValueError("confirm restored provider availability and record evidence before applying the plan")
            if restoring is None and any(key in plan for key in ("availability_confirmed", "availability_evidence")):
                raise ValueError("availability confirmation applies only to provider restoration")
            expected = draft_recovery(runtime.root, schemas, profiles, allowed_providers, restore_provider=restoring)
            if any(plan[key] != expected[key] for key in (
                "schema_version", "run_id", "workflow_id", "expected_revision_count",
                "state_updated_at", "unavailable_providers",
            )):
                raise ValueError("recovery plan is stale; create a new draft")
            if plan.get("restore_provider") != expected.get("restore_provider"):
                raise ValueError("recovery mode changed; create a new draft")
            if set(plan["routes"]) != set(expected["routes"]):
                raise ValueError("recovery plan must cover every unfinished task on the unavailable provider")
            delegation = effective_delegation(runtime.root, schemas)
            changes: Dict[str, Any] = {}
            for task_id, choice in plan["routes"].items():
                baseline = expected["routes"][task_id]
                for key in ("from_profile", "from_status", "attempts", "changed_files", "worktree_head", "worktree_snapshot"):
                    if choice[key] != baseline[key]:
                        raise ValueError(f"recovery evidence for {task_id} changed; create a new draft")
                original = delegation["execution_plan"][task_id]
                if restoring is not None:
                    if choice["to_profile"] != original["profile"] or choice["reason"] != baseline["reason"]:
                        raise ValueError(f"task {task_id} must retain its route for provider restoration")
                    to_route = copy.deepcopy(original)
                else:
                    approved = {item["profile"]: item for item in original.get("fallbacks", [])}
                    selected = approved.get(choice["to_profile"])
                    if selected is None or choice["reason"] != selected["reason"]:
                        raise ValueError(f"task {task_id} selected an unapproved fallback")
                    profile = profiles["profiles"][choice["to_profile"]]
                    if profile["executor"] in state.get("unavailable_providers", []):
                        raise ValueError(f"task {task_id} fallback provider is unavailable")
                    to_route = {
                        "profile": choice["to_profile"],
                        "executor": profile["executor"],
                        "isolation": profile["isolation"],
                        "model": profile.get("model"),
                        "effort": profile.get("effort"),
                        "timeout_seconds": profile["timeout_seconds"],
                        "max_attempts": profile["max_attempts"],
                        "reason": f"quota recovery: {choice['reason']}",
                    }
                if choice["changed_files"] and not choice["partial_reviewed"]:
                    raise ValueError(f"task {task_id} has partial edits; review them before applying recovery")
                changes[task_id] = {"from_profile": original["profile"], "to_route": to_route}
            revision = {
                "schema_version": "1.0",
                "run_id": manifest["run_id"],
                "sequence": sequence,
                "created_at": utc_now(),
                "plan_path": str(plan_path.resolve()),
                "plan": plan,
                "routes": changes,
            }
            validate_with_schema(revision, schemas / "route-revision.schema.json")
            revision_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(revision_path, revision)
        if plan.get("restore_provider"):
            state["unavailable_providers"] = [
                provider for provider in state.get("unavailable_providers", [])
                if provider != plan["restore_provider"]
            ]
        for task_id in revision["routes"]:
            item = state["tasks"][task_id]
            if item["status"] == "completed":
                raise ValueError(f"completed task {task_id} cannot be recovered")
            if item["status"] == "failed":
                if item.get("failure_code") != "provider_unavailable":
                    raise ValueError(f"task {task_id} failed for an unrelated reason")
                transition(state, task_id, "ready")
        refresh(state, graph)
        runtime.write_state(state, graph)
        EventLog(runtime.logs / "events.jsonl", manifest["run_id"]).emit(
            "recovery_applied", sequence=sequence, tasks=sorted(revision["routes"]),
            unavailable_providers=state.get("unavailable_providers", []),
        )
        return {"status": "recovered", "run_id": manifest["run_id"], "revision": str(revision_path), "tasks": sorted(revision["routes"])}


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower()
