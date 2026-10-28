"""Durable facts for Agents dispatched by the main Claude session.

The ledger never launches workers or infers whether an Agent is still alive.
Workspace exclusion covers verification as well as worker execution.
"""
from __future__ import annotations

import copy
import fcntl
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .budget import effective_parallel_limit, enforce_expensive_budget
from .configuration import configuration_snapshot, load_configuration_snapshot
from .continuation import task_continuation_context, task_decision_context
from .failures import WorkflowError
from .graph import ready_tasks, task_map
from .io import atomic_write_json, load_data, utc_now
from .json_schema import validate_with_schema
from .models import task_result
from .recovery import effective_delegation
from .review import apply_review
from .runs import file_sha256, sources_match
from .runtime import RuntimeStore
from .scope import changed_snapshots, git_head, git_snapshot
from .state import create_state, extend_for_graph, refresh, transition
from .validator import validate_delegation, validate_graph, validate_result, validate_review


def _snapshot(workspace: Path) -> dict:
    return {path: value for path, value in git_snapshot(workspace).items()
            if path != ".ai-runtime" and not path.startswith(".ai-runtime/")}


@contextmanager
def workspace_transaction(workspace: Path) -> Iterator[None]:
    """Shared checkout mutations use one lock and refuse overlapping operations."""
    directory = workspace.resolve() / ".ai-runtime"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".native.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("shared workspace has another operation in progress") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def native_transaction(runtime: RuntimeStore, workspace: Path | None = None) -> Iterator[None]:
    """Workspace lock precedes the existing per-run transaction lock."""
    if workspace is None:
        workspace = Path(load_data(runtime.manifest_path)["workspace"])
    with workspace_transaction(workspace), runtime:
        yield


def _session_path(runtime: RuntimeStore) -> Path:
    return runtime.root / "native-session.json"


def _save(runtime: RuntimeStore, session: dict) -> None:
    atomic_write_json(_session_path(runtime), session)


def _hashes(runtime: RuntimeStore) -> dict:
    return {name: file_sha256(runtime.root / name)
            for name in ("run.json", "configuration.json", "task-graph.yaml", "delegation.yaml")}


def _load(root: Path, runtime: RuntimeStore) -> tuple[dict, dict, dict, dict, dict]:
    manifest = load_data(runtime.manifest_path)
    if manifest["options"].get("worker_runtime") != "claude-native":
        raise ValueError("session operation requires a claude-native run")
    workspace = Path(manifest["workspace"]).resolve()
    if workspace / ".ai-runtime" not in runtime.root.resolve().parents:
        raise ValueError("native runtime must reside in its workspace .ai-runtime directory")
    if not sources_match(manifest):
        raise ValueError("visible orchestration sources changed; native session refuses dispatch")
    session = load_data(_session_path(runtime))
    _reconcile_review(root, runtime, session)
    if session.get("run_id") != manifest["run_id"] or session.get("input_hashes") != _hashes(runtime):
        raise ValueError("native session configuration or runtime inputs changed")
    schemas = root / ".ai/orchestration/schemas"
    bundle = load_configuration_snapshot(runtime.configuration_path, schemas)
    if bundle["execution_strategy"].get("worker_runtime") != "claude-native":
        raise ValueError("configuration is not a claude-native strategy")
    graph = load_data(runtime.root / "task-graph.yaml")
    validate_graph(graph)
    delegation = effective_delegation(runtime.root, schemas)
    validate_delegation(graph, delegation, bundle["profiles"], allowed_executors=["claude"])
    _validate_routes(delegation, bundle)
    state = runtime.load_state(graph)
    if state["run_id"] != manifest["run_id"]:
        raise ValueError("native session state run identity changed")
    _reconcile(root, runtime, session, graph, state)
    approval_path = runtime.root / "native-approval.json"
    if approval_path.exists() and not session.get("approval"):
        approval = load_data(approval_path)
        claim = session["claims"].get(approval["claim_id"])
        if claim is None or claim.get("review", {}).get("status") != "pass" or any(item["status"] != "completed" for item in state["tasks"].values()):
            raise ValueError("saved native approval is not bound to completed reviewed work")
        _review_unchanged(claim)
        session["approval"] = approval["approval"]
        state["status"] = "completed"
        state["review_status"] = "passed"
        runtime.write_state(state, graph)
        _save(runtime, session)
    return session, bundle, graph, delegation, state


def _validate_routes(delegation: dict, bundle: dict) -> None:
    for route in delegation["execution_plan"].values():
        profile = bundle["profiles"]["profiles"][route["profile"]]
        if route["executor"] != "claude" or route["isolation"] not in {"workspace", "read_only"}:
            raise ValueError("native routes require Claude workspace or read_only isolation")
        if route.get("model", profile.get("model")) not in {"haiku", "sonnet", "opus"}:
            raise ValueError("native routes require a supported model alias")
        if route.get("effort") or profile.get("effort"):
            raise ValueError("native Agent effort inherits the main session; explicit effort is unsupported")


