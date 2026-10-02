#!/usr/bin/env python3
"""Inspect, initialize, or explicitly replace repository-owned orchestration config."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PLUGIN_ROOT / "templates" / ".ai" / "orchestration"


def discover_workspace() -> Path:
    current = Path.cwd().resolve()
    result = subprocess.run(
        ["git", "-C", str(current), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return current


def validate_template() -> None:
    sys.path.insert(0, str(PLUGIN_ROOT / "src"))
    from orchestration.configuration import load_configuration

    load_configuration(PLUGIN_ROOT / "templates", TEMPLATE / "orchestration.yaml")


def _inventory(root: Path) -> Dict[str, str]:
    if not root.is_dir():
        return {}
    inventory: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            content = f"symlink:{os.readlink(path)}".encode("utf-8")
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        inventory[relative] = hashlib.sha256(content).hexdigest()
    return inventory


def inspect(workspace: Path) -> Dict[str, Any]:
    destination = workspace.resolve() / ".ai" / "orchestration"
    validate_template()
    template_files = _inventory(TEMPLATE)
    if not destination.exists():
        return {
            "status": "missing",
            "destination": str(destination),
            "template_only": sorted(template_files),
            "project_only": [],
            "changed": [],
        }
    if destination.is_symlink():
        raise ValueError(f"orchestration destination must not be a symlink: {destination}")
    if not destination.is_dir():
        raise ValueError(f"orchestration destination is not a directory: {destination}")
    project_files = _inventory(destination)
    template_paths = set(template_files)
    project_paths = set(project_files)
    changed = sorted(
        path for path in template_paths & project_paths
        if template_files[path] != project_files[path]
    )
    return {
        "status": "current" if not changed and template_paths == project_paths else "diverged",
        "destination": str(destination),
        "template_only": sorted(template_paths - project_paths),
        "project_only": sorted(project_paths - template_paths),
        "changed": changed,
    }


def print_inspection(report: Dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"Status: {report['status']}")
    print(f"Destination: {report['destination']}")
    for label, key in (
        ("Template-only files", "template_only"),
        ("Project-only files", "project_only"),
        ("Changed files", "changed"),
    ):
        values = report[key]
        print(f"{label}: {len(values)}")
        for value in values:
            print(f"  - {value}")


def initialize(workspace: Path) -> int:
    destination = workspace.resolve() / ".ai" / "orchestration"
    if destination.exists():
        print(f"Unchanged: {destination} already exists.")
        print("Inspect differences with --status before choosing merge or replacement.")
        return 0

    validate_template()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(TEMPLATE, destination)
    except FileExistsError:
        print(f"Unchanged: {destination} appeared during initialization.")
        return 0

    print(f"Created: {destination}")
    return 0


def replace(workspace: Path, *, confirmed: bool) -> int:
    if not confirmed:
        raise ValueError("replacement requires --yes after the user explicitly chooses replace")
    workspace = workspace.resolve()
    destination = workspace / ".ai" / "orchestration"
    if not destination.exists():
        return initialize(workspace)
    if destination.is_symlink():
        raise ValueError(f"orchestration destination must not be a symlink: {destination}")
    if not destination.is_dir():
        raise ValueError(f"orchestration destination is not a directory: {destination}")
    validate_template()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = workspace / ".ai" / "mission-control-backups" / stamp / "orchestration"
    staging = workspace / ".ai" / f".orchestration-replacement-{uuid.uuid4().hex}"
    shutil.copytree(TEMPLATE, staging)
    try:
        backup.parent.mkdir(parents=True, exist_ok=False)
        destination.rename(backup)
    except Exception:
        shutil.rmtree(staging)
        try:
            backup.parent.rmdir()
        except OSError:
            pass
        raise
    try:
        os.replace(staging, destination)
    except Exception:
        if not destination.exists() and backup.exists():
            backup.rename(destination)
        if staging.exists():
            shutil.rmtree(staging)
        raise

    print(f"Replaced: {destination}")
    print(f"Backup: {backup}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create .ai/orchestration from Mission Control's packaged template."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Target workspace; defaults to the current Git root or current directory.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status",
        action="store_true",
        help="Compare the project-owned directory with the packaged template without writing.",
    )
    mode.add_argument(
        "--replace",
        action="store_true",
        help="Replace the project-owned directory after preserving it in a timestamped backup.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm an explicitly chosen replacement; valid only with --replace.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print --status output as JSON.",
    )
    args = parser.parse_args()
    workspace = args.workspace if args.workspace is not None else discover_workspace()
    if args.yes and not args.replace:
        parser.error("--yes is valid only with --replace")
    if args.json and not args.status:
        parser.error("--json is valid only with --status")
    if args.replace and not args.yes:
        parser.error("--replace requires --yes after the user explicitly chooses replacement")
    if args.status:
        print_inspection(inspect(workspace), as_json=args.json)
        return 0
    if args.replace:
        return replace(workspace, confirmed=args.yes)
    return initialize(workspace)


if __name__ == "__main__":
    raise SystemExit(main())
