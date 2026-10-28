from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from .events import EventLog
from .io import atomic_write_json, load_data
from .validator import validate_result, validate_review, validate_state


class RuntimeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.results = root / "results"
        self.reviews = root / "reviews"
        self.logs = root / "logs"
        self.worktrees = root / "worktrees"
        for directory in (self.results, self.reviews, self.logs, self.worktrees):
            directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = root / ".lock"
        self._locked = False

    def acquire(self) -> None:
        try:
            descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            try:
                owner = int(self.lock_path.read_text(encoding="utf-8").strip())
                os.kill(owner, 0)
            except (ValueError, ProcessLookupError):
                self.lock_path.unlink(missing_ok=True)
                return self.acquire()
            except PermissionError:
                pass
            raise RuntimeError(f"runtime is already locked by process {owner}: {self.lock_path}") from error
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self._locked = True

    def release(self) -> None:
        if self._locked and self.lock_path.exists():
            self.lock_path.unlink()
        self._locked = False

    def __enter__(self) -> "RuntimeStore":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def configuration_path(self) -> Path:
        return self.root / "configuration.json"

    @property
    def manifest_path(self) -> Path:
        return self.root / "run.json"

    def write_state(self, state: Dict[str, Any], graph: Dict[str, Any]) -> None:
        validate_state(state, graph)
        atomic_write_json(self.state_path, state)

    def write_inputs(self, graph: Dict[str, Any], delegation: Dict[str, Any]) -> None:
        atomic_write_json(self.root / "task-graph.yaml", graph)
        atomic_write_json(self.root / "delegation.yaml", delegation)

    def write_configuration(self, configuration: Dict[str, Any]) -> None:
        atomic_write_json(self.configuration_path, configuration)

    def write_manifest(self, manifest: Dict[str, Any]) -> None:
        if self.manifest_path.exists():
            if load_data(self.manifest_path) != manifest:
                raise ValueError(f"run manifest is immutable: {self.manifest_path}")
            return
        atomic_write_json(self.manifest_path, manifest)

    def load_state(self, graph: Dict[str, Any], *, extend: bool = False) -> Dict[str, Any]:
        state = load_data(self.state_path)
        if extend:
            from .state import extend_for_graph

            extend_for_graph(state, graph)
        validate_state(state, graph)
        return state

    def write_result(self, result: Dict[str, Any]) -> Path:
        validate_result(result)
        name = f"{result['task_id']}-attempt-{result['attempt']}.json"
        path = self.results / name
        atomic_write_json(path, result)
        return path

    def write_review(self, review: Dict[str, Any], cycle: int) -> Path:
        validate_review(review)
        path = self.reviews / f"review-{cycle}.json"
        atomic_write_json(path, review)
        return path

    def event_log(self, run_id: str) -> EventLog:
        return EventLog(self.logs / "events.jsonl", run_id)
