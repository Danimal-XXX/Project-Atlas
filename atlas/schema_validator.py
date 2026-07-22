"""JSON Schema validation for canonical Atlas objects."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


DEFAULT_SCHEMA_DIRECTORY = Path(__file__).resolve().parents[1] / "schemas"


class AtlasValidationError(ValueError):
    """Raised when an object does not satisfy an Atlas schema."""

    def __init__(
        self,
        *,
        object_id: str,
        json_path: str,
        schema_path: str,
        message: str,
        schema_name: str,
    ) -> None:
        self.object_id = object_id
        self.json_path = json_path
        self.schema_path = schema_path
        self.validation_message = message
        self.schema_name = schema_name
        super().__init__(
            f"Object {object_id!r} failed {schema_name} validation at "
            f"{json_path} (schema {schema_path}): {message}"
        )


class AtlasSchemaValidator:
    """Load, cache, and apply Draft 2020-12 schemas from ``schemas/``."""

    def __init__(self, schema_directory: str | Path | None = None) -> None:
        self.schema_directory = Path(schema_directory or DEFAULT_SCHEMA_DIRECTORY).resolve()

    @lru_cache(maxsize=None)
    def load_schema(self, schema_name: str) -> dict[str, Any]:
        """Load a named schema, rejecting paths outside the schema directory."""
        candidate = (self.schema_directory / schema_name).resolve()
        if self.schema_directory not in candidate.parents:
            raise ValueError(f"Schema must be inside {self.schema_directory}: {schema_name}")
        if not candidate.is_file():
            raise FileNotFoundError(f"Atlas schema not found: {candidate}")
        with candidate.open(encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        Draft202012Validator.check_schema(schema)
        return schema

    @lru_cache(maxsize=None)
    def _validator(self, schema_name: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.load_schema(schema_name),
            format_checker=FormatChecker(),
        )

    def validate_object(
        self, instance: Mapping[str, Any], schema_name: str
    ) -> Mapping[str, Any]:
        """Validate one object and return it unchanged."""
        if not isinstance(instance, Mapping):
            raise AtlasValidationError(
                object_id="<unknown>",
                json_path="$",
                schema_path="$",
                message=f"expected an object, got {type(instance).__name__}",
                schema_name=schema_name,
            )
        errors = sorted(
            self._validator(schema_name).iter_errors(instance),
            key=lambda error: (
                tuple(map(str, error.absolute_path)),
                tuple(map(str, error.absolute_schema_path)),
            ),
        )
        if errors:
            error = errors[0]
            raise AtlasValidationError(
                object_id=str(instance.get("id", "<unknown>")),
                json_path=_json_path(error.absolute_path),
                schema_path=_json_path(error.absolute_schema_path),
                message=error.message,
                schema_name=schema_name,
            )
        return instance

    def validate_collection(
        self,
        instances: Iterable[Mapping[str, Any]],
        schema_name: str,
    ) -> list[Mapping[str, Any]]:
        """Materialize and validate every object in a collection."""
        if isinstance(instances, (str, bytes, Mapping)) or not isinstance(instances, Iterable):
            raise AtlasValidationError(
                object_id="<collection>",
                json_path="$",
                schema_path="$",
                message="expected an iterable collection of objects",
                schema_name=schema_name,
            )
        validated = []
        for instance in instances:
            validated.append(self.validate_object(instance, schema_name))
        return validated


def _json_path(parts: Iterable[Any]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path