def _active(session: dict) -> list[dict]:
    return [claim for claim in session["claims"].values() if claim["status"] in {"active", "validating"}]


def _partial(runtime: RuntimeStore, session: dict, state: dict) -> list[str]:
    partial = []
    for claim in session["claims"].values():
        if claim["kind"] == "review" and claim.get("review_failure"):
            if git_head(Path(claim["workspace"])) != claim["before_head"] or _snapshot(Path(claim["workspace"])) != claim["before_snapshot"]:
                partial.append("review:" + claim["claim_id"])
            continue
        if claim["kind"] != "task" or claim["status"] != "finished":
            continue
        item = state["tasks"].get(claim["task_id"], {})
        # A reopened partial task remains exclusive until its next claim consumes it.
        if item.get("attempts") != claim["attempt"] or item.get("status") == "completed":
            continue
        receipt = load_data(runtime.root / "native-receipts" / f"{claim['claim_id']}.json")
        result = receipt["result"]
        if result["status"] == "failed" and result["changed_files"]:
            partial.append(claim["task_id"])
    return sorted(set(partial))


def _other_owners(workspace: Path, runtime: RuntimeStore) -> None:
    for path in sorted((workspace / ".ai-runtime").glob("*/*/native-session.json")):
        if path.parent.resolve() == runtime.root.resolve():
            continue
        other = load_data(path)
        if _active(other):
            raise ValueError(f"workspace is owned by active native claims in run {other['run_id']}")
        other_runtime = RuntimeStore(path.parent)
        state = load_data(other_runtime.state_path)
        if _partial(other_runtime, other, state):
            raise ValueError(f"workspace has unresolved partial native work in run {other['run_id']}")


def _refresh(state: dict, graph: dict, session: dict) -> None:
    refresh(state, graph)
    if state["status"] == "completed" and not session.get("approval"):
        state["status"] = "review_required"


def _validate_native_result(root: Path, result: dict) -> None:
    validate_result(result)
    validate_with_schema(result, root / ".ai/orchestration/schemas/result.schema.json")


def _validate_native_review(root: Path, review: dict) -> None:
    validate_review(review)
    validate_with_schema(review, root / ".ai/orchestration/schemas/review.schema.json")


def _review_identity(session: dict, claim: dict, submission: dict) -> None:
    agent_id = submission.get("agent_id")
    if agent_id and any(other["claim_id"] != claim["claim_id"] and other.get("agent_id") == agent_id
                        for other in session["claims"].values()):
        raise ValueError("native reviewer must be a fresh Agent independent of every other claim")


def _reconcile(root: Path, runtime: RuntimeStore, session: dict, graph: dict, state: dict) -> None:
    """Receipt persistence is the commit point; replay never reruns commands."""
    dirty = False
    for claim in session["claims"].values():
        if claim["kind"] != "task":
            continue
        receipt_path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
        item = state["tasks"][claim["task_id"]]
        if claim["status"] in {"active", "validating"} and not receipt_path.exists():
            if item["attempts"] == claim["attempt"] - 1 and item["status"] == "ready":
                transition(state, claim["task_id"], "running")
                dirty = True
            elif item["attempts"] != claim["attempt"] or item["status"] != "running":
                raise ValueError("active native claim disagrees with task state")
            continue
        if not receipt_path.exists():
            raise ValueError("finished native claim has no immutable receipt")
        receipt = load_data(receipt_path)
        result = receipt["result"]
        _validate_native_result(root, result)
        submission = receipt["submission"]
        _terminal(submission, receipt.get("worker_stopped", False), receipt.get("stop_evidence", ""))
        if (receipt.get("run_id") != session["run_id"] or receipt.get("claim_id") != claim["claim_id"]
                or submission["claim_id"] != claim["claim_id"] or result["task_id"] != claim["task_id"]
                or result["attempt"] != claim["attempt"] or result["executor"] != claim["route"]["executor"]
                or result["profile"] != claim["route"]["profile"]
                or result.get("provider_session_id") != submission.get("agent_id")
                or result["status"] not in {"completed", "failed"}):
            raise ValueError("native task receipt identity disagrees with its persisted claim")
        if claim.get("validating_agent_id") != submission.get("agent_id"):
            raise ValueError("native task receipt changed the validating worker's identity")
        result_path = runtime.results / f"{claim['task_id']}-attempt-{claim['attempt']}.json"
        if result_path.exists():
            if load_data(result_path) != result:
                raise ValueError("immutable native task result changed")
        else:
            runtime.write_result(result)
        if item["attempts"] == claim["attempt"] and item["status"] == "running":
            transition(state, claim["task_id"], result["status"], result_file=str(result_path),
                       failure_code=(result.get("failure") or {}).get("code", ""))
            runtime.event_log(session["run_id"]).emit("task_finished", task_id=claim["task_id"],
                                                   attempt=claim["attempt"], status=result["status"],
                                                   claim_id=claim["claim_id"])
            dirty = True
        if claim["status"] != "finished":
            claim.update(status="finished", agent_id=receipt["submission"].get("agent_id"),
                         result_file=str(result_path), stop_evidence=receipt["stop_evidence"])
            dirty = True
    if dirty:
        _refresh(state, graph, session)
        runtime.write_state(state, graph)
        _save(runtime, session)


