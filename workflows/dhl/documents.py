"""Decode MyDHL shipment documents for a draft-only email hand-off."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MAX_OUTLOOK_ATTACHMENT_BYTES = 3 * 1024 * 1024
DEFAULT_DRAFT_SENDER_NAME = "Dan Walker"
DEFAULT_DRAFT_COMPANY_NAME = "StretchSense"
FORMAT_DETAILS = {
    "PDF": ("pdf", "application/pdf"),
    "PNG": ("png", "image/png"),
    "JPG": ("jpg", "image/jpeg"),
    "JPEG": ("jpg", "image/jpeg"),
}


class ShipmentDocumentError(ValueError):
    """Raised when returned shipment documents cannot be handled safely."""


@dataclass(frozen=True)
class ShipmentDocument:
    """One decoded attachment returned by MyDHL."""

    filename: str
    mime_type: str
    content: bytes
    type_code: str


def extract_shipment_documents(
    response: Mapping[str, Any],
) -> tuple[ShipmentDocument, ...]:
    """Validate and decode shipment-level documents without writing them to disk."""
    tracking_number = _safe_name(response.get("shipmentTrackingNumber"), "shipment")
    raw_documents = response.get("documents")
    if not isinstance(raw_documents, Sequence) or isinstance(raw_documents, (str, bytes)):
        raise ShipmentDocumentError("MyDHL response does not contain shipment documents")
    documents = []
    for index, raw_document in enumerate(raw_documents, start=1):
        if not isinstance(raw_document, Mapping):
            raise ShipmentDocumentError(f"Document {index} is not an object")
        image_format = str(raw_document.get("imageFormat", "")).upper()
        try:
            extension, mime_type = FORMAT_DETAILS[image_format]
        except KeyError as error:
            raise ShipmentDocumentError(
                f"Document {index} uses unsupported format {image_format!r}"
            ) from error
        type_code = _safe_name(raw_document.get("typeCode"), "document")
        encoded = raw_document.get("content")
        if not isinstance(encoded, str) or not encoded:
            raise ShipmentDocumentError(f"Document {index} has no encoded content")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ShipmentDocumentError(
                f"Document {index} content is not valid base64"
            ) from error
        if not content:
            raise ShipmentDocumentError(f"Document {index} is empty")
        if len(content) > MAX_OUTLOOK_ATTACHMENT_BYTES:
            raise ShipmentDocumentError(
                f"Document {index} exceeds the 3 MB direct Outlook attachment limit"
            )
        package_reference = raw_document.get("packageReferenceNumber")
        package_suffix = f"-piece-{package_reference}" if package_reference else ""
        documents.append(
            ShipmentDocument(
                filename=(
                    f"DHL-{tracking_number}-{type_code}{package_suffix}-{index}.{extension}"
                ),
                mime_type=mime_type,
                content=content,
                type_code=type_code,
            )
        )
    if not documents:
        raise ShipmentDocumentError("MyDHL response contains no shipment documents")
    return tuple(documents)


def build_outlook_draft_manifest(
    *,
    mailbox: str,
    to: Sequence[str],
    subject: str,
    body: str,
    documents: Sequence[ShipmentDocument],
    sender_name: str = DEFAULT_DRAFT_SENDER_NAME,
    company_name: str = DEFAULT_DRAFT_COMPANY_NAME,
) -> dict[str, Any]:
    """Create a hand-off manifest that permits draft creation but never sending."""
    if not mailbox.strip() or not subject.strip() or not body.strip():
        raise ValueError("Mailbox, subject and body are required")
    if not sender_name.strip() or not company_name.strip():
        raise ValueError("Sender name and company name are required")
    if not to or any(not isinstance(address, str) or not address.strip() for address in to):
        raise ValueError("At least one verified recipient is required")
    if not documents:
        raise ValueError("At least one DHL document is required")
    return {
        "operation": "create_draft",
        "send": False,
        "collection_instruction": "Sender to arrange pickup",
        "mailbox": mailbox.strip(),
        "to": [address.strip() for address in to],
        "subject": subject.strip(),
        "body": _with_sender_signature(body, sender_name, company_name),
        "attachments": [
            {
                "filename": document.filename,
                "mime_type": document.mime_type,
                "size_bytes": len(document.content),
            }
            for document in documents
        ],
    }


def _with_sender_signature(body: str, sender_name: str, company_name: str) -> str:
    """Append the controlled two-line sender signature to a draft body."""
    signature = f"{sender_name.strip()}\n{company_name.strip()}"
    message = body.rstrip()
    if message.endswith(signature):
        return message
    return f"{message}\n\n{signature}"


def _safe_name(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return cleaned[:80] or fallback
