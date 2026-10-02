from __future__ import annotations

import platform
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


def inspect_packaged_assets(root: Path, adapters: Iterable[str], *, review: bool) -> Dict[str, Any]:
    """Check only assets needed by the selected worker adapters and review mode."""
    selected = set(adapters)
    required = set()
    if selected & {"claude-cli", "cursor-cli"}:
        required.add("skills/code-discipline/SKILL.md")
    if "claude-cli" in selected:
        required.add(".ai/orchestration/prompts/claude-task.md")
    if "cursor-cli" in selected:
        required.add(".ai/orchestration/prompts/cursor-task.md")
    if review:
        required.add(".ai/orchestration/prompts/execution-review.md")
    missing = sorted(name for name in required if not (root / name).is_file())
    return {"status": "complete" if not missing else "missing", "missing": missing}


def _probe(binary: str, arguments: list, timeout: int = 10) -> Dict[str, Any]:
    path = shutil.which(binary)
    if path is None:
        return {"status": "unavailable", "binary": binary, "path": None}
    try:
        process = subprocess.run([path] + arguments, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "binary": binary, "path": path}
    output = (process.stdout or process.stderr).strip().splitlines()
    return {
        "status": "available" if process.returncode == 0 else "error",
        "binary": binary,
        "path": path,
        "exit_code": process.returncode,
        "summary": output[0][:300] if output else "",
    }


def _probe_claude_auth(binary: str) -> Dict[str, Any]:
    path = shutil.which(binary)
    if path is None:
        return {"status": "unavailable", "binary": binary}
    try:
        process = subprocess.run([path, "auth", "status"], capture_output=True, text=True, timeout=10, check=False)
        payload = json.loads(process.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"status": "error", "binary": binary}
    return {
        "status": "authenticated" if payload.get("loggedIn") else "unauthenticated",
        "binary": binary,
        "auth_method": payload.get("authMethod", "none"),
        "provider": payload.get("apiProvider", "unknown"),
    }


def _probe_cursor_auth(binary: str) -> Dict[str, Any]:
    path = shutil.which(binary)
    if path is None:
        return {"status": "unavailable", "binary": binary}
    try:
        process = subprocess.run([path, "status"], capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "binary": binary}
    output = (process.stdout or process.stderr).strip()
    normalized = output.lower()
    if "not logged in" in normalized or "unauthenticated" in normalized:
        status = "unauthenticated"
    elif process.returncode == 0:
        status = "authenticated"
    else:
        status = "error"
    return {
        "status": status,
        "binary": binary,
        "path": path,
        "exit_code": process.returncode,
        "summary": output.splitlines()[0][:300] if output else "",
    }


def _probe_help_contract(binary: str, required_flags: Iterable[str]) -> Dict[str, Any]:
    path = shutil.which(binary)
    flags = {flag: False for flag in required_flags}
    if path is None:
        return {"status": "unavailable", "flags": flags}
    try:
        process = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "flags": flags}
    output = f"{process.stdout}\n{process.stderr}"
    flags = {flag: flag in output for flag in flags}
    return {
        "status": "complete" if process.returncode == 0 and all(flags.values()) else "incomplete",
        "exit_code": process.returncode,
        "flags": flags,
    }


def diagnose(cursor_binary: str = "cursor-agent", claude_binary: str = "claude", live: bool = False) -> Dict[str, Any]:
    result = {
        "python": {"status": "available", "version": platform.python_version()},
        "git": _probe("git", ["--version"]),
        "claude": _probe(claude_binary, ["--version"]),
        "cursor": _probe(cursor_binary, ["--version"]),
    }
    if result["cursor"]["status"] == "available":
        result["cursor"]["contract"] = _probe_help_contract(
            cursor_binary, ["-p", "--force", "--sandbox", "--output-format", "--model"]
        )
    if result["claude"]["status"] == "available":
        result["claude"]["contract"] = _probe_help_contract(
            claude_binary,
            [
                "-p", "--output-format", "--permission-mode", "--model", "--json-schema", "--tools",
                "--no-session-persistence", "--no-chrome", "--safe-mode", "--restricted",
                "--strict-mcp-config", "--mcp-config",
            ],
        )
    if live:
        if result["cursor"]["status"] == "available":
            result["cursor_auth"] = _probe_cursor_auth(cursor_binary)
        if result["claude"]["status"] == "available":
            result["claude_auth"] = _probe_claude_auth(claude_binary)
    return result


def diagnose_configured(
    providers: Mapping[str, Mapping[str, Any]],
    binaries: Mapping[str, Optional[str]],
    live: bool = False,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "python": {"status": "available", "version": platform.python_version()},
        "git": _probe("git", ["--version"]),
        "providers": {},
    }
    for provider_id, manifest in sorted(providers.items()):
        adapter = manifest["adapter"]
        binary = binaries.get(provider_id)
        if adapter == "local":
            result["providers"][provider_id] = {
                "status": "available",
                "adapter": adapter,
                "capabilities": manifest["capabilities"],
            }
            continue
        if not binary:
            result["providers"][provider_id] = {"status": "unavailable", "adapter": adapter}
            continue
        provider = _probe(binary, ["--version"])
        provider["adapter"] = adapter
        provider["capabilities"] = manifest["capabilities"]
        if provider["status"] == "available" and adapter == "cursor-cli":
            provider["contract"] = _probe_help_contract(
                binary, ["-p", "--force", "--sandbox", "--output-format", "--model"]
            )
            if live:
                provider["auth"] = _probe_cursor_auth(binary)
        elif provider["status"] == "available" and adapter == "claude-cli":
            required_flags = [
                "-p", "--output-format", "--permission-mode", "--model", "--effort",
                "--json-schema", "--tools", "--no-session-persistence", "--no-chrome",
                "--safe-mode", "--restricted", "--strict-mcp-config", "--mcp-config",
            ]
            if manifest.get("allowed_bash_rules"):
                required_flags.append("--allowedTools")
            provider["contract"] = _probe_help_contract(
                binary, required_flags,
            )
            provider["allowed_bash_rules"] = manifest.get("allowed_bash_rules", [])
            if live:
                provider["auth"] = _probe_claude_auth(binary)
        else:
            provider["status"] = "unsupported_adapter"
        result["providers"][provider_id] = provider
    return result
