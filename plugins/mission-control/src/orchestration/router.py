from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from .validator import validate_graph, validate_profiles


def route_graph(
    graph: Mapping[str, Any],
    profiles: Mapping[str, Any],
    matrix: Mapping[str, Any],
    allowed_executors: Any = None,
) -> Dict[str, Any]:
    validate_graph(graph)
    validate_profiles(profiles)
    rules = sorted(matrix["rules"], key=lambda item: item.get("priority", 0), reverse=True)
    fallback_rules = sorted(matrix.get("fallback_rules", []), key=lambda item: item.get("priority", 0), reverse=True)
    execution_plan: Dict[str, Any] = {}
    allowed = set(allowed_executors) if allowed_executors is not None else None
    for task in graph["tasks"]:
        profile_name, rule = _route_task(task, profiles["profiles"], rules)
        profile = profiles["profiles"][profile_name]
        if allowed is not None and profile["executor"] not in allowed:
            raise ValueError(
                f"task {task['id']} routes to provider {profile['executor']} outside the selected strategy"
            )
        entry = {
            "profile": profile_name,
            "executor": profile["executor"],
            "isolation": profile["isolation"],
            "model": profile.get("model"),
            "effort": profile.get("effort"),
            "timeout_seconds": profile["timeout_seconds"],
            "max_attempts": profile["max_attempts"],
            "reason": f"{rule['id']}: {rule['reason']}",
        }
        # A requested profile is a pin. Only an explicit edit to delegation may add a fallback.
        if not task.get("requested_profile"):
            for fallback_rule in fallback_rules:
                if fallback_rule["from_profile"] != profile_name or not _matches(task, fallback_rule.get("when", {})):
                    continue
                entry["fallbacks"] = [
                    {"profile": name, "reason": f"{fallback_rule['id']}: {fallback_rule['reason']}"}
                    for name in fallback_rule["profiles"]
                ]
                break
        execution_plan[task["id"]] = entry
    return {
        "schema_version": "1.0",
        "workflow_id": graph["workflow"]["id"],
        "execution_plan": execution_plan,
    }


def _route_task(
    task: Mapping[str, Any], profiles: Mapping[str, Any], rules: list
) -> Tuple[str, Mapping[str, Any]]:
    for rule in rules:
        if not _matches(task, rule.get("when", {})):
            continue
        if "profile_from" in rule:
            profile_name = task.get(rule["profile_from"])
            if not profile_name or profile_name not in profiles:
                continue
        else:
            profile_name = rule["profile"]
        if profile_name not in profiles:
            raise ValueError(f"routing rule {rule['id']} references unknown profile {profile_name}")
        return profile_name, rule
    raise ValueError(f"no routing rule matched {task['id']}")


def _matches(task: Mapping[str, Any], conditions: Mapping[str, Any]) -> bool:
    requested = conditions.get("requested_profile")
    if requested == "*" and not task.get("requested_profile"):
        return False
    if requested not in (None, "*") and task.get("requested_profile") != requested:
        return False
    if "type_in" in conditions and task["type"] not in conditions["type_in"]:
        return False
    if "complexity_in" in conditions and task["complexity"] not in conditions["complexity_in"]:
        return False
    if "risk_in" in conditions and task["risk"] not in conditions["risk_in"]:
        return False
    required_any = set(conditions.get("capabilities_any", []))
    if required_any and not (required_any & set(task["capabilities"])):
        return False
    return True
