"""One-use, payload-bound approval controls for DHL write operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable

from atlas.schema_validator import AtlasSchemaValidator
from workflows.dhl.config import DHLEnvironment


class ApprovalRejected(RuntimeError):
    """Raised before a DHL write when approval is absent, stale, or mismatched."""


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Return a deterministic hash of the exact controlled-operation envelope."""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("DHL operation payload must be canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


class ApprovalGuard:
    """Validate and consume an approval exactly once before network contact."""

    def __init__(
        self,
        *,
        expected_approver: str,
        validator: AtlasSchemaValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not expected_approver:
            raise ValueError("expected_approver is required")
        self.expected_approver = expected_approver
        self.validator = validator or AtlasSchemaValidator()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._consumed: set[str] = set()

    def authorise(
        self,
        *,
        approval: Mapping[str, Any],
        operation: str,
        environment: DHLEnvironment,
        payload: Mapping[str, Any],
    ) -> None:
        """Validate exact operation, environment, payload, approver, and expiry."""
        self.validator.validate_object(approval, "shipment-approval.schema.json")
        approval_id = str(approval["id"])
        if approval_id in self._consumed:
            raise ApprovalRejected(f"Approval {approval_id!r} has already been consumed")
        if approval["approved_by"] != self.expected_approver:
            raise ApprovalRejected("Approval was not issued by the configured approver")
        if approval["operation"] != operation:
            raise ApprovalRejected("Approval operation does not match the requested operation")
        if approval["environment"] != environment.value:
            raise ApprovalRejected("Approval environment does not match the client environment")
        if approval["payload_sha256"] != payload_sha256(payload):
            raise ApprovalRejected("Approval payload hash does not match the frozen request")
        approved_at = _parse_datetime(str(approval["approved_at"]))
        expires_at = _parse_datetime(str(approval["expires_at"]))
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Approval clock must return a timezone-aware datetime")
        if approved_at > now:
            raise ApprovalRejected("Approval timestamp is in the future")
        if expires_at <= approved_at or expires_at <= now:
            raise ApprovalRejected("Approval has expired or has an invalid expiry")
        self._consumed.add(approval_id)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApprovalRejected("Approval contains an invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ApprovalRejected("Approval timestamps must include a timezone")
    return parsed
