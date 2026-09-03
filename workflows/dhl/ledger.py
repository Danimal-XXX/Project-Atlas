"""Durable approval, submission, and reconciliation records for DHL writes."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class DHLLedgerError(RuntimeError):
    """Raised when a durable DHL control record cannot be safely changed."""


class ApprovalAlreadyConsumed(DHLLedgerError):
    """Raised when an approval ID has already been used."""


class PayloadAlreadyConsumed(DHLLedgerError):
    """Raised when the exact operation payload has already been approved."""


class SubmissionAlreadyRecorded(DHLLedgerError):
    """Raised when an exact operation has already entered submission."""


class SQLiteApprovalLedger:
    """Append-oriented SQLite ledger shared by all Atlas DHL processes."""

    VALID_STATES = {"submitting", "succeeded", "outcome_unknown"}

    def __init__(self, path: str | Path) -> None:
        raw_path = str(path).strip()
        if not raw_path or raw_path == ":memory:":
            raise ValueError("A persistent DHL approval ledger path is required")
        self.path = Path(raw_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()
        os.chmod(self.path, 0o600)

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
        """Persist approval consumption atomically across processes and restarts."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO consumed_approvals (
                        approval_id, operation, environment, payload_sha256,
                        approved_by, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        operation,
                        environment,
                        payload_sha256,
                        approved_by,
                        consumed_at.isoformat(),
                    ),
                )
                self._event(
                    connection,
                    event_type="approval_consumed",
                    approval_id=approval_id,
                    operation=operation,
                    environment=environment,
                    payload_sha256=payload_sha256,
                    occurred_at=consumed_at,
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            with self._connect() as connection:
                approval_exists = connection.execute(
                    "SELECT 1 FROM consumed_approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
            if approval_exists:
                raise ApprovalAlreadyConsumed(
                    f"Approval {approval_id!r} has already been consumed"
                ) from error
            raise PayloadAlreadyConsumed(
                "This exact DHL operation payload has already been submitted"
            ) from error

    def begin_submission(
        self,
        *,
        approval_id: str,
        operation: str,
        environment: str,
        payload_sha256: str,
        started_at: datetime,
    ) -> None:
        """Reserve an exact carrier write before any network contact."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO dhl_submissions (
                        environment, operation, payload_sha256, approval_id,
                        state, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'submitting', ?, ?)
                    """,
                    (
                        environment,
                        operation,
                        payload_sha256,
                        approval_id,
                        started_at.isoformat(),
                        started_at.isoformat(),
                    ),
                )
                self._event(
                    connection,
                    event_type="submission_started",
                    approval_id=approval_id,
                    operation=operation,
                    environment=environment,
                    payload_sha256=payload_sha256,
                    occurred_at=started_at,
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            existing = self.get_submission(
                operation=operation,
                environment=environment,
                payload_sha256=payload_sha256,
            )
            state = existing["state"] if existing else "recorded"
            raise SubmissionAlreadyRecorded(
                "Exact DHL operation is already recorded with state "
                f"{state!r}; reconcile it before any retry"
            ) from error

    def complete_submission(
        self,
        *,
        operation: str,
        environment: str,
        payload_sha256: str,
        carrier_reference: str | None,
        completed_at: datetime,
    ) -> None:
        """Record a successful carrier response for a reserved write."""
        self._transition(
            operation=operation,
            environment=environment,
            payload_sha256=payload_sha256,
            state="succeeded",
            occurred_at=completed_at,
            carrier_reference=carrier_reference,
            error_summary=None,
        )

    def mark_outcome_unknown(
        self,
        *,
        operation: str,
        environment: str,
        payload_sha256: str,
        error_summary: str,
        occurred_at: datetime,
    ) -> None:
        """Block retries when a carrier write did not finish cleanly."""
        self._transition(
            operation=operation,
            environment=environment,
            payload_sha256=payload_sha256,
            state="outcome_unknown",
            occurred_at=occurred_at,
            carrier_reference=None,
            error_summary=error_summary[:500],
        )

    def get_submission(
        self,
        *,
        operation: str,
        environment: str,
        payload_sha256: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT environment, operation, payload_sha256, approval_id,
                       state, started_at, updated_at, carrier_reference,
                       error_summary
                FROM dhl_submissions
                WHERE environment = ? AND operation = ? AND payload_sha256 = ?
                """,
                (environment, operation, payload_sha256),
            ).fetchone()
        return dict(row) if row is not None else None

    def _transition(
        self,
        *,
        operation: str,
        environment: str,
        payload_sha256: str,
        state: str,
        occurred_at: datetime,
        carrier_reference: str | None,
        error_summary: str | None,
    ) -> None:
        if state not in self.VALID_STATES:
            raise ValueError(f"Invalid DHL submission state: {state}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE dhl_submissions
                SET state = ?, updated_at = ?, carrier_reference = ?,
                    error_summary = ?
                WHERE environment = ? AND operation = ? AND payload_sha256 = ?
                  AND state = 'submitting'
                """,
                (
                    state,
                    occurred_at.isoformat(),
                    carrier_reference,
                    error_summary,
                    environment,
                    operation,
                    payload_sha256,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise DHLLedgerError(
                    "DHL submission is missing or no longer in submitting state"
                )
            approval_row = connection.execute(
                """
                SELECT approval_id FROM dhl_submissions
                WHERE environment = ? AND operation = ? AND payload_sha256 = ?
                """,
                (environment, operation, payload_sha256),
            ).fetchone()
            self._event(
                connection,
                event_type=f"submission_{state}",
                approval_id=str(approval_row["approval_id"]),
                operation=operation,
                environment=environment,
                payload_sha256=payload_sha256,
                occurred_at=occurred_at,
                detail=carrier_reference or error_summary,
            )
            connection.commit()

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS consumed_approvals (
                    approval_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    UNIQUE (operation, environment, payload_sha256)
                );

                CREATE TABLE IF NOT EXISTS dhl_submissions (
                    environment TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    approval_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('submitting', 'succeeded', 'outcome_unknown')
                    ),
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    carrier_reference TEXT,
                    error_summary TEXT,
                    PRIMARY KEY (environment, operation, payload_sha256),
                    FOREIGN KEY (approval_id)
                        REFERENCES consumed_approvals(approval_id)
                );

                CREATE TABLE IF NOT EXISTS dhl_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        approval_id: str,
        operation: str,
        environment: str,
        payload_sha256: str,
        occurred_at: datetime,
        detail: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO dhl_audit_events (
                event_type, approval_id, operation, environment,
                payload_sha256, occurred_at, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                approval_id,
                operation,
                environment,
                payload_sha256,
                occurred_at.isoformat(),
                detail,
            ),
        )