def start_session(root: Path, bundle: dict, execution: dict) -> dict:
    graph = load_data(Path(execution["graph"]))
    delegation = load_data(Path(execution["delegation"]))
    validate_graph(graph)
    validate_delegation(graph, delegation, bundle["profiles"], allowed_executors=["claude"])
    _validate_routes(delegation, bundle)
    if bundle["execution_strategy"].get("worker_runtime") != "claude-native":
        raise ValueError("session start requires a claude-native strategy")
    options = execution["options"]
    if options.get("use_worktrees") or not options.get("review", True):
        raise ValueError("native sessions require shared workspace and semantic review")
    if not execution.get("manifest"):
        raise ValueError("native session start requires an immutable run manifest")
    manifest = copy.deepcopy(execution["manifest"])
    manifest["options"]["worker_runtime"] = "claude-native"
    workspace = Path(execution["workspace"]).resolve()
    runtime = RuntimeStore(Path(execution["runtime"]).resolve())
    if manifest["workspace"] != str(workspace) or manifest["workflow_id"] != graph["workflow"]["id"] or runtime.root != workspace / ".ai-runtime" / manifest["workflow_id"] / manifest["run_id"]:
        raise ValueError("native execution identity disagrees with its immutable manifest")
    if not sources_match(manifest):
        raise ValueError("visible orchestration sources changed before native start")
    if workspace / ".ai-runtime" not in runtime.root.parents:
        raise ValueError("native runtime must reside in its workspace .ai-runtime directory")
    with native_transaction(runtime, workspace):
        if _session_path(runtime).exists():
            session, _, _, _, _ = _load(root, runtime)
            if load_data(runtime.manifest_path) != manifest or load_data(runtime.configuration_path) != configuration_snapshot(bundle):
                raise ValueError("existing native session inputs differ")
            return _public(runtime, session)
        if runtime.state_path.exists() or runtime.manifest_path.exists():
            raise ValueError("runtime already belongs to another run; native start refuses replacement")
        _other_owners(workspace, runtime)
        runtime.write_manifest(manifest)
        runtime.write_inputs(graph, delegation)
        runtime.write_configuration(configuration_snapshot(bundle))
        if execution.get("decisions"):
            atomic_write_json(runtime.root / "decisions.yaml", load_data(Path(execution["decisions"])))
        session = {"schema_version": "1.0", "run_id": manifest["run_id"], "created_at": utc_now(),
                   "input_hashes": _hashes(runtime), "claims": {}, "review_claim_id": None,
                   "approval": None}
        runtime.write_state(create_state(graph, manifest["run_id"]), graph)
        _save(runtime, session)
        runtime.event_log(manifest["run_id"]).emit("native_session_started")
        return _public(runtime, session)


def _ticket(claim: dict, *, launch_allowed: bool) -> dict:
    keys = ("claim_id", "kind", "run_id", "task_id", "attempt", "cycle", "agent_id",
            "agent_type", "model", "workspace", "prompt")
    ticket = {key: claim[key] for key in keys if key in claim}
    ticket["launch_allowed"] = launch_allowed
    ticket["status"] = "claimed" if claim["status"] == "active" else claim["status"]
    return ticket


