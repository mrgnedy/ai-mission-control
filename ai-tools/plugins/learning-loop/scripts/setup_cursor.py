#!/usr/bin/env python3
"""Preview/apply shared Learning Loop wiring without replacing project knowledge."""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Optional


POINTER = ".learning-loop-rulebook-dir"
CURSOR_RULE = ".cursor/rules/learning-loop.mdc"


class SetupError(Exception):
    pass


def read(path: Path) -> Optional[bytes]:
    return path.read_bytes() if path.is_file() else None


def valid_relative(value: str) -> str:
    value = value.strip()
    path = PurePosixPath(value)
    if (not value or value.startswith("/") or "\\" in value or
            any(part in {"", ".", ".."} for part in value.split("/")) or
            path.as_posix() != value):
        raise SetupError(f"unsafe rulebook directory: {value!r}")
    return value


def git_root(requested: str) -> Path:
    result = subprocess.run(
        ["git", "-C", requested, "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise SetupError("learning-loop setup requires a Git repository")
    return Path(result.stdout.strip()).resolve()


def ensure_safe(root: Path, relative: str) -> Path:
    valid_relative(relative)
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise SetupError(f"symbolic-link target is unsafe: {relative}")
    if not current.resolve().is_relative_to(root):
        raise SetupError(f"path leaves the repository: {relative}")
    if current.exists() and not current.is_file() and not current.is_dir():
        raise SetupError(f"unsupported target: {relative}")
    return current


def select_directory(root: Path) -> str:
    pointer = ensure_safe(root, POINTER)
    explicit = os.environ.get("LESSON_RULEBOOK_DIR", "").strip()
    pointed = ""
    if pointer.is_file():
        lines = pointer.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1:
            raise SetupError("invalid rulebook pointer; expected one relative directory")
        pointed = valid_relative(lines[0])
    references = set()
    for instruction in ("AGENTS.md", "CLAUDE.md"):
        source = read(ensure_safe(root, instruction))
        if source:
            for match in re.finditer(rb"(?m)^@([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)/rules\.md\s*$", source):
                references.add(valid_relative(match.group(1).decode("ascii")))
    if len(references) > 1:
        raise SetupError("project instructions import different rulebooks; resolve this before setup")
    if explicit:
        selected = valid_relative(explicit)
        if pointed and selected != valid_relative(pointed):
            raise SetupError("explicit rulebook directory conflicts with repository pointer")
    elif pointed:
        selected = valid_relative(pointed)
    else:
        candidates = []
        for rel in (".claude", ".Codex"):
            if ensure_safe(root, f"{rel}/rules.md").is_file():
                candidates.append(rel)
        if len(candidates) > 1:
            raise SetupError("multiple rulebooks found; set LESSON_RULEBOOK_DIR explicitly")
        selected = candidates[0] if candidates else next(iter(references), ".claude")
    if references and selected not in references:
        raise SetupError("selected rulebook conflicts with project instruction import")
    if references and not ensure_safe(root, f"{selected}/rules.md").is_file():
        raise SetupError("project instructions import a missing rulebook; repair that wiring first")
    ensure_safe(root, selected)
    if selected != ".claude" and (root / ".claude/rules.md").is_file() and not explicit and not pointed:
        raise SetupError("existing .claude rulebook conflicts with selected custom rulebook")
    return selected


def claude_block(directory: str) -> bytes:
    return (
        "## Shared rulebook\n\n"
        f"@{directory}/rules.md\n\n"
        "When you discover a durable, non-obvious project insight that would help future agents avoid\n"
        "mistakes or repeated investigation, consider invoking the `learning-loop:lesson` skill and\n"
        "propose capturing it.\n"
    ).encode()


def cursor_rule(directory: str) -> bytes:
    return (
        "---\n"
        "description: Shared Learning Loop rulebook\n"
        "alwaysApply: true\n"
        "---\n\n"
        f"Before repository work, read `{directory}/rules.md` as the canonical project rulebook. "
        "Do not duplicate its contents here. When corrected or when a durable insight arises, "
        "propose the learning-loop lesson skill; write a rule only after the user approves its exact wording.\n"
    ).encode()


def plan(root: Path, client: str):
    directory = select_directory(root)
    plugin = Path(__file__).resolve().parent.parent
    templates = {
        f"{directory}/rules.md": (plugin / "skills/lesson/assets/rules.template.md").read_bytes(),
        f"{directory}/checks.tsv": (plugin / "skills/lesson/assets/checks.template.tsv").read_bytes(),
    }
    if directory != ".claude":
        templates[POINTER] = (directory + "\n").encode()
    if client == "cursor":
        templates[CURSOR_RULE] = cursor_rule(directory)
        existing_rule = read(ensure_safe(root, CURSOR_RULE))
        if existing_rule is not None and existing_rule != templates[CURSOR_RULE]:
            raise SetupError("existing Cursor rule differs from the managed pointer; merge it manually")
    else:
        instructions = read(root / "CLAUDE.md") or b""
        if f"@{directory}/rules.md".encode() not in instructions.splitlines():
            block = claude_block(directory)
            templates["CLAUDE.md"] = block + (b"\n" + instructions if instructions else b"")

    observed = set(templates) | {"CLAUDE.md", "AGENTS.md", POINTER,
                                  ".claude/rules.md", ".Codex/rules.md", CURSOR_RULE}
    digest = hashlib.sha256()
    digest.update(client.encode() + b"\0" + directory.encode() + b"\0")
    for rel in sorted(observed):
        target = ensure_safe(root, rel)
        content = read(target)
        digest.update(rel.encode() + b"\0" + (content if content is not None else b"<missing>") + b"\0")
    for rel in sorted(templates):
        digest.update(rel.encode() + b"\0" + templates[rel] + b"\0")
    return directory, templates, digest.hexdigest()[:20]


def preview(root: Path, client: str) -> None:
    directory, files, identity = plan(root, client)
    print(f"LEARNING LOOP {client.upper()} SETUP PLAN\nPROJECT: {root}\nRULEBOOK: {directory}\nPLAN_ID: {identity}")
    for rel, content in files.items():
        current = read(root / rel)
        if current is not None:
            if rel == POINTER and current != content:
                raise SetupError("existing rulebook pointer differs; resolve manually")
            if rel == "CLAUDE.md" and current != content:
                print("\nPREPEND: CLAUDE.md\n----- exact resulting content -----")
                print(content.decode(), end="" if content.endswith(b"\n") else "\n")
                print("----- end content -----")
            else:
                print(f"\nPRESERVE: {rel} (already exists)")
        else:
            print(f"\nCREATE: {rel}\n----- exact content -----")
            print(content.decode(), end="" if content.endswith(b"\n") else "\n")
            print("----- end content -----")
    print("\nNo files were changed. Apply only after approval of this exact plan.")


def apply(root: Path, client: str, expected: str) -> None:
    directory, files, identity = plan(root, client)
    if not expected or expected != identity:
        raise SetupError("setup files changed after preview; run plan again and request fresh approval")
    for rel in files:
        target = ensure_safe(root, rel)
        if target.exists() and not target.is_file():
            raise SetupError(f"target is not a regular file: {rel}")
        if target.is_file() and (rel != "CLAUDE.md" or target.read_bytes() == files[rel]):
            continue
        parent = target.parent
        while parent != root:
            if parent.exists() and not parent.is_dir():
                raise SetupError(f"parent is not a directory: {parent}")
            if parent.exists():
                break
            parent = parent.parent
        if not os.access(str(parent), os.W_OK | os.X_OK):
            raise SetupError(f"destination is not writable: {rel}")
    created = []
    try:
        for rel, content in files.items():
            target = ensure_safe(root, rel)
            if target.is_file():
                if rel == "CLAUDE.md" and target.read_bytes() != content:
                    original_mode = target.stat().st_mode & 0o777
                    temporary = target.with_name(".CLAUDE.md.learning-loop.tmp")
                    if temporary.exists():
                        raise SetupError("temporary CLAUDE.md path already exists")
                    with temporary.open("xb") as handle:
                        handle.write(content)
                    temporary.chmod(original_mode)
                    os.replace(str(temporary), str(target))
                    created.append(rel)
                    print("UPDATED: CLAUDE.md")
                else:
                    print(f"PRESERVED: {rel}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents a concurrent writer from being overwritten.
            with target.open("xb") as handle:
                handle.write(content)
            created.append(rel)
            print(f"CREATED: {rel}")
    except OSError as error:
        raise SetupError(f"apply stopped: {error}; created so far: {', '.join(created) or 'none'}") from error
    print("SETUP COMPLETE" if created else "SETUP ALREADY COMPLETE")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"plan", "apply", "plan-claude", "apply-claude", "resolve-pointer"}:
        raise SetupError("usage: setup_cursor.py plan|apply|plan-claude|apply-claude|resolve-pointer REPO [PLAN_ID]")
    action = sys.argv[1]
    root = git_root(sys.argv[2])
    if action == "resolve-pointer":
        pointer = ensure_safe(root, POINTER)
        if not pointer.is_file():
            raise SetupError("rulebook pointer is missing or unsafe")
        lines = pointer.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1:
            raise SetupError("rulebook pointer must contain exactly one relative path")
        directory = valid_relative(lines[0])
        print(ensure_safe(root, directory))
        return
    client = "claude" if action.endswith("-claude") else "cursor"
    if action.startswith("plan"):
        preview(root, client)
    else:
        apply(root, client, sys.argv[3] if len(sys.argv) > 3 else "")


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
