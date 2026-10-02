from __future__ import annotations

import subprocess
import time
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .models import verification_record


COMMAND_ENVIRONMENT_KEYS = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "COMSPEC", "PATHEXT",
}


def command_environment() -> Dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in COMMAND_ENVIRONMENT_KEYS}


def run_commands(
    commands: Iterable[List[str]], workspace: Path, timeout_seconds: int
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for command in commands:
        started = time.monotonic()
        try:
            process = subprocess.run(
                command,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=command_environment(),
            )
            duration = int((time.monotonic() - started) * 1000)
            status = "passed" if process.returncode == 0 else "failed"
            results.append(
                verification_record(command, status, process.returncode, duration, process.stdout, process.stderr)
            )
            if status == "failed":
                break
        except subprocess.TimeoutExpired as error:
            duration = int((time.monotonic() - started) * 1000)
            stdout = error.stdout if isinstance(error.stdout, str) else ""
            stderr = error.stderr if isinstance(error.stderr, str) else ""
            results.append(verification_record(command, "timeout", None, duration, stdout, stderr))
            break
        except OSError as error:
            duration = int((time.monotonic() - started) * 1000)
            results.append(verification_record(command, "failed", None, duration, "", str(error)))
            break
    return results


def verification_passed(records: Iterable[Mapping[str, Any]]) -> bool:
    return all(record["status"] == "passed" for record in records)