def claim_task(root: Path, runtime: RuntimeStore, task_id: str) -> dict:
    with native_transaction(runtime):
        session, bundle, graph, delegation, state = _load(root, runtime)
        if task_id not in state["tasks"]:
            raise ValueError(f"unknown native task: {task_id}")
        prior = next((claim for claim in _active(session) if claim.get("task_id") == task_id), None)
        if prior:
            return _ticket(prior, launch_allowed=False)
        item = state["tasks"][task_id]
        prior = next((claim for claim in session["claims"].values() if claim.get("task_id") == task_id
                      and claim["attempt"] == item["attempts"] and claim["status"] == "finished"), None)
        if prior and item["status"] in {"completed", "failed"}:
            return _ticket(prior, launch_allowed=False)
        workspace = Path(load_data(runtime.manifest_path)["workspace"])
        _other_owners(workspace, runtime)
        active = _active(session)
        if any(claim["kind"] == "review" for claim in active):
            raise ValueError("native review owns the workspace")
        if task_id not in ready_tasks(graph, state):
            raise ValueError("native task is not ready; inspect state or verified reopen")
        partial = _partial(runtime, session, state)
        if partial and (partial != [task_id] or state["tasks"][task_id]["status"] != "ready"):
            raise ValueError(f"partial native work blocks claims until verified reopen: {partial}")
        route = copy.deepcopy(delegation["execution_plan"][task_id])
        profile = bundle["profiles"]["profiles"][route["profile"]]
        limit = effective_parallel_limit(graph, bundle["profiles"])
        manifest = load_data(runtime.manifest_path)
        if not graph["parallelization"]["enabled"] or not manifest["options"]["parallel"]:
            limit = 1
        if len(active) >= limit:
            raise ValueError(f"native active-agent budget reached ({limit})")
        if active and (route["isolation"] != "read_only" or any(claim["route"]["isolation"] != "read_only" for claim in active)):
            raise ValueError("a native writer owns the workspace alone through verification")
        enforce_expensive_budget([claim["task_id"] for claim in active] + [task_id], delegation, bundle["profiles"])
        attempt = state["tasks"][task_id]["attempts"] + 1
        maximum = min(route.get("max_attempts", profile["max_attempts"]), profile["max_attempts"],
                      bundle["profiles"]["budgets"]["max_attempts_per_task"])
        if attempt > maximum:
            raise ValueError(f"native task attempt budget reached ({maximum})")
        task = copy.deepcopy(task_map(graph)[task_id])
        continuation = task_continuation_context(runtime.root, session["run_id"], task_id, attempt, workspace)
        if partial and not continuation:
            raise ValueError("partial native work requires a verified reopen checkpoint")
        route.update(continuation)
        if continuation and "retained_changed_files" not in route:
            route["retained_changed_files"] = load_data(runtime.results / f"{task_id}-attempt-{attempt - 1}.json")["changed_files"]
        route["resolved_decisions"] = task_decision_context(runtime.root, task)
        baseline = _snapshot(workspace)
        before_head = git_head(workspace)
        if active and any(claim["before_head"] != before_head or claim["before_snapshot"] != baseline for claim in active):
            raise ValueError("workspace changed during stable native reader batch")
        claim_id = str(uuid.uuid4())
        claim = {"claim_id": claim_id, "kind": "task", "run_id": session["run_id"], "task_id": task_id,
                 "attempt": attempt, "status": "active", "created_at": utc_now(), "agent_id": None,
                 "agent_type": "mission-control:native-reader" if route["isolation"] == "read_only" else "mission-control:native-writer",
                 "model": route.get("model", profile.get("model")), "workspace": str(workspace),
                 "task": task, "route": route, "before_head": before_head, "before_snapshot": baseline}
        claim["prompt"] = (
            "Execute only this claimed task in the shared workspace. Do not commit, launch other Agents, "
            "or modify .ai-runtime. Return compact JSON with claim_id, agent_id, status (completed|failed), "
            "summary and optional no_op_reason, failure, permission_request or decision_request. "
            "The main session supplies actual returned Agent identity and terminal evidence.\n"
            + json.dumps({key: claim[key] for key in ("claim_id", "run_id", "task_id", "attempt", "task", "route", "workspace")}, indent=2)
        )
        session["claims"][claim_id] = claim
        _save(runtime, session)  # Durable before the parent can launch the Agent.
        transition(state, task_id, "running")
        _refresh(state, graph, session)
        runtime.write_state(state, graph)
        runtime.event_log(session["run_id"]).emit("task_started", task_id=task_id, attempt=attempt, claim_id=claim_id,
                                                executor="claude", profile=route["profile"])
        return _ticket(claim, launch_allowed=True)


def _terminal(submission: dict, worker_stopped: bool, stop_evidence: str, *, review: bool = False) -> None:
    allowed = {"claim_id", "agent_id", "review", "status", "summary", "failure"} if review else {
        "claim_id", "agent_id", "status", "summary", "no_op_reason", "failure", "permission_request", "decision_request", "observed_model"}
    if not isinstance(submission, dict) or set(submission) - allowed:
        raise ValueError("native submission contains untrusted or unsupported fields")
    if not isinstance(submission.get("claim_id"), str) or not submission["claim_id"]:
        raise ValueError("native submission requires claim_id")
    if not worker_stopped or not isinstance(stop_evidence, str) or not stop_evidence.strip():
        raise ValueError("native finish requires explicit worker-stopped evidence")
    if submission.get("agent_id") is not None and (not isinstance(submission["agent_id"], str) or not submission["agent_id"].strip()):
        raise ValueError("native submission has invalid returned Agent identity")
    if not submission.get("agent_id") and submission.get("status") != "failed":
        raise ValueError("completed native work requires actual returned Agent identity")
    if submission.get("failure") is not None:
        failure = submission["failure"]
        if not isinstance(failure, dict) or set(failure) != {"code", "message", "retryable"} or not isinstance(failure["message"], str) or not failure["message"].strip() or not isinstance(failure["retryable"], bool):
            raise ValueError("native failure requires code, message and boolean retryable")
    for name, code in (("permission_request", "permission_required"), ("decision_request", "decision_required")):
        if name in submission and (submission.get("failure") or {}).get("code") != code:
            raise ValueError(f"{name} requires its corresponding native failure code")


