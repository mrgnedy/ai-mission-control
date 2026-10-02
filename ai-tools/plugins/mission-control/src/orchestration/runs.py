from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .io import load_data, utc_now
from .json_schema import validate_with_schema
from .recovery import effective_delegation, revision_count
from .validator import validate_state


def resolve_change(change_dir: Path, *, require_delegation: bool = True) -> Tuple[Path, Path]:
    change = change_dir.resolve()
    orchestration = change / "orchestration"
    graph = orchestration / "task-graph.yaml"
    delegation = orchestration / "delegation.yaml"
    required = (graph, delegation) if require_delegation else (graph,)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"change directory is missing orchestration artifacts: {missing}")
    return graph, delegation


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_run_manifest(
    run_id: str,
    workflow_id: str,
    workspace: Path,
    change_dir: Path,
    graph_path: Path,
    delegation_path: Path,
    *,
    parallel: bool,
    use_worktrees: bool,
    review: bool,
    provider_binaries: Mapping[str, str],
    decisions_path: Optional[Path] = None,
    supersedes: Optional[str] = None,
) -> Dict[str, Any]:
    graph_source = graph_path.resolve()
    delegation_source = delegation_path.resolve()
    manifest = {
        "schema_version": "1.0",
        "run_id": run_id,
        "workflow_id": workflow_id,
        "created_at": utc_now(),
        "workspace": str(workspace.resolve()),
        "change_dir": str(change_dir.resolve()),
        "sources": {
            "task_graph": {"path": str(graph_source), "sha256": file_sha256(graph_source)},
            "delegation": {
                "path": str(delegation_source),
                "sha256": file_sha256(delegation_source),
            },
        },
        "options": {
            "parallel": parallel,
            "use_worktrees": use_worktrees,
            "review": review,
            "provider_binaries": dict(sorted(provider_binaries.items())),
        },
    }
    if decisions_path is not None:
        source = decisions_path.resolve()
        manifest["sources"]["decisions"] = {"path": str(source), "sha256": file_sha256(source)}
    if supersedes is not None:
        manifest["supersedes"] = supersedes
    return manifest


def validate_run_manifest(manifest: Any, schemas: Path) -> None:
    validate_with_schema(manifest, schemas / "run-manifest.schema.json")


def sources_match(manifest: Mapping[str, Any]) -> bool:
    for source in manifest["sources"].values():
        path = Path(source["path"])
        if not path.is_file() or file_sha256(path) != source["sha256"]:
            return False
    return True


def failed_attempt(runtime: Path, state: Mapping[str, Any], task_id: str) -> Tuple[Path, Dict[str, Any], str]:
    """Return the exact failed result named by state, never an older attempt."""
    item = state["tasks"].get(task_id)
    if item is None or item["status"] != "failed" or item["attempts"] < 1:
        raise ValueError(f"task {task_id} has no failed attempt")
    path = runtime / "results" / f"{task_id}-attempt-{item['attempts']}.json"
    if not path.is_file() or (item.get("result_file") and Path(item["result_file"]).resolve() != path.resolve()):
        raise ValueError(f"task {task_id} latest result is missing or disagrees with state")
    result = load_data(path)
    code = (result.get("failure") or {}).get("code")
    if result.get("task_id") != task_id or result.get("attempt") != item["attempts"] or result.get("status") != "failed" or code != item.get("failure_code"):
        raise ValueError(f"task {task_id} latest failed result disagrees with state")
    return path, result, file_sha256(path)


def discover_runs(workspace: Path, schemas: Path, *, include_completed: bool = False) -> List[Dict[str, Any]]:
    runtime_root = workspace.resolve() / ".ai-runtime"
    records = []
    if not runtime_root.is_dir():
        return records
    for manifest_path in sorted(runtime_root.glob("*/*/run.json")):
        record = _inspect_run(manifest_path.parent, schemas)
        if include_completed or record["classification"] != "completed":
            records.append(record)
    records.sort(key=lambda item: (item.get("updated_at", ""), item["run_id"]), reverse=True)
    return records


def resolve_run(workspace: Path, schemas: Path, run_id: Optional[str] = None) -> Dict[str, Any]:
    records = discover_runs(workspace, schemas, include_completed=True)
    if run_id:
        matches = [record for record in records if record["run_id"] == run_id]
        if not matches:
            raise ValueError(f"run not found under {workspace.resolve()}: {run_id}")
        if len(matches) > 1:
            raise ValueError(f"run ID is not unique under {workspace.resolve()}: {run_id}")
        return matches[0]
    unfinished = [record for record in records if record["classification"] != "completed"]
    if not unfinished:
        raise ValueError(f"no unfinished Mission Control runs found under {workspace.resolve()}")
    if len(unfinished) > 1:
        ids = [record["run_id"] for record in unfinished]
        raise ValueError(f"multiple unfinished runs found; specify one run ID: {ids}")
    return unfinished[0]


