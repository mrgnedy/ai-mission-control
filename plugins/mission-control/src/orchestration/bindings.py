"""File-backed worker guidance; parent skill invocation remains the parent's job."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from .failures import ValidationError
from .io import atomic_write_json, load_data


WARN_BYTES = 32 * 1024
MAX_BYTES = 128 * 1024
PARENT_GATES = {"intake", "investigation", "planning", "closure", "learning_capture"}
LINK = re.compile(r"\[[^\]\n]+\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)]*)?\)|^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|[^\s]+)", re.MULTILINE)


def validate_bindings(methodology: Mapping[str, Any], graph: Mapping[str, Any] | None = None) -> None:
    """Pure contract checks: snapshots need not retain the original source files."""
    files = methodology.get("file_skills", {})
    gates = methodology.get("gate_skills", {})
    reviewers = methodology.get("reviewer_skills", [])
    if not isinstance(files, dict) or any(not isinstance(k, str) or not k.strip() or
            not isinstance(v, str) or not v.strip() for k, v in files.items()):
        raise ValidationError("file_skills must map nonempty aliases to source directories")
    for source in files.values():
        if source.startswith("mission-control:"):
            if not re.fullmatch(r"mission-control:[a-z0-9][a-z0-9-]*", source):
                raise ValidationError(f"invalid own-plugin skill: {source}")
            if source == "mission-control:mission-control":
                raise ValidationError("recursive Mission Control file binding")
        elif Path(source).is_absolute() or ":" in source or ".." in Path(source).parts:
            raise ValidationError(f"skill directory must be repository-relative: {source}")
    if not isinstance(gates, dict) or set(gates) - (PARENT_GATES & set(methodology["gates"])):
        raise ValidationError("gate_skills must name selected parent gates, excluding implementation/review")
    for entries in gates.values():
        if not isinstance(entries, list):
            raise ValidationError("gate_skills entries must be ordered lists")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"skill", "when"} or any(
                    not isinstance(v, str) or not v.strip() for v in entry.values()):
                raise ValidationError("gate skill needs nonempty skill and when strings")
            if entry["skill"] in {"mission-control", "mission-control:mission-control"}:
                raise ValidationError("recursive Mission Control gate binding")
            if entry["skill"] in seen:
                raise ValidationError("duplicate gate skill")
            seen.add(entry["skill"])
    selections = [("reviewer_skills", reviewers)]
    if graph is not None:
        selections += [(task["id"], task.get("skills", [])) for task in graph["tasks"]]
    for label, names in selections:
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names) or len(names) != len(set(names)):
            raise ValidationError(f"{label} skills must be a unique ordered string list")
        missing = set(names) - set(files)
        if missing:
            raise ValidationError(f"{label} references unknown file skills: {sorted(missing)}")


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValidationError(f"skill resource escapes permitted root: {path}")
    if not resolved.is_file():
        raise ValidationError(f"missing skill resource: {path}")
    return resolved


def build_bundle(root: Path, workspace: Path, methodology: Mapping[str, Any],
                 graph: Mapping[str, Any] | None = None) -> dict:
    validate_bindings(methodology, graph)
    definitions = methodology.get("file_skills", {})
    reviewers = methodology.get("reviewer_skills", [])
    selected = set(definitions) if graph is None else set(reviewers).union(
        *(set(task.get("skills", [])) for task in graph["tasks"]))
    bundle: dict = {"schema_version": "1.0", "skills": {}, "files": {},
                    "reviewer_skills": list(reviewers), "diagnostics": []}
    for name in sorted(selected):
        source = definitions[name]
        own = source.startswith("mission-control:")
        boundary = (root / "skills" if own else workspace).resolve()
        directory = boundary / source.split(":", 1)[1] if own else boundary / source
        entry = _contained(directory / "SKILL.md", boundary)
        captured: list[str] = []

        def collect(path: Path) -> None:
            path = _contained(path, boundary)
            key = ("plugin:" if own else "repo:") + path.relative_to(boundary).as_posix()
            if key in captured:
                return
            captured.append(key)
            text = path.read_text(encoding="utf-8")
            bundle["files"][key] = text
            for match in LINK.finditer(text):
                link = next(part for part in match.groups() if part is not None).strip("<>")
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = path.parent / unquote(parsed.path)
                # Check containment even for unsupported resources before reading anything.
                if not target.resolve().is_relative_to(boundary):
                    raise ValidationError(f"skill resource escapes permitted root: {link} in {key}")
                if target.suffix.lower() == ".md":
                    collect(target)
                else:
                    bundle["diagnostics"].append(f"{key}: unsupported resource link {link}; assess compatibility")

        collect(entry)
        bundle["skills"][name] = {"source": captured[0], "files": captured}
    return bundle


def skill_payload(bundle: Mapping[str, Any] | None, names: Sequence[str]) -> str:
    if not names:
        return ""
    if bundle is None:
        raise ValidationError("task requests skills but this run has no frozen skill bundle")
    files: list[str] = []
    for name in names:
        if name not in bundle["skills"]:
            raise ValidationError(f"skill not frozen for this run: {name}")
        for key in bundle["skills"][name]["files"]:
            if key not in files:
                files.append(key)
    text = "\n\nBound skill instructions (task scope, permissions and result contract still apply). " \
           "Linked frozen references are the labeled appendices below; do not reread their live sources.\n"
    for key in files:
        text += f"\n### {key}\n{bundle['files'][key]}\n"
    size = len(text.encode("utf-8"))
    if size > MAX_BYTES:
        raise ValidationError(f"bound skill payload is {size} bytes; limit is {MAX_BYTES}; use a smaller compatible procedure")
    return text


def payload_report(bundle: Mapping[str, Any], graph: Mapping[str, Any] | None = None) -> dict:
    uses = [("review", bundle["reviewer_skills"])]
    uses += ([(task["id"], task.get("skills", [])) for task in graph["tasks"]] if graph is not None
             else [(name, [name]) for name in bundle["skills"]])
    payloads: dict[str, dict] = {}
    for label, names in uses:
        text = skill_payload(bundle, names)
        if not text:
            continue
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest not in payloads:
            keys = {key for name in names for key in bundle["skills"][name]["files"]}
            payloads[digest] = {"bytes": len(text.encode()), "uses": [], "contributors": sorted(
                ({"source": key, "bytes": len(bundle["files"][key].encode())} for key in keys),
                key=lambda item: item["bytes"], reverse=True)}
        payloads[digest]["uses"].append(label)
    return {"payloads": list(payloads.values()), "warnings": [item for item in payloads.values()
            if item["bytes"] > WARN_BYTES], "diagnostics": bundle["diagnostics"]}


def freeze_bundle(runtime: Path, bundle: dict, manifest: dict) -> None:
    if not bundle["skills"]:
        return
    from .runs import file_sha256
    path = runtime / "skills.json"
    if path.exists() and load_data(path) != bundle:
        raise ValidationError("frozen skill bundle already exists with different content")
    atomic_write_json(path, bundle)
    manifest["sources"]["skills"] = {"path": str(path.resolve()), "sha256": file_sha256(path)}


def load_run_skills(runtime: Path) -> dict | None:
    from .runs import file_sha256
    manifest = load_data(runtime / "run.json")
    source = manifest["sources"].get("skills")
    if source is None:
        return None
    path = runtime / "skills.json"
    if Path(source["path"]).resolve() != path.resolve() or not path.is_file() or file_sha256(path) != source["sha256"]:
        raise ValidationError("frozen skill bundle is missing or corrupt")
    bundle = load_data(path)
    if bundle.get("schema_version") != "1.0":
        raise ValidationError("unsupported frozen skill bundle version")
    return bundle
