"""Controlled intake from one explicitly selected Outlook email to an RMA draft."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from atlas.schema_validator import AtlasSchemaValidator, AtlasValidationError
from workflows.dhl.controls import payload_sha256
from workflows.dhl.workflow import ShipmentWorkflow


OUTLOOK_RMA_TRIGGER_CATEGORY = "Create DHL RMA"
REVIEW_SCHEMA = "rma-email-review.schema.json"
REQUIRED_DRAFT_PATHS = (
    "billing_account_ref",
    "reason",
    "sender.company_name",
    "sender.contact_name",
    "sender.phone",
    "sender.email",
    "sender.address.address_line_1",
    "sender.address.city",
    "sender.address.postal_code",
    "sender.address.country_code",
    "recipient.company_name",
    "recipient.contact_name",
    "recipient.phone",
    "recipient.email",
    "recipient.address.address_line_1",
    "recipient.address.city",
    "recipient.address.postal_code",
    "recipient.address.country_code",
    "packages",
    "customs.declarable",
    "customs.incoterm",
    "customs.line_items",
)


class OutlookRMAIntakeError(ValueError):
    """Raised when an Outlook RMA intake violates the controlled boundary."""


def build_outlook_rma_review(
    *,
    message: Mapping[str, Any],
    proposed_draft: Mapping[str, Any],
    validator: AtlasSchemaValidator | None = None,
) -> Mapping[str, Any]:
    """Build a review-only candidate from one explicitly categorised message.

    ``proposed_draft`` may be assembled by a human or extraction service, but Atlas
    never treats extraction as approval. The returned manifest contains no raw email
    body and performs no network request.
    """
    schema_validator = validator or AtlasSchemaValidator()
    source = _normalise_outlook_source(message)
    if not isinstance(proposed_draft, Mapping):
        raise OutlookRMAIntakeError("proposed_draft must be an object")

    candidate = deepcopy(dict(proposed_draft))
    candidate.setdefault("schema_version", "1.0")
    candidate.setdefault("id", f"outlook-rma-{source['source_sha256'][:12]}")
    candidate.setdefault("shipment_type", "rma_return")
    candidate.setdefault("pickup_requested", False)
    candidate.setdefault("collection_arrangement", "sender_to_arrange_pickup")

    extensions = candidate.setdefault("extensions", {})
    if not isinstance(extensions, dict):
        raise OutlookRMAIntakeError("proposed_draft.extensions must be an object")
    extensions["outlook_rma_source"] = {
        "message_id": source["message_id"],
        "source_sha256": source["source_sha256"],
    }

    candidate = dict(ShipmentWorkflow(schema_validator).apply_defaults(candidate))
    missing_fields = sorted(_missing_draft_fields(candidate))
    validation_errors: list[str] = []
    if not missing_fields:
        try:
            ShipmentWorkflow(schema_validator).validate_draft(candidate)
        except AtlasValidationError as error:
            validation_errors.append(str(error))

    review = {
        "schema_version": "1.0",
        "id": f"review-{source['source_sha256'][:16]}",
        "operation": "prepare_rma_draft",
        "status": (
            "ready_for_validation"
            if not missing_fields and not validation_errors
            else "needs_review"
        ),
        "source": source,
        "proposed_draft": candidate,
        "draft_sha256": payload_sha256(candidate),
        "missing_fields": missing_fields,
        "validation_errors": validation_errors,
        "dhl_request_made": False,
        "shipment_created": False,
        "pickup_created": False,
    }
    return schema_validator.validate_object(review, REVIEW_SCHEMA)


def confirm_outlook_rma_review(
    review: Mapping[str, Any],
    *,
    validator: AtlasSchemaValidator | None = None,
) -> Mapping[str, Any]:
    """Return the canonical draft only after the review is complete and unchanged."""
    schema_validator = validator or AtlasSchemaValidator()
    validated_review = schema_validator.validate_object(review, REVIEW_SCHEMA)
    if validated_review["status"] != "ready_for_validation":
        raise OutlookRMAIntakeError("RMA email intake still needs review")
    if validated_review["missing_fields"] or validated_review["validation_errors"]:
        raise OutlookRMAIntakeError("RMA email intake contains unresolved fields")
    candidate = validated_review["proposed_draft"]
    if payload_sha256(candidate) != validated_review["draft_sha256"]:
        raise OutlookRMAIntakeError("RMA email intake draft changed after review")
    return ShipmentWorkflow(schema_validator).validate_draft(candidate)


def _normalise_outlook_source(message: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(message, Mapping):
        raise OutlookRMAIntakeError("Outlook message must be an object")
    categories = message.get("categories")
    if not _contains_trigger_category(categories):
        raise OutlookRMAIntakeError(
            f"Outlook message must be explicitly categorised {OUTLOOK_RMA_TRIGGER_CATEGORY!r}"
        )

    sender = message.get("from")
    email_address = sender.get("emailAddress") if isinstance(sender, Mapping) else None
    if not isinstance(email_address, Mapping):
        raise OutlookRMAIntakeError("Outlook message sender is required")
    body = message.get("body")
    body_content = body.get("content") if isinstance(body, Mapping) else ""
    if not isinstance(body_content, str):
        raise OutlookRMAIntakeError("Outlook message body must contain text")

    required_values = {
        "message_id": message.get("id"),
        "subject": message.get("subject"),
        "from_email": email_address.get("address"),
        "received_at": message.get("receivedDateTime"),
    }
    missing = [name for name, value in required_values.items() if not _nonempty(value)]
    if missing:
        raise OutlookRMAIntakeError(
            "Outlook message is missing required metadata: " + ", ".join(missing)
        )

    fingerprint = {
        **required_values,
        "conversation_id": message.get("conversationId"),
        "internet_message_id": message.get("internetMessageId"),
        "body_sha256": payload_sha256({"content": body_content}),
    }
    return {
        "provider": "outlook",
        "selection": "explicit",
        "trigger_category": OUTLOOK_RMA_TRIGGER_CATEGORY,
        "message_id": str(required_values["message_id"]).strip(),
        "conversation_id": _optional_text(message.get("conversationId")),
        "internet_message_id": _optional_text(message.get("internetMessageId")),
        "subject": str(required_values["subject"]).strip(),
        "from_name": _optional_text(email_address.get("name")),
        "from_email": str(required_values["from_email"]).strip(),
        "received_at": str(required_values["received_at"]).strip(),
        "source_sha256": payload_sha256(fingerprint),
    }


def _contains_trigger_category(categories: Any) -> bool:
    return (
        isinstance(categories, Sequence)
        and not isinstance(categories, (str, bytes))
        and OUTLOOK_RMA_TRIGGER_CATEGORY in categories
    )


def _missing_draft_fields(candidate: Mapping[str, Any]) -> set[str]:
    missing = {
        path for path in REQUIRED_DRAFT_PATHS if not _value_at_path(candidate, path)
    }
    packages = candidate.get("packages")
    if isinstance(packages, Sequence) and not isinstance(packages, (str, bytes)):
        for index, package in enumerate(packages):
            for field in ("description", "weight_kg", "length_cm", "width_cm", "height_cm"):
                if not isinstance(package, Mapping) or not _present(package.get(field)):
                    missing.add(f"packages[{index}].{field}")
    line_items = candidate.get("customs", {}).get("line_items") if isinstance(candidate.get("customs"), Mapping) else None
    if isinstance(line_items, Sequence) and not isinstance(line_items, (str, bytes)):
        for index, item in enumerate(line_items):
            for field in (
                "product_ref",
                "description",
                "quantity",
                "unit_value",
                "hs_code",
                "country_of_origin",
                "net_weight_kg",
            ):
                if not isinstance(item, Mapping) or not _present(item.get(field)):
                    missing.add(f"customs.line_items[{index}].{field}")
    return missing


def _value_at_path(candidate: Mapping[str, Any], path: str) -> bool:
    value: Any = candidate
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return _present(value)


def _present(value: Any) -> bool:
    if value is False or value == 0:
        return True
    if isinstance(value, (str, bytes, Sequence, Mapping)):
        return bool(value)
    return value is not None


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _optional_text(value: Any) -> str | None:
    return str(value).strip() if _nonempty(value) else None
