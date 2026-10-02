from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .configuration import configuration_snapshot, load_configuration, load_configuration_snapshot
from .continuation import reopen_task
from .doctor import diagnose_configured, inspect_packaged_assets
from .engine import WorkflowEngine
from .failures import ValidationError
from .io import atomic_write_json, load_data
from .json_schema import validate_with_schema
from .permissions import grant_permission
from .provider_registry import ProviderRegistry
from .recovery import apply_recovery, draft_recovery, effective_delegation, write_recovery_draft
from .report import generate_report
from .review import apply_review
from .router import route_graph
from .runs import (
    create_run_manifest,
    discover_runs,
    resolve_change,
    resolve_run,
    sources_match,
    validate_run_manifest,
)
from .runtime import RuntimeStore
from .validator import validate_decision_dependencies, validate_delegation, validate_required_artifacts


ROOT = Path(__file__).resolve().parents[2]
PACKAGED_CONFIG = ROOT / ".ai/orchestration/orchestration.yaml"
SCHEMAS = ROOT / ".ai/orchestration/schemas"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-orchestrate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--live", action="store_true")
    _config_arguments(doctor)

    route = subparsers.add_parser("route")
    route.add_argument("graph", type=Path, nargs="?")
    route.add_argument("output", type=Path, nargs="?")
    route.add_argument("--change", type=Path)
    _config_arguments(route)

    validate = subparsers.add_parser("validate")
    validate.add_argument("graph", type=Path, nargs="?")
    validate.add_argument("delegation", type=Path, nargs="?")
    validate.add_argument("--change", type=Path)
    _config_arguments(validate)

    run = subparsers.add_parser("run")
    run.add_argument("graph", type=Path, nargs="?")
    run.add_argument("delegation", type=Path, nargs="?")
    run.add_argument("--change", type=Path)
    run.add_argument("--supersedes", metavar="RUN_ID", help="Link this new run to a prior run of the same change")
    _execution_arguments(run)

    resume = subparsers.add_parser("resume")
    resume.add_argument("targets", nargs="*")
    _execution_arguments(resume, resume=True)

    runs = subparsers.add_parser("runs")
    runs.add_argument("--workspace", type=Path, default=Path.cwd())
    runs.add_argument("--all", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("run_id", nargs="?")
    status.add_argument("--workspace", type=Path, default=Path.cwd())

    report = subparsers.add_parser("report")
    report.add_argument("run_id", nargs="?")
    report.add_argument("--workspace", type=Path, default=Path.cwd())
    report.add_argument("--runtime", type=Path, help="Explicit runtime directory for a legacy run")

    grant = subparsers.add_parser("grant-permission")
    grant.add_argument("run_id")
    grant.add_argument("task_id")
    grant.add_argument("--workspace", type=Path, default=Path.cwd())
    grant.add_argument("--reason", required=True)
    grant.add_argument("--partial-reviewed", action="store_true")

    reopen = subparsers.add_parser("reopen")
    reopen.add_argument("run_id")
    reopen.add_argument("task_id")
    reopen.add_argument("--workspace", type=Path, default=Path.cwd())
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--checkpoint", default="")
    reopen.add_argument("--partial-reviewed", action="store_true")
    reopen.add_argument("--resolution", default="")
    reopen.add_argument("--authority", default="")
    reopen.add_argument("--evidence", default="")
    reopen.add_argument("--dry-run", action="store_true")

    recover = subparsers.add_parser("recover")
    recover.add_argument("run_id", nargs="?")
    recover.add_argument("--workspace", type=Path, default=Path.cwd())
    recover.add_argument("--apply", type=Path, metavar="PLAN")
    recover.add_argument("--restore-provider", metavar="PROVIDER")

    review = subparsers.add_parser("review")
    review.add_argument("graph", type=Path)
    review.add_argument("review", type=Path)
    review.add_argument("output_graph", type=Path)
    review.add_argument("output_delegation", type=Path)
    review.add_argument("--remediation-cycles", type=int, default=0)
    _config_arguments(review)
    return parser


def _execution_arguments(parser: argparse.ArgumentParser, *, resume: bool = False) -> None:
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--parallel", action="store_true", default=None if resume else False)
    parser.add_argument("--no-worktrees", action="store_true", default=None if resume else False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-review", action="store_true", default=None if resume else False)
    _config_arguments(parser)


def _config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument(
        "--provider-binary",
        action="append",
        default=[],
        metavar="PROVIDER=PATH",
        help="Override one configured provider binary; may be repeated.",
    )


def _default_config() -> Path:
    project_config = Path.cwd() / ".ai/orchestration/orchestration.yaml"
    return project_config if project_config.is_file() else PACKAGED_CONFIG


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "runs":
        records = discover_runs(args.workspace, SCHEMAS, include_completed=args.all)
        _print({"runs": [_public_record(record) for record in records]})
        return 0
    if args.command == "status":
        record = resolve_run(args.workspace, SCHEMAS, args.run_id)
        _print(_public_record(record))
        return 0
    if args.command == "report":
        if args.runtime is not None:
            if args.run_id is not None:
                raise ValueError("report accepts RUN_ID or --runtime PATH, not both")
            runtime_path = args.runtime.resolve()
        else:
            if args.run_id is None:
                raise ValueError("report requires an exact RUN_ID or --runtime PATH")
            record = resolve_run(args.workspace, SCHEMAS, args.run_id)
            if record["classification"] == "corrupt":
                raise ValueError(f"run metadata is corrupt: {record['reason']}")
            runtime_path = Path(record["runtime"])
        _print(generate_report(runtime_path))
        return 0
    if args.command == "grant-permission":
        record = resolve_run(args.workspace, SCHEMAS, args.run_id)
        if record["classification"] != "permission_required":
            raise ValueError(f"run has no pending permission request: {record['classification']}")
        _print(grant_permission(
            RuntimeStore(Path(record["runtime"])), SCHEMAS, args.task_id, args.reason,
            partial_reviewed=args.partial_reviewed,
        ))
        return 0
    if args.command == "reopen":
        record = resolve_run(args.workspace, SCHEMAS, args.run_id)
        if record["classification"] in {"active", "corrupt", "source_changed", "completed", "recovery_required"}:
            raise ValueError(f"run is not eligible for task reopen: {record['classification']}")
        _print(reopen_task(
            Path(record["runtime"]) if args.dry_run else RuntimeStore(Path(record["runtime"])),
            SCHEMAS, args.task_id, args.reason,
            checkpoint=args.checkpoint, partial_reviewed=args.partial_reviewed,
            resolution=args.resolution, authority=args.authority, evidence=args.evidence,
            dry_run=args.dry_run,
        ))
        return 0
    if args.command == "recover":
        if args.apply is not None and args.restore_provider is not None:
            raise ValueError("--restore-provider creates a draft; use --apply PLAN separately")
        record = resolve_run(args.workspace, SCHEMAS, args.run_id)
        if record["classification"] != "recovery_required":
            raise ValueError(f"run is not eligible for quota recovery: {record['classification']}")
        runtime_path = Path(record["runtime"])
        bundle = load_configuration_snapshot(runtime_path / "configuration.json", SCHEMAS)
        profiles = bundle["profiles"]
        allowed = bundle["execution_strategy"]["allowed_worker_providers"]
        if args.apply is None:
            if record.get("pending_recovery_plan"):
                _print({"status": "apply_pending", "run_id": record["run_id"], "plan": record["pending_recovery_plan"]})
            else:
                plan = draft_recovery(runtime_path, SCHEMAS, profiles, allowed, restore_provider=args.restore_provider)
                path = write_recovery_draft(Path(record["change_dir"]), plan)
                _print({"status": "drafted", "run_id": record["run_id"], "plan": str(path), "tasks": sorted(plan["routes"])})
        else:
            _print(apply_recovery(RuntimeStore(runtime_path), args.apply.resolve(), SCHEMAS, profiles, allowed))
        return 0

    if args.command == "resume":
        execution = _resume_invocation(args)
    elif args.command == "run":
        execution = _run_invocation(args)
    else:
        execution = None

    if execution is not None and execution.get("refusal"):
        _print(execution["refusal"])
        return execution["exit_code"]

    bundle = _bundle_for_command(args, execution)
    profiles = bundle["profiles"]
    matrix = bundle["routing_matrix"]
    providers = bundle["providers"]
    allowed = bundle["execution_strategy"]["allowed_worker_providers"]

    if args.command == "doctor":
        overrides = _binary_overrides(args.provider_binary)
        registry = ProviderRegistry(ROOT, providers, overrides)
        report = diagnose_configured(providers, registry.binaries(), args.live)
        report["methodology"] = bundle["methodology"]["id"]
        report["execution_strategy"] = bundle["execution_strategy"]["id"]
        report["control_plane"] = bundle["orchestration"]["control_plane"]
        report["packaged_assets"] = inspect_packaged_assets(
            ROOT, (provider["adapter"] for provider in providers.values()),
            review=bundle["methodology"]["require_independent_review"],
        )
        _print(report)
        return 0
    if args.command == "route":
        graph_path, output_path = _route_paths(args)
        graph = load_data(graph_path)
        validate_with_schema(graph, SCHEMAS / "task-graph.schema.json")
        delegation = route_graph(graph, profiles, matrix, allowed)
        atomic_write_json(output_path, delegation)
        _print({
            "status": "ok",
            "output": str(output_path),
            "tasks": len(graph["tasks"]),
            "strategy": bundle["execution_strategy"]["id"],
        })
        return 0
    if args.command == "validate":
        graph_path, delegation_path = _validation_paths(args)
        graph = load_data(graph_path)
        delegation = load_data(delegation_path)
        decisions = _load_decisions(graph_path.parent / "decisions.yaml")
        _validate_execution(graph, delegation, profiles, providers, allowed, decisions)
        if args.change is not None:
            _validate_pre_execution(bundle, args.change, graph, delegation, decisions)
        _print({"status": "valid", "workflow_id": graph["workflow"]["id"], "tasks": len(graph["tasks"])})
        return 0
    if args.command in {"run", "resume"}:
        assert execution is not None
        graph = load_data(execution["graph"])
        delegation = (
            effective_delegation(Path(execution["runtime"]), SCHEMAS)
            if args.command == "resume" and execution.get("run_id")
            else load_data(execution["delegation"])
        )
        decision_path = execution.get("decisions")
        decisions = _load_decisions(decision_path) if decision_path is not None else None
        _validate_execution(graph, delegation, profiles, providers, allowed, decisions)
        options = execution["options"]
        if args.command == "run" and args.change is not None:
            _validate_pre_execution(bundle, args.change, graph, delegation, decisions, review=options["review"])
        if args.dry_run:
            _print({
                "status": "dry_run",
                "workflow_id": graph["workflow"]["id"],
                "methodology": bundle["methodology"]["id"],
                "strategy": bundle["execution_strategy"]["id"],
                "runtime": str(execution["runtime"]),
                "supersedes": execution.get("manifest", {}).get("supersedes"),
                "execution_plan": delegation["execution_plan"],
            })
            return 0
        runtime = RuntimeStore(execution["runtime"])
        if execution.get("manifest") is not None:
            if not sources_match(execution["manifest"]):
                raise ValueError("orchestration sources changed while preparing the run; validate again")
            validate_run_manifest(execution["manifest"], SCHEMAS)
            runtime.write_manifest(execution["manifest"])
            if decisions is not None:
                atomic_write_json(runtime.root / "decisions.yaml", decisions)
        registry = ProviderRegistry(ROOT, providers, options["provider_binaries"])
        executors = registry.create_executors(allowed)
        review_profile = bundle["orchestration"]["control_plane"]["review_profile"]
        reviewer = None if not options["review"] else registry.create_reviewer(review_profile, profiles)
        review_provider = bundle["orchestration"]["control_plane"]["provider"]
        engine = WorkflowEngine(
            ROOT,
            graph,
            delegation,
            profiles,
            execution["workspace"],
            runtime,
            executors,
            parallel=options["parallel"],
            use_worktrees=options["use_worktrees"],
            reviewer=reviewer,
            routing_matrix=matrix,
            allowed_executors=list(allowed),
            parallel_executors=bundle["execution_strategy"]["parallel_worktree_providers"],
            configuration_snapshot=configuration_snapshot(bundle),
            review_provider=review_provider,
            review_profile=review_profile,
        )
        state = engine.run(resume=args.command == "resume", run_id=execution.get("run_id", ""))
        _report_after_stop(Path(execution["runtime"]))
        _print(state)
        return 0 if state["status"] == "completed" else 2
    if args.command == "review":
        graph = load_data(args.graph)
        review_value = load_data(args.review)
        validate_with_schema(graph, SCHEMAS / "task-graph.schema.json")
        validate_with_schema(review_value, SCHEMAS / "review.schema.json")
        updated_graph, updated_delegation = apply_review(
            graph, review_value, profiles, matrix, args.remediation_cycles, allowed
        )
        atomic_write_json(args.output_graph, updated_graph)
        atomic_write_json(args.output_delegation, updated_delegation)
        _print({"status": "ok", "tasks": len(updated_graph["tasks"])})
        return 0
    return 1


def _route_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    if args.change is not None:
        if args.graph is not None or args.output is not None:
            raise ValueError("--change cannot be combined with positional route paths")
        graph, delegation = resolve_change(args.change, require_delegation=False)
        return graph, delegation
    if args.graph is None or args.output is None:
        raise ValueError("route requires GRAPH OUTPUT or --change PATH")
    return args.graph.resolve(), args.output.resolve()


def _validation_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    if args.change is not None:
        if args.graph is not None or args.delegation is not None:
            raise ValueError("--change cannot be combined with positional validation paths")
        return resolve_change(args.change)
    if args.graph is None or args.delegation is None:
        raise ValueError("validate requires GRAPH DELEGATION or --change PATH")
    return args.graph.resolve(), args.delegation.resolve()


def _run_invocation(args: argparse.Namespace) -> Dict[str, Any]:
    overrides = _binary_overrides(args.provider_binary)
    options = {
        "parallel": bool(args.parallel),
        "use_worktrees": not bool(args.no_worktrees),
        "review": not bool(args.skip_review),
        "provider_binaries": overrides,
    }
    if args.change is not None:
        if args.graph is not None or args.delegation is not None or args.runtime is not None:
            raise ValueError("run --change cannot be combined with positional paths or --runtime")
        if args.workspace is None:
            raise ValueError("run --change requires --workspace PATH")
        graph_path, delegation_path = resolve_change(args.change)
        graph = load_data(graph_path)
        run_id = str(uuid.uuid4())
        workspace = args.workspace.resolve()
        supersedes = None
        if args.supersedes is not None:
            prior = resolve_run(workspace, SCHEMAS, args.supersedes)
            if prior["classification"] in {"active", "corrupt"}:
                raise ValueError(f"cannot supersede an {prior['classification']} run" if prior["classification"] == "active" else "cannot supersede a corrupt run")
            if prior["workflow_id"] != graph["workflow"]["id"] or Path(prior["change_dir"]).resolve() != args.change.resolve():
                raise ValueError("superseded run must belong to the same workflow and change directory")
            supersedes = prior["run_id"]
        decisions_path = graph_path.parent / "decisions.yaml"
        if not decisions_path.is_file():
            decisions_path = None
        runtime = workspace / ".ai-runtime" / graph["workflow"]["id"] / run_id
        manifest = create_run_manifest(
            run_id,
            graph["workflow"]["id"],
            workspace,
            args.change,
            graph_path,
            delegation_path,
            parallel=options["parallel"],
            use_worktrees=options["use_worktrees"],
            review=options["review"],
            provider_binaries=overrides,
            decisions_path=decisions_path,
            supersedes=supersedes,
        )
        return {
            "graph": graph_path,
            "delegation": delegation_path,
            "workspace": workspace,
            "runtime": runtime,
            "run_id": run_id,
            "manifest": manifest,
            "decisions": decisions_path,
            "options": options,
        }
    if args.supersedes is not None:
        raise ValueError("--supersedes requires run --change")
    if args.graph is None or args.delegation is None or args.workspace is None or args.runtime is None:
        raise ValueError("legacy run requires GRAPH DELEGATION --workspace PATH --runtime PATH")
    return {
        "graph": args.graph.resolve(),
        "delegation": args.delegation.resolve(),
        "workspace": args.workspace.resolve(),
        "runtime": args.runtime.resolve(),
        "options": options,
    }


def _resume_invocation(args: argparse.Namespace) -> Dict[str, Any]:
    if len(args.targets) == 2:
        if args.workspace is None or args.runtime is None:
            raise ValueError("legacy resume requires GRAPH DELEGATION --workspace PATH --runtime PATH")
        options = {
            "parallel": bool(args.parallel),
            "use_worktrees": not bool(args.no_worktrees),
            "review": not bool(args.skip_review),
            "provider_binaries": _binary_overrides(args.provider_binary),
        }
        runtime = args.runtime.resolve()
        runtime_graph = runtime / "task-graph.yaml"
        runtime_delegation = runtime / "delegation.yaml"
        return {
            "graph": runtime_graph if runtime_graph.exists() else Path(args.targets[0]).resolve(),
            "delegation": runtime_delegation if runtime_delegation.exists() else Path(args.targets[1]).resolve(),
            "workspace": args.workspace.resolve(),
            "runtime": runtime,
            "options": options,
        }
    if len(args.targets) > 1 or args.runtime is not None:
        raise ValueError("resume accepts at most one RUN_ID; legacy resume requires two positional paths")
    if any(value is not None for value in (args.parallel, args.no_worktrees, args.skip_review)) or args.provider_binary:
        raise ValueError("simplified resume restores execution options from run.json; do not override them")
    discovery_root = (args.workspace or Path.cwd()).resolve()
    run_id = args.targets[0] if args.targets else None
    record = resolve_run(discovery_root, SCHEMAS, run_id)
    if not record["resumable"]:
        return {
            "refusal": {
                "status": "not_resumed",
                "run_id": record["run_id"],
                "classification": record["classification"],
                "reason": record["reason"],
            },
            "exit_code": 0 if record["classification"] == "completed" else 2,
            "runtime": Path(record["runtime"]),
        }
    manifest = record["manifest"]
    runtime = Path(record["runtime"])
    return {
        "graph": runtime / "task-graph.yaml",
        "delegation": runtime / "delegation.yaml",
        "workspace": Path(manifest["workspace"]).resolve(),
        "runtime": runtime,
        "run_id": manifest["run_id"],
        "options": manifest["options"],
        "decisions": runtime / "decisions.yaml" if "decisions" in manifest["sources"] else None,
    }


def _bundle_for_command(args: argparse.Namespace, execution: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if args.command == "resume" and execution is not None:
        snapshot = Path(execution["runtime"]) / "configuration.json"
        if snapshot.exists():
            return load_configuration_snapshot(snapshot, SCHEMAS)
    return load_configuration(ROOT, args.config)


def _validate_execution(
    graph: Dict[str, Any],
    delegation: Dict[str, Any],
    profiles: Dict[str, Any],
    providers: Dict[str, Any],
    allowed: list,
    decisions: Any = None,
) -> None:
    validate_with_schema(graph, SCHEMAS / "task-graph.schema.json")
    validate_with_schema(delegation, SCHEMAS / "delegation.schema.json")
    validate_delegation(graph, delegation, profiles, providers, allowed)
    if decisions is not None:
        validate_with_schema(decisions, SCHEMAS / "decisions.schema.json")
    validate_decision_dependencies(graph, decisions)


def _validate_pre_execution(
    bundle: Dict[str, Any],
    change_dir: Path,
    graph: Dict[str, Any],
    delegation: Dict[str, Any],
    decisions: Any,
    *,
    review: Optional[bool] = None,
) -> None:
    methodology = bundle["methodology"]
    if not methodology.get("enforce_pre_execution_gates", False):
        return
    validate_required_artifacts(change_dir, methodology["required_artifacts"])
    validate_decision_dependencies(graph, decisions, require_all_resolved=True)
    if review is False and methodology["require_independent_review"]:
        raise ValidationError("--skip-review conflicts with the selected methodology's required independent review")
    adapters = {
        bundle["providers"][route["executor"]]["adapter"]
        for route in delegation["execution_plan"].values()
    }
    assets = inspect_packaged_assets(
        ROOT, adapters,
        review=methodology["require_independent_review"] if review is None else review,
    )
    if assets["missing"]:
        raise ValidationError(f"required packaged worker assets are missing: {assets['missing']}")


def _load_decisions(path: Path) -> Any:
    return load_data(path) if path.is_file() else None


def _binary_overrides(values: Sequence[str]) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for value in values:
        provider, separator, binary = value.partition("=")
        if not separator or not provider or not binary:
            raise ValueError(f"invalid provider binary override: {value!r}; expected PROVIDER=PATH")
        if provider in overrides:
            raise ValueError(f"duplicate provider binary override: {provider}")
        overrides[provider] = binary
    return overrides


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "run_id", "workflow_id", "classification", "resumable", "reason", "updated_at",
        "workspace", "change_dir", "runtime", "state_status", "unavailable_providers", "route_revisions", "effective_route_changes", "pending_recovery_plan",
        "pending_permissions",
        "pending_decisions", "supersedes",
        "task_reopens",
    )
    return {key: record[key] for key in keys if key in record}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _report_after_stop(runtime: Path) -> None:
    try:
        generate_report(runtime)
    except Exception as error:
        print(f"warning: run report could not be generated: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
