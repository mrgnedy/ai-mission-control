from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, Mapping

from .failures import ValidationError
from .io import load_data
from .json_schema import validate_with_schema
from .validator import validate_profiles


IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SNAPSHOT_KEYS = {
    "schema_version",
    "orchestration",
    "methodology",
    "execution_strategy",
    "providers",
    "profiles",
    "routing_matrix",
}


def load_configuration(root: Path, config_path: Path) -> Dict[str, Any]:
    config_path = config_path.resolve()
    base = config_path.parent
    schemas = root / ".ai/orchestration/schemas"
    orchestration = load_data(config_path)
    validate_with_schema(orchestration, schemas / "orchestration.schema.json")

    methodology_id = _identifier(orchestration["methodology"], "methodology")
    strategy_id = _identifier(orchestration["execution_strategy"], "execution_strategy")
    methodology = load_data(base / "methodologies" / f"{methodology_id}.yaml")
    strategy = load_data(base / "strategies" / f"{strategy_id}.yaml")
    validate_with_schema(methodology, schemas / "methodology.schema.json")
    validate_with_schema(strategy, schemas / "execution-strategy.schema.json")

    providers: Dict[str, Any] = {}
    for path in sorted((base / "providers").glob("*.yaml")):
        provider = load_data(path)
        validate_with_schema(provider, schemas / "provider.schema.json")
        provider_id = provider["id"]
        if path.stem != provider_id:
            raise ValidationError(f"provider filename {path.name} disagrees with id {provider_id}")
        if provider_id in providers:
            raise ValidationError(f"duplicate provider id: {provider_id}")
        providers[provider_id] = provider

    routing_name = strategy["routing_matrix"]
    if Path(routing_name).name != routing_name:
        raise ValidationError("execution strategy routing_matrix must be a local filename")
    bundle = {
        "schema_version": "1.0",
        "orchestration": orchestration,
        "methodology": methodology,
        "execution_strategy": strategy,
        "providers": providers,
        "profiles": load_data(base / "profiles.yaml"),
        "routing_matrix": load_data(base / routing_name),
    }
    validate_configuration_bundle(bundle, schemas)
    return bundle


def load_configuration_snapshot(path: Path, schemas: Path) -> Dict[str, Any]:
    bundle = load_data(path)
    validate_configuration_bundle(bundle, schemas)
    return bundle


def configuration_snapshot(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(bundle))