def finish_task(root: Path, runtime: RuntimeStore, submission: dict, *, worker_stopped: bool = False,
                stop_evidence: str = "") -> dict:
    _terminal(submission, worker_stopped, stop_evidence)
    with native_transaction(runtime):
        session, bundle, graph, _, state = _load(root, runtime)
        claim = session["claims"].get(submission["claim_id"])
        if claim is None or claim["kind"] != "task":
            raise ValueError("unknown native task claim")
        receipt_path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
        if receipt_path.exists():
            receipt = load_data(receipt_path)
            if receipt["submission"] != submission:
                raise ValueError("native finish receipt is immutable; submission differs")
            return {"status": "already_finished", "claim_id": claim["claim_id"], "result": receipt["result"]}
        reconciling = claim["status"] == "validating"
        if reconciling and (submission.get("status") != "failed" or not stop_evidence.strip().startswith("reconciled:")):
            raise ValueError("verification outcome is unknown; inspect commands and close as failed with stop evidence 'reconciled: ...'; checks will not rerun")
        if claim["status"] not in {"active", "validating"} or state["tasks"][claim["task_id"]]["attempts"] != claim["attempt"]:
            raise ValueError("native task claim is no longer active for this attempt")
        if submission.get("status") not in {"completed", "failed"} or not isinstance(submission.get("summary"), str) or not submission["summary"].strip():
            raise ValueError("native task submission requires completed|failed status and summary")
        agent_id = submission.get("agent_id")
        if reconciling and claim.get("validating_agent_id") != agent_id:
            raise ValueError("reconciliation must preserve the validating worker's actual identity")
        if agent_id and any(other.get("agent_id") == agent_id for other in session["claims"].values() if other["claim_id"] != claim["claim_id"]):
            raise ValueError("returned Agent identity is already bound to another native claim")
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(claim["created_at"].replace("Z", "+00:00"))
        failure = copy.deepcopy(submission.get("failure"))
        if submission["status"] == "failed" and failure is None:
            failure = {"code": "agent_implementation", "message": submission["summary"], "retryable": False}
        if submission["status"] == "completed" and failure is not None:
            raise ValueError("completed native submission cannot contain failure")
        observed_model = submission.get("observed_model")
        if observed_model is not None and (not isinstance(observed_model, str) or not observed_model.strip()):
            raise ValueError("observed_model must come from the actual native Agent result")
        if observed_model in {"haiku", "sonnet", "opus"}:
            raise ValueError("observed_model must be a resolved model ID; omit it when only the requested alias is visible")
        result = task_result(claim["task_id"], submission["status"], submission["summary"], "claude",
                             claim["route"]["profile"], claim["attempt"], int(elapsed.total_seconds() * 1000),
                             failure=failure, model=observed_model, provider_session_id=agent_id)
        result["requested_model"] = claim["model"]
        if observed_model is None:
            result["warnings"].append("Native Agent model override was requested; actual model was not observed.")
        if reconciling:
            result["warnings"].append("Prior verification outcome was unknown; parent reconciled it as failed without rerunning commands.")
        for name in ("no_op_reason", "permission_request", "decision_request"):
            if name in submission:
                result[name] = copy.deepcopy(submission[name])
        _validate_native_result(root, result)
        from .verification import collect_task_evidence
        profile = bundle["profiles"]["profiles"][claim["route"]["profile"]]
        if not reconciling:
            claim["status"] = "validating"
            claim["validating_agent_id"] = agent_id
            claim["validation_started_at"] = utc_now()
            _save(runtime, session)
        result = collect_task_evidence(claim["task"], claim["route"], Path(claim["workspace"]),
                                       claim["before_head"], claim["before_snapshot"], result,
                                       timeout_seconds=claim["route"].get("timeout_seconds", profile["timeout_seconds"]),
                                       force_commands=True)
        _validate_native_result(root, result)
        receipt = {"schema_version": "1.0", "claim_id": claim["claim_id"], "run_id": session["run_id"],
                   "submission": copy.deepcopy(submission), "worker_stopped": True,
                   "stop_evidence": stop_evidence.strip(), "finished_at": utc_now(), "result": result}
        atomic_write_json(receipt_path, receipt)
        _reconcile(root, runtime, session, graph, state)
        return {"status": "finished", "claim_id": claim["claim_id"], "result": result}


def _review_unchanged(claim: dict) -> None:
    workspace = Path(claim["workspace"])
    if git_head(workspace) != claim["before_head"] or _snapshot(workspace) != claim["before_snapshot"]:
        raise ValueError("workspace changed during native review or before parent approval")


