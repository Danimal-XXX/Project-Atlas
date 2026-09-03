"""One-use, payload-bound approval controls for DHL write operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol
from typing import Any, Callable

from atlas.schema_validator import AtlasSchemaValidator
from workflows.dhl.config import DHLEnvironment
from workflows.dhl.ledger import DHLLedgerError, SQLiteApprovalLedger


class ApprovalRejected(RuntimeError):
    """Raised before a DHL write when approval is absent, stale, or mismatched."""


class ApprovalLedger(Protocol):
    """Atomic store for consumed approvals and submitted payload identities."""

    def consume(
        self,
        *,
        approval_id: str,
        operation: str,
        environment: str,
        payload_sha256: str,
        approved_by: str,
        consumed_at: datetime,
    ) -> None: ...


class MemoryApprovalLedger:
    """Process-local ledger retained for tests and short-lived tooling."""

    def __init__(self) -> None:
        self._approval_ids: set[str] = set()
        self._payloads: set[tuple[str, str, str]] = set()
        self._lock = Lock()

    def consume(
        self,
        *,
        approval_id: str,
        operation: str,
        environment: str,
        payload_sha256: str,
        approved_by: str,
        consumed_at: datetime,
    ) -> None:
        del approved_by, consumed_at
        payload_key = (operation, environment, payload_sha256)
        with self._lock:
            if approval_id in self._approval_ids:
                raise ApprovalRejected(
                    f"Approval {approval_id!r} has already been consumed"
                )
            if payload_key in self._payloads:
                raise ApprovalRejected(
                    "This exact DHL operation payload has already been submitted"
                )
            self._approval_ids.add(approval_id)
            self._payloads.add(payload_key)


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
        ledger: ApprovalLedger | None = None,
    ) -> None:
        if not expected_approver:
            raise ValueError("expected_approver is required")
        self.expected_approver = expected_approver
        self.validator = validator or AtlasSchemaValidator()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ledger = ledger or MemoryApprovalLedger()

    @property
    def is_durable(self) -> bool:
        return isinstance(self.ledger, SQLiteApprovalLedger)

    def authorise(
        self,
        *,
        approval: Mapping[str, Any],
        operation: str,
        environment: DHLEnvironment,
        payload: Mapping[str, Any],
    ) -> str:
        """Validate exact operation, environment, payload, approver, and expiry."""
        self.validator.validate_object(approval, "shipment-approval.schema.json")
        approval_id = str(approval["id"])
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
        try:
            self.ledger.consume(
                approval_id=approval_id,
                operation=operation,
                environment=environment.value,
                payload_sha256=str(approval["payload_sha256"]),
                approved_by=str(approval["approved_by"]),
                consumed_at=now,
            )
        except DHLLedgerError as error:
            raise ApprovalRejected(str(error)) from error
        return approval_id


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApprovalRejected("Approval contains an invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ApprovalRejected("Approval timestamps must include a timezone")
    return parsed