def validate_configuration_bundle(bundle: Any, schemas: Path) -> None:
    if not isinstance(bundle, Mapping) or set(bundle) != SNAPSHOT_KEYS:
        raise ValidationError("configuration snapshot has invalid fields")
    if bundle["schema_version"] != "1.0":
        raise ValidationError("configuration snapshot version must be 1.0")
    validate_with_schema(bundle["orchestration"], schemas / "orchestration.schema.json")
    validate_with_schema(bundle["methodology"], schemas / "methodology.schema.json")
    validate_with_schema(bundle["execution_strategy"], schemas / "execution-strategy.schema.json")
    if bundle["methodology"]["id"] != bundle["orchestration"]["methodology"]:
        raise ValidationError("selected methodology id does not match its manifest")
    if bundle["execution_strategy"]["id"] != bundle["orchestration"]["execution_strategy"]:
        raise ValidationError("selected execution strategy id does not match its manifest")

    providers = bundle["providers"]
    if not isinstance(providers, Mapping) or not providers:
        raise ValidationError("configuration requires at least one provider")
    for provider_id, provider in providers.items():
        validate_with_schema(provider, schemas / "provider.schema.json")
        if provider_id != provider["id"]:
            raise ValidationError(f"provider key {provider_id} disagrees with its manifest")
        rules = provider.get("allowed_bash_rules", [])
        if rules and provider["adapter"] != "claude-cli":
            raise ValidationError(f"provider {provider_id} cannot configure Claude Bash rules")
        for rule in rules:
            if rule in {"Bash(*)", "Bash(:*)"} or len(rule) > 506 or any(token in rule for token in (",", "\n", "\r")) or not rule[5:-1].strip():
                raise ValidationError(f"provider {provider_id} has an unsafe or ambiguous Bash allow rule: {rule}")

    validate_profiles(bundle["profiles"], providers)
    profiles = bundle["profiles"]["profiles"]
    orchestration = bundle["orchestration"]
    control = orchestration["control_plane"]
    control_provider = control["provider"]
    default_control_provider = bundle["methodology"]["default_control_plane"]
    if default_control_provider not in providers:
        raise ValidationError(
            f"methodology default control-plane provider is not registered: {default_control_provider}"
        )
    if control_provider not in providers:
        raise ValidationError(f"control-plane provider is not registered: {control_provider}")
    for field in ("planning_profile", "review_profile", "final_approval_profile"):
        profile_name = control[field]
        if profile_name not in profiles:
            raise ValidationError(f"control_plane.{field} references unknown profile {profile_name}")
        if profiles[profile_name]["executor"] != control_provider:
            raise ValidationError(f"control_plane.{field} must use provider {control_provider}")
    for field in ("review_profile", "final_approval_profile"):
        if profiles[control[field]]["isolation"] != "read_only":
            raise ValidationError(f"control-plane {field} must be read_only")

    allowed = set(bundle["execution_strategy"]["allowed_worker_providers"])
    unknown = allowed - set(providers)
    if unknown:
        raise ValidationError(f"execution strategy references unknown providers: {sorted(unknown)}")
    if control_provider not in allowed:
        raise ValidationError("execution strategy must allow the control-plane provider")
    parallel = set(bundle["execution_strategy"]["parallel_worktree_providers"])
    if not parallel <= allowed:
        raise ValidationError("parallel worktree providers must be allowed by the strategy")
    for provider_id in parallel:
        if not providers[provider_id]["capabilities"]["file_editing"]:
            raise ValidationError(f"parallel provider {provider_id} does not support file editing")

    matrix = bundle["routing_matrix"]
    if not isinstance(matrix, Mapping) or matrix.get("schema_version") != "1.0":
        raise ValidationError("routing matrix has invalid version")
    rules = matrix.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValidationError("routing matrix rules must be a non-empty array")
    for rule in rules:
        if "profile" in rule:
            profile_name = rule["profile"]
            if profile_name not in profiles:
                raise ValidationError(f"routing rule references unknown profile {profile_name}")
            provider = profiles[profile_name]["executor"]
            if provider not in allowed:
                raise ValidationError(
                    f"routing rule {rule.get('id', '<unknown>')} uses provider {provider} outside the strategy"
                )
    fallback_rules = matrix.get("fallback_rules", [])
    if not isinstance(fallback_rules, list):
        raise ValidationError("routing matrix fallback_rules must be an array")
    for rule in fallback_rules:
        if not isinstance(rule, Mapping) or not all(key in rule for key in ("id", "from_profile", "profiles", "reason")):
            raise ValidationError("fallback rule requires id, from_profile, profiles, and reason")
        if not isinstance(rule["id"], str) or not rule["id"] or not isinstance(rule["reason"], str) or not rule["reason"].strip():
            raise ValidationError("fallback rule id and reason must be non-empty strings")
        if not isinstance(rule.get("priority", 0), int) or isinstance(rule.get("priority", 0), bool):
            raise ValidationError(f"fallback rule {rule['id']} has invalid priority")
        when = rule.get("when", {})
        if not isinstance(when, Mapping) or any(
            key not in {"type_in", "risk_in", "complexity_in", "capabilities_any"}
            or not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
            for key, value in when.items()
        ):
            raise ValidationError(f"fallback rule {rule['id']} has invalid conditions")
        source = rule["from_profile"]
        if source not in profiles:
            raise ValidationError(f"fallback rule {rule['id']} references unknown source profile {source}")
        if profiles[source]["executor"] not in allowed:
            raise ValidationError(f"fallback rule {rule['id']} source is outside the strategy")
        candidates = rule["profiles"]
        if not isinstance(candidates, list) or not candidates or not all(isinstance(item, str) for item in candidates) or len(set(candidates)) != len(candidates):
            raise ValidationError(f"fallback rule {rule['id']} needs unique ordered profiles")
        for candidate in candidates:
            if candidate not in profiles:
                raise ValidationError(f"fallback rule {rule['id']} references unknown profile {candidate}")
            if candidate == source or profiles[candidate]["isolation"] != profiles[source]["isolation"]:
                raise ValidationError(f"fallback rule {rule['id']} must change profile without changing isolation")
            if profiles[candidate]["executor"] not in allowed:
                raise ValidationError(f"fallback rule {rule['id']} uses a provider outside the strategy")


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValidationError(f"{field} must be a safe identifier")
    return value
