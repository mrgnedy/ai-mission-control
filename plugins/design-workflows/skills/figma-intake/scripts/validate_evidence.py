#!/usr/bin/env python3
"""Validate the portable figma-design-evidence/v1 handoff without dependencies."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def obj(value: object, label: str, required: set[str], optional: set[str] | None = None) -> dict:
    require(isinstance(value, dict), f"{label} must be an object")
    value = dict(value)
    keys = set(value)
    require(required <= keys, f"{label} is missing {sorted(required - keys)}")
    require(keys <= required | (optional or set()), f"{label} has unknown fields {sorted(keys - required - (optional or set()))}")
    return value


def nonempty(value: object, label: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{label} must be a nonempty string")
    return value


def string_list(value: object, label: str) -> list[str]:
    require(isinstance(value, list), f"{label} must be an array")
    items = [nonempty(item, f"{label} item") for item in value]
    require(len(items) == len(set(items)), f"{label} contains duplicates")
    return items


def local_file(base: Path, relative: object, label: str) -> None:
    name = nonempty(relative, label)
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(part not in {".", ".."} for part in path.parts)
            and "\\" not in name, f"{label} must be a safe relative path")
    target = (base / name).resolve()
    require(target.is_relative_to(base.resolve()), f"{label} escapes the intake directory")
    require(target.is_file(), f"{label} does not exist: {name}")


def validate(data: object, base: Path) -> dict[str, int]:
    document = obj(data, "evidence", {"schema_version", "source", "frame", "nodes", "assets", "unknowns"})
    require(document["schema_version"] == "figma-design-evidence/v1", "unsupported schema_version")

    source = obj(document["source"], "source", {"url", "node_id", "captured_at"}, {"file_version"})
    url = urlsplit(nonempty(source["url"], "source.url"))
    require(url.scheme == "https" and (url.hostname == "figma.com" or
            (url.hostname or "").endswith(".figma.com")), "source.url must be an HTTPS Figma URL")
    node_id = nonempty(source["node_id"], "source.node_id")
    stamp = nonempty(source["captured_at"], "source.captured_at")
    try:
        captured = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("source.captured_at must be an ISO-8601 timestamp") from error
    require(captured.tzinfo is not None, "source.captured_at must include a timezone")
    if "file_version" in source:
        nonempty(source["file_version"], "source.file_version")

    frame = obj(document["frame"], "frame", {"id", "name", "screenshot"})
    require(nonempty(frame["id"], "frame.id") == node_id, "frame.id must match source.node_id")
    nonempty(frame["name"], "frame.name")
    local_file(base, frame["screenshot"], "frame.screenshot")

    nodes = document["nodes"]
    require(isinstance(nodes, list) and bool(nodes), "nodes must be a nonempty array")
    by_id: dict[str, dict] = {}
    for index, raw in enumerate(nodes):
        label = f"nodes[{index}]"
        node = obj(raw, label, {"id", "parent", "children", "kind", "name", "layout", "appearance"},
                   {"text", "component_ref", "asset_ids"})
        ident = nonempty(node["id"], f"{label}.id")
        require(ident not in by_id, f"duplicate node ID: {ident}")
        require(node["parent"] is None or isinstance(node["parent"], str) and bool(node["parent"].strip()),
                f"{label}.parent must be null or a node ID")
        string_list(node["children"], f"{label}.children")
        kind = nonempty(node["kind"], f"{label}.kind")
        nonempty(node["name"], f"{label}.name")
        require(isinstance(node["layout"], dict), f"{label}.layout must be an object")
        require(isinstance(node["appearance"], dict), f"{label}.appearance must be an object")
        if kind.upper() == "TEXT":
            require(isinstance(node.get("text"), str), f"{label}.text must preserve exact text")
        elif "text" in node:
            require(isinstance(node["text"], str), f"{label}.text must be a string")
        if "component_ref" in node:
            nonempty(node["component_ref"], f"{label}.component_ref")
        if "asset_ids" in node:
            string_list(node["asset_ids"], f"{label}.asset_ids")
        by_id[ident] = node

    require(frame["id"] in by_id and by_id[frame["id"]]["parent"] is None,
            "frame root node is missing or has a parent")
    for ident, node in by_id.items():
        if ident != frame["id"]:
            parent = node["parent"]
            require(parent in by_id, f"{ident} references missing parent {parent}")
            require(ident in by_id[parent]["children"], f"{ident} is absent from parent {parent}'s children")
        for child in node["children"]:
            require(child in by_id, f"{ident} references missing child {child}")
            require(by_id[child]["parent"] == ident, f"{ident} and child {child} disagree on parent")

    visited: set[str] = set()
    active: set[str] = set()

    def visit(ident: str) -> None:
        require(ident not in active, f"node hierarchy has a cycle at {ident}")
        if ident in visited:
            return
        active.add(ident)
        for child in by_id[ident]["children"]:
            visit(child)
        active.remove(ident)
        visited.add(ident)

    visit(frame["id"])
    require(set(by_id) == visited, f"unreachable nodes: {sorted(set(by_id) - visited)}")

    assets = document["assets"]
    require(isinstance(assets, list), "assets must be an array")
    asset_ids: set[str] = set()
    for index, raw in enumerate(assets):
        label = f"assets[{index}]"
        asset = obj(raw, label, {"id", "node_id", "kind"}, {"path", "note"})
        ident = nonempty(asset["id"], f"{label}.id")
        require(ident not in asset_ids, f"duplicate asset ID: {ident}")
        asset_ids.add(ident)
        owner = nonempty(asset["node_id"], f"{label}.node_id")
        require(owner in by_id, f"{label} references missing node {owner}")
        nonempty(asset["kind"], f"{label}.kind")
        require("path" in asset or "note" in asset, f"{label} needs a saved path or an unavailability note")
        if "path" in asset:
            local_file(base, asset["path"], f"{label}.path")
        if "note" in asset:
            nonempty(asset["note"], f"{label}.note")
        require(ident in by_id[owner].get("asset_ids", []), f"{label} is not linked from node {owner}")
    for ident, node in by_id.items():
        require(set(node.get("asset_ids", [])) <= asset_ids, f"{ident} references an unknown asset")

    unknowns = document["unknowns"]
    require(isinstance(unknowns, list), "unknowns must be an array")
    unknown_ids: set[str] = set()
    blocking = 0
    for index, raw in enumerate(unknowns):
        label = f"unknowns[{index}]"
        item = obj(raw, label, {"id", "question", "impact"}, {"node_id"})
        ident = nonempty(item["id"], f"{label}.id")
        require(ident not in unknown_ids, f"duplicate unknown ID: {ident}")
        unknown_ids.add(ident)
        nonempty(item["question"], f"{label}.question")
        require(item["impact"] in {"blocking", "nonblocking"}, f"{label}.impact is invalid")
        blocking += item["impact"] == "blocking"
        if "node_id" in item:
            require(item["node_id"] in by_id, f"{label} references an unknown node")

    return {"nodes": len(nodes), "assets": len(assets), "unknowns": len(unknowns), "blocking": blocking}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Path to evidence.json")
    args = parser.parse_args()
    try:
        with args.evidence.open(encoding="utf-8") as handle:
            result = validate(json.load(handle), args.evidence.parent)
    except (EvidenceError, OSError, json.JSONDecodeError) as error:
        print(f"invalid evidence: {error}", file=sys.stderr)
        return 1
    print("valid evidence: " + ", ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
