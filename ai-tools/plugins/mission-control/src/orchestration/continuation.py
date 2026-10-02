from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from .io import atomic_write_json, load_data, utc_now
from .permissions import _task_workspace
from .recovery import effective_delegation
from .runs import failed_attempt, file_sha256, sources_match
from .scope import _match, git_changed_files, git_head, validate_scope
from .state import refresh, transition
from .validator import validate_state


MAX_CONTEXT = 2000


def _records(runtime: Path) -> list[tuple[Path, Dict[str, Any]]]:
    directory = runtime / "task-reopens"
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    records = []
    for sequence, path in enumerate(paths, 1):
        record = load_data(path)
        if (path.name != f"{sequence:04d}.json" or not isinstance(record, dict) or
                record.get("schema_version") != "1.0" or record.get("sequence") != sequence or
                not isinstance(record.get("path_fingerprint"), dict) or
                not isinstance(record.get("result_attempt"), int) or record["result_attempt"] < 1 or
                not isinstance(record.get("result_sha256"), str)):
            raise ValueError(f"invalid task reopen sequence: {path}")
        records.append((path, record))
    return records


def reopen_count(runtime: Path) -> int:
    return len(_records(runtime))


def _bounded(value: Any, name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > MAX_CONTEXT:
        raise ValueError(f"{name} must be a non-empty string of at most {MAX_CONTEXT} characters")
    return value.strip()


def _path_fingerprint(workspace: Path, paths: list[str]) -> Dict[str, str]:
    snapshot = {}
    root = workspace.resolve()
    for name in sorted(set(paths)):
        relative = Path(name)
        if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe changed path in failed result: {name!r}")
        target = root / relative
        if not target.resolve().is_relative_to(root) or any(
            (root / Path(*relative.parts[:index])).is_symlink()
            for index in range(1, len(relative.parts) + 1)
        ):
            raise ValueError(f"symlink in partial-work path requires a successor run: {name}")
        if target.is_file():
            snapshot[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        elif target.exists():
            snapshot[name] = "directory"
        else:
            snapshot[name] = "deleted"
    return snapshot


def _later_sibling_overlap(runtime: Path, task_id: str, attempt: int, paths: list[str]) -> list[str]:
    """Use ordered completion events; if absent, conservatively inspect all sibling results."""
    owned = set(paths)
    if not owned:
        return []
    events_path = runtime / "logs" / "events.jsonl"
    events = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "task_finished":
                events.append(event)
    marker = next((index for index, event in enumerate(events) if event.get("task_id") == task_id and event.get("attempt") == attempt), None)
    later = events[marker + 1:] if marker is not None else None
    if later is None:
        candidates = [path for path in (runtime / "results").glob("*.json") if not path.name.startswith(f"{task_id}-attempt-")]
    else:
        candidates = [runtime / "results" / f"{event['task_id']}-attempt-{event['attempt']}.json"
                      for event in later if event.get("task_id") != task_id]
    overlap = set()
    for path in candidates:
        if not path.is_file():
            raise ValueError(f"recorded sibling result is missing: {path}")
        overlap.update(owned & set(load_data(path).get("changed_files", [])))
    return sorted(overlap)


def _eligible(runtime: Path, schemas: Path, task_id: str, *, partial_reviewed: bool) -> Dict[str, Any]:
    manifest = load_data(runtime / "run.json")
    if not sources_match(manifest):
        raise ValueError("visible orchestration sources changed; task reopen is unsafe")
    graph = load_data(runtime / "task-graph.yaml")
    state = load_data(runtime / "state.json")
    validate_state(state, graph)
    result_path, result, result_hash = failed_attempt(runtime, state, task_id)
    code = result["failure"]["code"]
    if code in {"provider_unavailable", "permission_required"}:
        raise ValueError(f"{code} has its own recovery command")
    changed = result.get("changed_files", [])
    if changed and not partial_reviewed:
        raise ValueError("task has partial edits; inspect them and pass --partial-reviewed")
    task = next(item for item in graph["tasks"] if item["id"] == task_id)
    validate_scope(changed, task["scope"]["allowed"], task["scope"]["forbidden"])
    route = effective_delegation(runtime, schemas)["execution_plan"][task_id]
    workspace = _task_workspace(runtime, manifest, task_id, route)
    if not workspace.is_dir():
        raise ValueError(f"task workspace is missing: {workspace}")
    dirty = git_changed_files(workspace)
    vanished = sorted(set(changed) - set(dirty))
    if vanished:
        raise ValueError(f"recorded partial task paths no longer exist in the dirty diff: {vanished}")
    if manifest["options"]["use_worktrees"] and route["isolation"] == "worktree":
        unrecorded = sorted(set(dirty) - set(changed))
    else:
        unrecorded = sorted(
            path for path in set(dirty) - set(changed)
            if any(_match(path, pattern) for pattern in task["scope"]["allowed"])
        )
    if unrecorded:
        raise ValueError(f"unattributed dirty task paths require reconciliation before reopen: {unrecorded}")
    if not manifest["options"]["use_worktrees"]:
        overlap = _later_sibling_overlap(runtime, task_id, result["attempt"], changed)
        if overlap:
            raise ValueError(f"shared-workspace sibling edits overlap failed task paths: {overlap}")
    return {
        "manifest": manifest, "graph": graph, "state": state, "result": result,
        "result_path": result_path, "result_hash": result_hash, "workspace": workspace,
        "git_head": git_head(workspace), "path_fingerprint": _path_fingerprint(workspace, changed),
    }


def _context_fields(result: Mapping[str, Any], checkpoint: str, resolution: str, authority: str, evidence: str) -> tuple[str, str, str, str]:
    if result["failure"]["code"] == "decision_required":
        request = result.get("decision_request")
        if not isinstance(request, dict) or not request.get("checkpoint"):
            raise ValueError("decision failure has no worker checkpoint")
        checkpoint = _bounded(request["checkpoint"], "decision checkpoint")
        resolution = _bounded(resolution, "decision resolution")
        evidence = _bounded(evidence, "decision evidence")
        if authority not in {"user", "control_plane", "repository_evidence"}:
            raise ValueError("decision authority must be user, control_plane, or repository_evidence")
    else:
        checkpoint = _bounded(checkpoint, "reviewed checkpoint")
        if resolution or authority or evidence:
            raise ValueError("decision resolution fields require decision_required failure")
    return checkpoint, resolution, authority, evidence


def reopen_task(
    runtime: Any, schemas: Path, task_id: str, reason: str, *, checkpoint: str = "",
    partial_reviewed: bool = False, resolution: str = "", authority: str = "", evidence: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    reason = _bounded(reason, "reopen reason")
    if dry_run:
        root = runtime if isinstance(runtime, Path) else runtime.root
        data = _eligible(root, schemas, task_id, partial_reviewed=partial_reviewed)
        _context_fields(data["result"], checkpoint, resolution, authority, evidence)
        return {"status": "eligible", "run_id": data["manifest"]["run_id"], "task_id": task_id,
                "failed_attempt": data["result"]["attempt"], "changed_files": data["result"].get("changed_files", [])}
    with runtime:
        prior_records = _records(runtime.root)
        current_state = load_data(runtime.root / "state.json")
        existing = next((record for _, record in prior_records if record.get("task_id") == task_id and
                         current_state["tasks"].get(task_id, {}).get("attempts") == record.get("result_attempt")), None)
        if existing is not None:
            state = current_state
            manifest = load_data(runtime.manifest_path)
            if not sources_match(manifest):
                raise ValueError("visible orchestration sources changed; task reopen is unsafe")
            result_path = runtime.results / f"{task_id}-attempt-{existing['result_attempt']}.json"
            if file_sha256(result_path) != existing["result_sha256"]:
                raise ValueError("saved task reopen result changed")
            route = effective_delegation(runtime.root, schemas)["execution_plan"][task_id]
            workspace = _task_workspace(runtime.root, manifest, task_id, route)
            if git_head(workspace) != existing["git_head"] or _path_fingerprint(workspace, list(existing["path_fingerprint"])) != existing["path_fingerprint"]:
                raise ValueError("task files changed since saved reopen")
            if state["tasks"][task_id]["status"] == "failed":
                graph = load_data(runtime.root / "task-graph.yaml")
                transition(state, task_id, "ready")
                state["tasks"][task_id]["failure_code"] = None
                refresh(state, graph)
                runtime.write_state(state, graph)
            elif state["tasks"][task_id]["status"] != "ready":
                raise ValueError("saved task reopen has already been dispatched")
            return {"status": "already_reopened", "run_id": manifest["run_id"], "task_id": task_id,
                    "failed_attempt": existing["result_attempt"]}
        data = _eligible(runtime.root, schemas, task_id, partial_reviewed=partial_reviewed)
        result = data["result"]
        checkpoint, resolution, authority, evidence = _context_fields(result, checkpoint, resolution, authority, evidence)
        records = _records(runtime.root)
        if any(record.get("result_sha256") == data["result_hash"] for _, record in records):
            raise ValueError("this failed result was already reopened")
        sequence = len(records) + 1
        record = {
            "schema_version": "1.0", "sequence": sequence, "run_id": data["manifest"]["run_id"],
            "task_id": task_id, "result_attempt": result["attempt"],
            "result_sha256": data["result_hash"], "reason": reason, "checkpoint": checkpoint,
            "partial_reviewed": partial_reviewed, "created_at": utc_now(),
            "git_head": data["git_head"], "path_fingerprint": data["path_fingerprint"],
        }
        if resolution:
            record["decision"] = {"resolution": resolution, "authority": authority, "evidence": evidence}
        directory = runtime.root / "task-reopens"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sequence:04d}.json"
        if path.exists():
            raise ValueError(f"task reopen record already exists: {path}")
        atomic_write_json(path, record)
        state = data["state"]
        transition(state, task_id, "ready")
        state["tasks"][task_id]["failure_code"] = None
        refresh(state, data["graph"])
        runtime.write_state(state, data["graph"])
        runtime.event_log(data["manifest"]["run_id"]).emit(
            "task_reopened", task_id=task_id, failed_attempt=result["attempt"], record_file=str(path),
        )
        return {"status": "reopened", "run_id": data["manifest"]["run_id"], "task_id": task_id,
                "failed_attempt": result["attempt"], "record_file": str(path)}


def task_continuation_context(runtime: Path, run_id: str, task_id: str, attempt: int, workspace: Path) -> Dict[str, Any]:
    records = _records(runtime)
    relevant = [record for _, record in records if record.get("task_id") == task_id and record.get("result_attempt") == attempt - 1]
    if not relevant:
        return {}
    record = relevant[-1]
    if record.get("run_id") != run_id or record.get("schema_version") != "1.0":
        raise ValueError(f"invalid task reopen record for {task_id}")
    result_path = runtime / "results" / f"{task_id}-attempt-{attempt - 1}.json"
    if not result_path.is_file() or file_sha256(result_path) != record.get("result_sha256"):
        raise ValueError(f"task {task_id} failed result changed after reopen")
    if git_head(workspace) != record.get("git_head") or _path_fingerprint(workspace, list(record["path_fingerprint"])) != record["path_fingerprint"]:
        raise ValueError(f"task {task_id} files changed after reopen")
    context = {"continuation": record["checkpoint"]}
    if record.get("decision"):
        context["decision_resolution"] = record["decision"]["resolution"]
    return context


def task_decision_context(runtime: Path, task: Mapping[str, Any]) -> list[str]:
    references = task.get("decisions", [])
    if not references:
        return []
    manifest_path = runtime / "run.json"
    if manifest_path.is_file():
        manifest = load_data(runtime / "run.json")
        source = manifest.get("sources", {}).get("decisions")
        if source is None or not sources_match(manifest):
            raise ValueError("task references decisions but frozen decision source is missing")
        path = Path(source["path"])
    else:
        path = runtime / "decisions.yaml"
        if not path.is_file():
            raise ValueError("task references decisions but no decision index is available")
    index = load_data(path)
    entries = {entry["id"]: entry for entry in index["decisions"]}
    context = []
    for decision_id in references:
        entry = entries.get(decision_id)
        if entry is None or entry.get("status") != "resolved":
            raise ValueError(f"task {task['id']} references missing or unresolved decision {decision_id}")
        context.append(f"{decision_id}: {_bounded(entry.get('resolution'), 'decision resolution')}")
    if sum(len(item) for item in context) > 4000:
        raise ValueError(f"task {task['id']} referenced decisions exceed the 4000-character prompt limit")
    return context
