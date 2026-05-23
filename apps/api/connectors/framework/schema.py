from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    pass


def validate_json_schema(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    if schema.get("type") != "object":
        raise SchemaValidationError("Only object parameter schemas are supported")
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    for field in required:
        if field not in arguments:
            raise SchemaValidationError(f"{field} is required")
    for key, value in arguments.items():
        if key not in properties:
            continue
        expected = properties[key].get("type")
        if expected == "string" and not isinstance(value, str):
            raise SchemaValidationError(f"{key} must be a string")
        if expected == "integer" and not isinstance(value, int):
            raise SchemaValidationError(f"{key} must be an integer")
        if expected == "number" and not isinstance(value, (int, float)):
            raise SchemaValidationError(f"{key} must be a number")
        if expected == "boolean" and not isinstance(value, bool):
            raise SchemaValidationError(f"{key} must be a boolean")
        if expected == "object" and not isinstance(value, dict):
            raise SchemaValidationError(f"{key} must be an object")
        if expected == "array" and not isinstance(value, list):
            raise SchemaValidationError(f"{key} must be an array")