def _reconcile_review(root: Path, runtime: RuntimeStore, session: dict) -> None:
    claim = session["claims"].get(session.get("review_claim_id"))
    if claim is None:
        return
    path = runtime.root / "native-receipts" / f"{claim['claim_id']}.json"
    if not path.exists():
        if claim["status"] == "finished":
            raise ValueError("finished native review has no immutable receipt")
        return
    receipt = load_data(path)
    _terminal(receipt["submission"], True, receipt["stop_evidence"], review=True)
    _review_identity(session, claim, receipt["submission"])
    if receipt["submission"]["claim_id"] != claim["claim_id"]:
        raise ValueError("native review receipt identity disagrees with claim")
    if bool(receipt.get("failed_review")) != (receipt["submission"].get("status") == "failed"):
        raise ValueError("native review receipt outcome disagrees with submission")
    if receipt.get("failed_review") and "review" in receipt["submission"]:
        raise ValueError("failed native review receipt cannot contain a passing review")
    if not receipt.get("failed_review") and set(receipt["submission"]) - {"claim_id", "agent_id", "review"}:
        raise ValueError("passing native review receipt contains unsupported fields")
    if receipt.get("failed_review"):
        _validate_native_result(root, task_result("TASK-REVIEW", "failed", receipt["submission"]["summary"], "claude",
                                                claim["route"]["profile"], claim["attempt"], 0, failure=receipt["failure"]))
    else:
        _validate_native_review(root, receipt["submission"]["review"])
    if claim["status"] == "finished":
        expected = claim.get("review_failure") if receipt.get("failed_review") else claim.get("review")
        actual = receipt["failure"] if receipt.get("failed_review") else receipt["submission"]["review"]
        if expected != actual or claim.get("agent_id") != receipt["submission"].get("agent_id"):
            raise ValueError("immutable native review receipt changed")
        return
    # Only the recorded receipt may explain input drift during remediation replay.
    for name, key in (("task-graph.yaml", "graph"), ("delegation.yaml", "delegation")):
        current = runtime.root / name
        if file_sha256(current) != session["input_hashes"][name] and load_data(current) != receipt[key]:
            raise ValueError("runtime inputs changed outside recorded native remediation")
    for name in ("configuration.json", "run.json"):
        if file_sha256(runtime.root / name) != session["input_hashes"][name]:
            raise ValueError("native configuration or manifest changed during review")
    if receipt.get("failed_review"):
        state = load_data(runtime.state_path)
        state["review_status"] = "failed"
        state["status"] = "review_required"
        runtime.write_state(state, receipt["graph"])
        claim.update(status="finished", agent_id=receipt["submission"].get("agent_id"),
                     review_failure=receipt["failure"], stop_evidence=receipt["stop_evidence"])
        _save(runtime, session)
        runtime.event_log(session["run_id"]).emit("review_failed", claim_id=claim["claim_id"], cycle=claim["cycle"],
                                                failure=receipt["failure"])
        return
    _review_unchanged(claim)
    review = receipt["submission"]["review"]
    graph = receipt["graph"]
    review_path = runtime.reviews / f"review-{claim['cycle']}.json"
    if review_path.exists() and load_data(review_path) != review:
        raise ValueError("immutable native review changed")
    runtime.write_review(review, claim["cycle"])
    state = load_data(runtime.state_path)
    state["last_review_file"] = str(review_path)
    if review["status"] == "remediation_required":
        runtime.write_inputs(graph, receipt["delegation"])
        state["remediation_cycles"] = max(state["remediation_cycles"], claim["cycle"])
        state["review_status"] = "remediation_required"
        extend_for_graph(state, graph)
    else:
        state["review_status"] = "passed"
        state["status"] = "review_required"
    runtime.write_state(state, graph)
    claim.update(status="finished", agent_id=receipt["submission"]["agent_id"], review=copy.deepcopy(review),
                 stop_evidence=receipt["stop_evidence"])
    session["input_hashes"] = _hashes(runtime)
    _save(runtime, session)


