from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .failures import FailureCode, WorkflowError


def _run(command: list, cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    process = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, check=False)
    if check and process.returncode != 0:
        raise WorkflowError(FailureCode.TOOL, (process.stderr or process.stdout).strip(), False)
    return process


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", value).strip("-").lower()


class WorktreeManager:
    def __init__(self, repository: Path, runtime_root: Path, run_id: str) -> None:
        root = _run(["git", "rev-parse", "--show-toplevel"], repository).stdout.strip()
        self.repository = Path(root)
        self.root = runtime_root / "worktrees"
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = _slug(run_id)
        self.base_ref = _run(["git", "rev-parse", "HEAD"], self.repository).stdout.strip()
        self.task_paths: Dict[str, Path] = {}
        self.integration_path: Optional[Path] = None

    def prepare_task(self, task_id: str) -> Path:
        if task_id in self.task_paths:
            return self.task_paths[task_id]
        path = self.root / _slug(task_id)
        branch = f"ai-{self.run_id}-{_slug(task_id)}"
        if path.exists() and _run(["git", "rev-parse", "--is-inside-work-tree"], path, check=False).returncode == 0:
            self.task_paths[task_id] = path
            return path
        branch_exists = _run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            self.repository,
            check=False,
        ).returncode == 0
        integration = self.ensure_integration()
        start_ref = _run(["git", "rev-parse", "HEAD"], integration).stdout.strip()
        command = (
            ["git", "worktree", "add", str(path), branch]
            if branch_exists
            else ["git", "worktree", "add", "-b", branch, str(path), start_ref]
        )
        _run(command, self.repository)
        self.task_paths[task_id] = path
        return path

    def commit_task(self, task_id: str) -> Optional[str]:
        path = self.task_paths[task_id]
        _run(["git", "add", "-A"], path)
        pending = _run(["git", "diff", "--cached", "--quiet"], path, check=False)
        if pending.returncode == 0:
            return None
        _run(
            [
                "git", "-c", "user.name=AI Orchestrator", "-c",
                "user.email=ai-orchestrator@localhost", "commit", "-m", f"orchestrator: {task_id}",
            ],
            path,
        )
        return _run(["git", "rev-parse", "HEAD"], path).stdout.strip()

    def ensure_integration(self) -> Path:
        if self.integration_path is not None:
            return self.integration_path
        path = self.root / "integration"
        branch = f"ai-{self.run_id}-integration"
        if path.exists() and _run(["git", "rev-parse", "--is-inside-work-tree"], path, check=False).returncode == 0:
            self.integration_path = path
            return path
        branch_exists = _run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            self.repository,
            check=False,
        ).returncode == 0
        command = (
            ["git", "worktree", "add", str(path), branch]
            if branch_exists
            else ["git", "worktree", "add", "-b", branch, str(path), self.base_ref]
        )
        _run(command, self.repository)
        self.integration_path = path
        return path

    def integrate(self, commit: Optional[str]) -> Path:
        path = self.ensure_integration()
        if not commit:
            return path
        process = _run(["git", "cherry-pick", commit], path, check=False)
        if process.returncode != 0:
            _run(["git", "cherry-pick", "--abort"], path, check=False)
            raise WorkflowError(
                FailureCode.MERGE_CONFLICT,
                (process.stderr or process.stdout or "worktree integration conflict").strip(),
                False,
            )
        return path

    def cleanup_task(self, task_id: str) -> bool:
        path = self.task_paths.get(task_id)
        if path is None or not path.exists():
            return True
        status = _run(["git", "status", "--porcelain"], path).stdout.strip()
        if status:
            return False
        _run(["git", "worktree", "remove", str(path)], self.repository)
        self.task_paths.pop(task_id, None)
        return True
