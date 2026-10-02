from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .failures import ValidationError
from .io import load_data


def validate_with_schema(instance: Any, schema_path: Path) -> None:
    """Validate the JSON-Schema features used by the committed contracts."""
    schema = load_data(schema_path)
    _validate(instance, schema, schema_path.parent, "$", schema_path.name)


def _validate(value: Any, schema: Mapping[str, Any], base: Path, path: str, source: str) -> None:
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or reference.startswith(("#", "http:" , "https:")):
            raise ValidationError(f"{source}: unsupported schema reference {reference!r}")
        target = base / reference
        _validate(value, load_data(target), target.parent, path, target.name)
        return

    if "const" in schema and value != schema["const"]:
        _fail(path, f"must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"must be one of {schema['enum']!r}")
    if "type" in schema and not _matches_type(value, schema["type"]):
        _fail(path, f"must have type {schema['type']!r}")

    if isinstance(value, Mapping):
        if len(value) < schema.get("minProperties", 0):
            _fail(path, f"must contain at least {schema['minProperties']} properties")
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                _fail(path, f"is missing required property {key!r}")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in properties:
                _validate(item, properties[key], base, child, source)
            elif additional is False:
                _fail(path, f"contains unknown property {key!r}")
            elif isinstance(additional, Mapping):
                _validate(item, additional, base, child, source)

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _fail(path, f"must contain at least {schema['minItems']} items")
        if schema.get("uniqueItems"):
            rendered = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(rendered) != len(set(rendered)):
                _fail(path, "must contain unique items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item, item_schema, base, f"{path}[{index}]", source)

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _fail(path, f"must contain at least {schema['minLength']} characters")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            _fail(path, f"does not match {pattern!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, f"must be at least {schema['minimum']}")


def _matches_type(value: Any, expected: Any) -> bool:
    names: Sequence[str] = expected if isinstance(expected, list) else [expected]
    return any(_matches_single_type(value, name) for name in names)


def _matches_single_type(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "object":
        return isinstance(value, Mapping)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ValidationError(f"unsupported schema type {name!r}")


def _fail(path: str, message: str) -> None:
    raise ValidationError(f"{path} {message}")