def _inspect_run(runtime: Path, schemas: Path) -> Dict[str, Any]:
    from .continuation import reopen_count
    fallback_id = runtime.name
    base = {
        "run_id": fallback_id,
        "workflow_id": runtime.parent.name,
        "runtime": str(runtime),
        "classification": "corrupt",
        "resumable": False,
        "reason": "run metadata could not be validated",
        "updated_at": "",
    }
    try:
        manifest = load_data(runtime / "run.json")
        validate_run_manifest(manifest, schemas)
        if Path(manifest["workspace"]).resolve() / ".ai-runtime" not in runtime.parents:
            raise ValueError("run manifest workspace does not contain its runtime directory")
        graph = load_data(runtime / "task-graph.yaml")
        state = load_data(runtime / "state.json")
        validate_state(state, graph)
        if state["run_id"] != manifest["run_id"]:
            raise ValueError("run manifest and state IDs disagree")
        if state["workflow_id"] != manifest["workflow_id"]:
            raise ValueError("run manifest and state workflow IDs disagree")
        delegation = effective_delegation(runtime, schemas)
        if delegation["workflow_id"] != manifest["workflow_id"]:
            raise ValueError("effective delegation workflow ID disagrees with run")
        original_delegation = load_data(runtime / "delegation.yaml")
        changes = {
            task_id: {"original": original_delegation["execution_plan"][task_id]["profile"], "effective": route["profile"]}
            for task_id, route in delegation["execution_plan"].items()
            if task_id in original_delegation["execution_plan"]
            and route["profile"] != original_delegation["execution_plan"][task_id]["profile"]
        }
    except Exception as error:
        base["reason"] = str(error)
        return base

    base.update({
        "run_id": manifest["run_id"],
        "workflow_id": manifest["workflow_id"],
        "workspace": manifest["workspace"],
        "change_dir": manifest["change_dir"],
        "updated_at": state["updated_at"],
        "state_status": state["status"],
        "manifest": manifest,
        "supersedes": manifest.get("supersedes"),
        "unavailable_providers": state.get("unavailable_providers", []),
        "route_revisions": revision_count(runtime),
        "task_reopens": reopen_count(runtime),
        "effective_route_changes": changes,
    })
    if _lock_is_active(runtime / ".lock"):
        return _classified(base, "active", False, "run is locked by a live process")
    if not sources_match(manifest):
        return _classified(base, "source_changed", False, "visible orchestration sources changed or are missing")
    statuses = {item["status"] for item in state["tasks"].values()}
    if state["status"] == "completed":
        return _classified(base, "completed", False, "run already completed")
    if state["status"] == "failed":
        unavailable = set(state.get("unavailable_providers", []))
        unresolved_route = any(
            item["status"] != "completed" and delegation["execution_plan"][task_id]["executor"] in unavailable
            for task_id, item in state["tasks"].items()
        )
        unfinished_quota_failure = any(
            item["status"] == "failed" and item.get("failure_code") == "provider_unavailable"
            for item in state["tasks"].values()
        )
        if unavailable and (unresolved_route or unfinished_quota_failure):
            if base["route_revisions"]:
                last = load_data(runtime / "route-revisions" / f"{base['route_revisions']:04d}.json")
                if state["updated_at"] == last["plan"]["state_updated_at"] and (
                    not unresolved_route or last["plan"].get("restore_provider")
                ):
                    base["pending_recovery_plan"] = last["plan_path"]
                    return _classified(base, "recovery_required", False, "saved recovery revision needs state reopening")
            return _classified(base, "recovery_required", False, "unavailable provider has unfinished work")
    pending_permissions = []
    for task_id, item in state["tasks"].items():
        if item["status"] != "failed" or item.get("failure_code") != "permission_required":
            continue
        result_path = runtime / "results" / f"{task_id}-attempt-{item['attempts']}.json"
        result = load_data(result_path)
        request = result.get("permission_request")
        if not isinstance(request, dict) or not all(request.get(key) for key in ("command", "reason", "checkpoint")):
            return _classified(base, "corrupt", False, f"task {task_id} has an invalid permission request")
        pending_permissions.append({
            "task_id": task_id,
            "command": request["command"],
            "reason": request["reason"],
            "changed_files": result.get("changed_files", []),
        })
    if pending_permissions:
        base["pending_permissions"] = pending_permissions
        return _classified(base, "permission_required", False, "orchestrator must review and grant or reject worker command")
    pending_decisions = []
    for task_id, item in state["tasks"].items():
        if item["status"] != "failed" or item.get("failure_code") != "decision_required":
            continue
        result_path = runtime / "results" / f"{task_id}-attempt-{item['attempts']}.json"
        result = load_data(result_path)
        request = result.get("decision_request")
        if not isinstance(request, dict) or not all(request.get(key) for key in ("question", "evidence", "checkpoint")):
            return _classified(base, "corrupt", False, f"task {task_id} has an invalid decision request")
        pending_decisions.append({"task_id": task_id, "question": request["question"], "changed_files": result.get("changed_files", [])})
    if pending_decisions:
        base["pending_decisions"] = pending_decisions
        return _classified(base, "decision_required", False, "orchestrator must resolve the question and amend affected work")
    if state["status"] == "failed":
        return _classified(base, "failed", False, "failed work requires an explicit repair decision")
    if state["status"] == "blocked":
        return _classified(base, "blocked", False, "blocked work requires an explicit repair decision")
    if state["status"] == "review_required" or "review_required" in statuses:
        return _classified(base, "review_required", True, "review-required work can continue")
    if "running" in statuses:
        return _classified(base, "interrupted", True, "a previous process stopped while work was running")
    if state["status"] in {"pending", "running"} or statuses & {"pending", "ready"}:
        return _classified(base, "resumable", True, "unfinished work can continue")
    return _classified(base, "corrupt", False, f"unsupported runtime state: {state['status']}")


def _classified(record: Dict[str, Any], classification: str, resumable: bool, reason: str) -> Dict[str, Any]:
    record["classification"] = classification
    record["resumable"] = resumable
    record["reason"] = reason
    return record


def _lock_is_active(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        owner = int(lock_path.read_text(encoding="utf-8").strip())
        os.kill(owner, 0)
        return True
    except (OSError, ValueError):
        return False
