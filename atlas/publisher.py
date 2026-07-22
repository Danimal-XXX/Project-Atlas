"""Validated input boundary shared by canonical Atlas publishers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from atlas.schema_validator import AtlasSchemaValidator


def validate_publisher_input(
    objects: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    schema_name: str = "knowledge.schema.json",
    validator: AtlasSchemaValidator | None = None,
) -> list[Mapping[str, Any]]:
    """Validate canonical objects before a publisher performs side effects.

    Publishers accepting canonical Atlas data must call this function at their
    public boundary. A single object is normalized to a one-item list.
    """
    schema_validator = validator or AtlasSchemaValidator()
    if isinstance(objects, Mapping):
        schema_validator.validate_object(objects, schema_name)
        return [objects]
    return schema_validator.validate_collection(objects, schema_name)