def session_review(root: Path, runtime: RuntimeStore, submission: dict | None = None, *, approval: str = "",
                   worker_stopped: bool = False, stop_evidence: str = "") -> dict:
    if submission is not None:
        _terminal(submission, worker_stopped, stop_evidence, review=True)
    with native_transaction(runtime):
        session, bundle, graph, delegation, state = _load(root, runtime)
        workspace = Path(load_data(runtime.manifest_path)["workspace"])
        _other_owners(workspace, runtime)
        existing = session["claims"].get(session.get("review_claim_id"))
        if submission is not None and existing and existing["status"] == "finished" and submission["claim_id"] == existing["claim_id"]:
            receipt = load_data(runtime.root / "native-receipts" / f"{existing['claim_id']}.json")
            if receipt["submission"] != submission:
                raise ValueError("native review receipt is immutable")
            if receipt.get("failed_review"):
                return {"status": "review_failed", "claim_id": existing["claim_id"], "failure": receipt["failure"]}
            _validate_native_review(root, receipt["submission"]["review"])
            if existing["review"]["status"] == "remediation_required":
                return {"status": "remediation_required", "claim_id": existing["claim_id"], "review": existing["review"],
                        "ready_tasks": ready_tasks(graph, state)}
        if any(item["status"] != "completed" for item in state["tasks"].values()):
            raise ValueError("native review requires all tasks completed")
        if submission is None and approval:
            if existing is None or existing["status"] != "finished" or existing.get("review", {}).get("status") != "pass":
                raise ValueError("parent approval requires a completed passing native review")
            _review_unchanged(existing)
            return _approve(runtime, session, graph, state, approval)
        if submission is None:
            if existing and existing["status"] == "active":
                return _ticket(existing, launch_allowed=False)
            if session.get("approval"):
                return _public(runtime, session)
            if existing and existing["status"] == "finished" and existing.get("review", {}).get("status") == "pass":
                return {"status": "approval_required", "claim_id": existing["claim_id"]}
            if _active(session):
                raise ValueError("native workers still own the workspace")
            profile_name = bundle["orchestration"]["control_plane"]["review_profile"]
            profile = bundle["profiles"]["profiles"][profile_name]
            if profile["executor"] != "claude" or profile["isolation"] != "read_only" or profile.get("effort"):
                raise ValueError("native review requires a Claude read_only profile with inherited effort")
            if profile.get("model") not in {"haiku", "sonnet", "opus"}:
                raise ValueError("native review requires a supported model alias")
            if _partial(runtime, session, state):
                raise ValueError("failed native review changed workspace; reconcile its edits before another review")
            cycle = state["remediation_cycles"] + 1
            attempts = sum(1 for claim in session["claims"].values() if claim["kind"] == "review" and claim["cycle"] == cycle)
            maximum = min(profile["max_attempts"], bundle["profiles"]["budgets"]["max_attempts_per_task"])
            if attempts >= maximum:
                raise ValueError(f"native review attempt budget reached ({maximum})")
            if profile["expense"] == "expensive" and bundle["profiles"]["budgets"]["max_expensive_tasks"] < 1:
                raise ValueError("native review exceeds active expensive-agent budget")
            claim_id = str(uuid.uuid4())
            claim = {"claim_id": claim_id, "kind": "review", "run_id": session["run_id"], "status": "active",
                     "created_at": utc_now(), "agent_id": None, "cycle": cycle, "attempt": attempts + 1,
                     "agent_type": "mission-control:native-reader", "model": profile["model"],
                     "workspace": str(workspace), "before_head": git_head(workspace), "before_snapshot": _snapshot(workspace),
                     "route": {"profile": profile_name, "isolation": "read_only"}}
            claim["prompt"] = "Independently review completed work without edits or commits. Return compact JSON with claim_id, agent_id, review (schema_version, status pass|remediation_required, summary, issues).\n" + json.dumps({
                "claim_id": claim_id, "graph": graph,
                "results": [load_data(Path(item["result_file"])) for item in state["tasks"].values()],
            }, indent=2)
            session["claims"][claim_id] = claim
            session["review_claim_id"] = claim_id
            _save(runtime, session)
            return _ticket(claim, launch_allowed=True)
        if existing is None or submission["claim_id"] != existing["claim_id"]:
            raise ValueError("unknown or superseded native review claim")
        _review_identity(session, existing, submission)
        if submission.get("status") == "failed":
            if "review" in submission or not isinstance(submission.get("summary"), str) or not submission["summary"].strip():
                raise ValueError("failed review requires summary and cannot contain a passing review")
            failure = submission.get("failure") or {"code": "review", "message": submission["summary"], "retryable": False}
            _validate_native_result(root, task_result("TASK-REVIEW", "failed", submission["summary"], "claude", existing["route"]["profile"],
                                                     existing["attempt"], 0, failure=failure))
            receipt_path = runtime.root / "native-receipts" / f"{existing['claim_id']}.json"
            receipt = {"submission": copy.deepcopy(submission), "stop_evidence": stop_evidence.strip(),
                       "failed_review": True, "failure": copy.deepcopy(failure), "graph": graph, "delegation": delegation,
                       "changed_files": changed_snapshots(existing["before_snapshot"], _snapshot(workspace)),
                       "after_head": git_head(workspace)}
            atomic_write_json(receipt_path, receipt)
            _reconcile_review(root, runtime, session)
            return {"status": "review_failed", "claim_id": existing["claim_id"], "failure": failure}
        if "status" in submission or "summary" in submission or "failure" in submission:
            raise ValueError("successful review submission requires only claim_id, agent_id and review")
        _review_unchanged(existing)
        review = submission.get("review")
        _validate_native_review(root, review)
        receipt_path = runtime.root / "native-receipts" / f"{existing['claim_id']}.json"
        if receipt_path.exists():
            receipt = load_data(receipt_path)
            if receipt["submission"] != submission:
                raise ValueError("native review receipt is immutable")
        else:
            updated_graph, updated_delegation = apply_review(graph, review, bundle["profiles"], bundle["routing_matrix"],
                                                            state["remediation_cycles"], ["claude"])
            _validate_routes(updated_delegation, bundle)
            # Existing routes, including explicitly selected profiles, remain frozen.
            for task_id, route in delegation["execution_plan"].items():
                updated_delegation["execution_plan"][task_id] = route
            receipt = {"submission": copy.deepcopy(submission), "stop_evidence": stop_evidence.strip(),
                       "graph": updated_graph, "delegation": updated_delegation}
            atomic_write_json(receipt_path, receipt)
        _reconcile_review(root, runtime, session)
        state = load_data(runtime.state_path)
        if review["status"] == "pass":
            if approval:
                return _approve(runtime, session, graph, state, approval)
            return {"status": "approval_required", "claim_id": existing["claim_id"], "review": review}
        return {"status": "remediation_required", "claim_id": existing["claim_id"], "review": review,
                "ready_tasks": ready_tasks(receipt["graph"], state)}


