from __future__ import annotations

from typing import Any, Mapping, Optional


def maximum_attempts(
    profile: Mapping[str, Any],
    route: Optional[Mapping[str, Any]] = None,
    budgets: Optional[Mapping[str, Any]] = None,
) -> int:
    limits = [profile["max_attempts"]]
    if route is not None and route.get("max_attempts") is not None:
        limits.append(route["max_attempts"])
    if budgets is not None:
        limits.append(budgets["max_attempts_per_task"])
    return min(limits)


def retry_delay_seconds(profile: Mapping[str, Any], attempt: int) -> float:
    base = float(profile.get("retry_backoff_seconds", 0))
    return base * (2 ** max(0, attempt - 1))


def should_retry(
    result: Mapping[str, Any],
    profile: Mapping[str, Any],
    attempt: int,
    route: Optional[Mapping[str, Any]] = None,
    budgets: Optional[Mapping[str, Any]] = None,
) -> bool:
    if result["status"] != "failed" or not result.get("failure"):
        return False
    if attempt >= maximum_attempts(profile, route, budgets):
        return False
    failure = result["failure"]
    return bool(failure.get("retryable")) and failure.get("code") in set(profile.get("retry_on", []))
