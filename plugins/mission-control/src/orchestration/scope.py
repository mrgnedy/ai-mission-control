from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set

from .failures import FailureCode, WorkflowError


def git_head(workspace: Path) -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise WorkflowError(
            FailureCode.CONFIGURATION,
            f"scope validation requires a Git workspace: {(process.stderr or process.stdout).strip()}",
            False,
        )
    return process.stdout.strip()


def git_changed_files(workspace: Path) -> List[str]:
    process = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise WorkflowError(
            FailureCode.CONFIGURATION,
            f"scope validation requires a Git workspace: {(process.stderr or process.stdout).strip()}",
            False,
        )
    entries = process.stdout.split("\0")
    changed: Set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        changed.add(path)
        if "R" in status or "C" in status:
            if index < len(entries) and entries[index]:
                changed.add(entries[index])
                index += 1
    return sorted(changed)


def changed_since(before: Sequence[str], after: Sequence[str]) -> List[str]:
    return sorted(set(after) - set(before))


def git_snapshot(workspace: Path) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for path in git_changed_files(workspace):
        target = workspace / path
        if target.is_file():
            snapshot[path] = hashlib.sha256(target.read_bytes()).hexdigest()
        elif target.exists():
            snapshot[path] = "directory"
        else:
            snapshot[path] = "deleted"
    return snapshot


def changed_snapshots(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def validate_scope(paths: Iterable[str], allowed: Sequence[str], forbidden: Sequence[str]) -> None:
    violations: List[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if any(_match(normalized, pattern) for pattern in forbidden):
            violations.append(normalized)
            continue
        if not any(_match(normalized, pattern) for pattern in allowed):
            violations.append(normalized)
    if violations:
        raise WorkflowError(
            FailureCode.SCOPE,
            f"changed files outside declared scope: {', '.join(sorted(set(violations)))}",
            False,
        )


def _match(path: str, pattern: str) -> bool:
    path_parts = tuple(path.replace("\\", "/").split("/"))
    pattern_parts = tuple(pattern.replace("\\", "/").split("/"))

    @lru_cache(maxsize=None)
    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)