def _approve(runtime: RuntimeStore, session: dict, graph: dict, state: dict, approval: str) -> dict:
    if not isinstance(approval, str) or not approval.strip():
        raise ValueError("parent final approval requires a reason")
    if session.get("approval") and session["approval"]["reason"] != approval.strip():
        raise ValueError("parent approval is immutable")
    approval_path = runtime.root / "native-approval.json"
    if approval_path.exists():
        saved = load_data(approval_path)
        if saved["claim_id"] != session["review_claim_id"] or saved["approval"]["reason"] != approval.strip():
            raise ValueError("parent approval receipt is immutable")
        session["approval"] = saved["approval"]
    else:
        session["approval"] = {"reason": approval.strip(), "created_at": utc_now()}
        atomic_write_json(approval_path, {"claim_id": session["review_claim_id"], "approval": session["approval"]})
    state["status"] = "completed"
    state["review_status"] = "passed"
    runtime.write_state(state, graph)
    _save(runtime, session)
    runtime.event_log(session["run_id"]).emit("workflow_finished", status="completed", review_status="passed")
    return _public(runtime, session)


def _public(runtime: RuntimeStore, session: dict) -> dict:
    graph = load_data(runtime.root / "task-graph.yaml")
    state = load_data(runtime.state_path)
    partial = _partial(runtime, session, state)
    active = _active(session)
    candidates = ready_tasks(graph, state)
    bundle = load_data(runtime.configuration_path)
    delegation = load_data(runtime.root / "delegation.yaml")
    manifest = load_data(runtime.manifest_path)
    limit = effective_parallel_limit(graph, bundle["profiles"])
    if not graph["parallelization"]["enabled"] or not manifest["options"]["parallel"]:
        limit = 1
    if any(claim["kind"] == "review" or claim["route"]["isolation"] != "read_only" for claim in active) or len(active) >= limit:
        candidates = []
    elif active:
        candidates = [task_id for task_id in candidates if delegation["execution_plan"][task_id]["isolation"] == "read_only"]
    if partial:
        candidates = [task_id for task_id in candidates if partial == [task_id] and state["tasks"][task_id]["status"] == "ready"]
    permitted = []
    for task_id in candidates:
        route = delegation["execution_plan"][task_id]
        profile = bundle["profiles"]["profiles"][route["profile"]]
        maximum = min(route.get("max_attempts", profile["max_attempts"]), profile["max_attempts"],
                      bundle["profiles"]["budgets"]["max_attempts_per_task"])
        if state["tasks"][task_id]["attempts"] >= maximum:
            continue
        try:
            enforce_expensive_budget([claim["task_id"] for claim in active] + [task_id], delegation, bundle["profiles"])
        except WorkflowError:
            continue
        permitted.append(task_id)
    return {"worker_runtime": "claude-native", "run_id": session["run_id"], "runtime": str(runtime.root),
            "status": state["status"], "review_status": state["review_status"],
            "ready_tasks": permitted,
            "claims": [{key: claim[key] for key in ("claim_id", "kind", "task_id", "attempt", "cycle",
                        "status", "agent_id", "agent_type", "model", "result_file", "validation_started_at") if key in claim}
                       for claim in session["claims"].values()],
            "active_claims": [claim["claim_id"] for claim in active], "partial_block": partial,
            "approval": session.get("approval"), "tasks": state["tasks"]}


def session_status(runtime: Path) -> dict:
    store = RuntimeStore(runtime)
    return _public(store, load_data(_session_path(store)))
