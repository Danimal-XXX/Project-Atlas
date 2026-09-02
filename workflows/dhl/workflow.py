"""Carrier-neutral shipment validation and frozen MyDHL operation preparation."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from atlas.schema_validator import AtlasSchemaValidator
from workflows.dhl.config import DHLEnvironment
from workflows.dhl.controls import payload_sha256


SUPPORTED_OPERATIONS = {"create_shipment", "create_pickup"}
RMA_SHIPMENT_TYPES = {"rma_return", "warranty_return", "paid_repair", "evaluation"}
RMA_EXPORT_REASON = "Faulty - return for repair/assessment"
RMA_VALUATION_NOTE = "NO COMMERCIAL VALUE - Value for customs purposes only"
RMA_DEFAULT_UNIT_VALUE_USD = 50.0
RMA_DEFAULT_BILLING_CHARGES = ["freight", "duties_taxes"]
SINGLE_GLOVE_RMA_DIMENSIONS_CM = {
    "length_cm": 20,
    "width_cm": 15,
    "height_cm": 10,
}
UNKNOWN_GLOVE_RETURN_DESCRIPTION = "faulty Motion capture glove"


@dataclass(frozen=True)
class PreparedOperation:
    """Immutable review snapshot used for approval and later API submission."""

    operation: str
    environment: DHLEnvironment
    draft: Mapping[str, Any]
    dhl_payload: Mapping[str, Any]
    envelope: Mapping[str, Any]
    payload_sha256: str


class ShipmentWorkflow:
    """Validate a shipment draft and freeze its exact carrier operation."""

    def __init__(self, validator: AtlasSchemaValidator | None = None) -> None:
        self.validator = validator or AtlasSchemaValidator()

    def validate_draft(self, draft: Mapping[str, Any]) -> Mapping[str, Any]:
        normalised = self.apply_defaults(draft)
        return self.validator.validate_object(normalised, "shipment-draft.schema.json")

    @staticmethod
    def apply_defaults(draft: Mapping[str, Any]) -> Mapping[str, Any]:
        """Apply controlled defaults before canonical schema validation."""
        if not isinstance(draft, Mapping):
            return draft
        normalised = deepcopy(dict(draft))
        has_explicit_billing_charges = "billing_charges" in normalised
        normalised.setdefault("collection_arrangement", "sender_to_arrange_pickup")
        normalised.setdefault("pickup_requested", False)
        normalised.setdefault("billing_charges", ["freight"])
        if normalised.get("shipment_type") not in RMA_SHIPMENT_TYPES:
            return normalised
        if not has_explicit_billing_charges:
            normalised["billing_charges"] = deepcopy(RMA_DEFAULT_BILLING_CHARGES)
        customs = normalised.get("customs")
        if not isinstance(customs, dict):
            return normalised
        customs.setdefault("currency", "USD")
        customs.setdefault("incoterm", "DDP")
        customs.setdefault("invoice_type", "proforma")
        customs.setdefault("export_reason_type", "return")
        customs.setdefault("export_reason", RMA_EXPORT_REASON)
        customs.setdefault("commercial_value_status", "no_commercial_value")
        customs.setdefault("valuation_note", RMA_VALUATION_NOTE)
        line_items = customs.get("line_items")
        if isinstance(line_items, list):
            for line_item in line_items:
                if isinstance(line_item, dict):
                    line_item.setdefault("unit_value", RMA_DEFAULT_UNIT_VALUE_USD)
            is_single_glove = (
                len(line_items) == 1
                and isinstance(line_items[0], dict)
                and line_items[0].get("quantity") == 1
                and "glove" in " ".join(
                    str(line_items[0].get(field, ""))
                    for field in ("product_ref", "description")
                ).lower()
            )
            if is_single_glove:
                line_items[0].setdefault("product_ref", "FAULTY-MOCAP-GLOVE")
                line_items[0].setdefault(
                    "description", UNKNOWN_GLOVE_RETURN_DESCRIPTION
                )
                packages = normalised.get("packages")
                if isinstance(packages, list) and len(packages) == 1:
                    package = packages[0]
                    if isinstance(package, dict):
                        package.setdefault(
                            "description", UNKNOWN_GLOVE_RETURN_DESCRIPTION
                        )
                        for field, value in SINGLE_GLOVE_RMA_DIMENSIONS_CM.items():
                            package.setdefault(field, value)
        return normalised

    def prepare(
        self,
        *,
        draft: Mapping[str, Any],
        dhl_payload: Mapping[str, Any],
        operation: str,
        environment: DHLEnvironment = DHLEnvironment.TEST,
    ) -> PreparedOperation:
        """Freeze an exact request; any later change invalidates its approval hash."""
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError(f"Unsupported DHL operation: {operation}")
        validated = self.validate_draft(draft)
        if operation == "create_shipment" and validated["pickup_requested"]:
            raise ValueError(
                "Shipment creation must not include pickup; prepare a separate pickup operation"
            )
        if not isinstance(dhl_payload, Mapping) or not dhl_payload:
            raise ValueError("dhl_payload must be a non-empty object")
        if operation == "create_shipment" and self._payload_requests_pickup(dhl_payload):
            raise ValueError(
                "MyDHL shipment payload must set pickup.isRequested to false"
            )
        envelope = {
            "operation": operation,
            "environment": environment.value,
            "draft_sha256": payload_sha256(validated),
            "dhl_payload_sha256": payload_sha256(dhl_payload),
        }
        digest = payload_sha256(envelope)
        return PreparedOperation(
            operation=operation,
            environment=environment,
            draft=validated,
            dhl_payload=dict(dhl_payload),
            envelope=envelope,
            payload_sha256=digest,
        )

    @staticmethod
    def _payload_requests_pickup(payload: Mapping[str, Any]) -> bool:
        pickup = payload.get("pickup")
        return not isinstance(pickup, Mapping) or pickup.get("isRequested") is not False
