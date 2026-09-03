"""Small, guarded client for the DHL Express MyDHL API."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth

from workflows.dhl.config import DHLConfig, DHLEnvironment
from workflows.dhl.controls import ApprovalGuard
from workflows.dhl.ledger import SubmissionAlreadyRecorded
from workflows.dhl.workflow import PreparedOperation


class MyDHLAPIError(RuntimeError):
    """Raised when a MyDHL API request cannot be completed safely."""


class MyDHLClient:
    """MyDHL boundary with read retries and guarded, non-retried writes."""

    def __init__(
        self,
        config: DHLConfig,
        *,
        approval_guard: ApprovalGuard,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        config.assert_environment_safe()
        if config.environment is DHLEnvironment.PRODUCTION:
            if not approval_guard.is_durable:
                raise ValueError("DHL production requires a durable approval guard")
            if approval_guard.expected_approver != config.approver_id:
                raise ValueError(
                    "Approval guard approver does not match DHL_APPROVER_ID"
                )
            assert approval_guard.ledger is not None
            expected_path = config.approval_ledger_path
            if expected_path is None or (
                approval_guard.ledger.path != expected_path.expanduser().resolve()
            ):
                raise ValueError(
                    "Approval guard ledger does not match DHL_APPROVAL_LEDGER_PATH"
                )
        self.config = config
        self.approval_guard = approval_guard
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(config.username, config.password)
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Language": "en",
                "Content-Type": "application/json",
                "User-Agent": "Project-Atlas-DHL/0.1",
            }
        )
        self._sleep = sleeper

    def validate_address(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Check DHL pickup or delivery capability for an address."""
        return self._json(
            "GET",
            "/address-validate",
            retryable=True,
            params=dict(params),
        )

    def get_rates(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Retrieve transient products and rates without persisting the response."""
        return self._json(
            "POST",
            "/rates",
            retryable=True,
            json=dict(payload),
        )

    def validate_shipment(self, prepared: PreparedOperation) -> dict[str, Any]:
        """Validate a complete payload without creating a shipment or label."""
        self._assert_prepared(prepared, "create_shipment", "/shipments")
        self._assert_production_draft_lock(prepared)
        self._assert_pickup_disabled(prepared)
        return self._json(
            "POST",
            "/shipments",
            retryable=True,
            params={"validateDataOnly": "true"},
            json=dict(prepared.dhl_payload),
        )

    def create_shipment(
        self,
        prepared: PreparedOperation,
        approval: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create one shipment only after consuming an exact one-use approval."""
        return self._controlled_write(
            prepared=prepared,
            approval=approval,
            expected_operation="create_shipment",
            path="/shipments",
        )

    def create_pickup(
        self,
        prepared: PreparedOperation,
        approval: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create one pickup under its own explicit one-use approval."""
        return self._controlled_write(
            prepared=prepared,
            approval=approval,
            expected_operation="create_pickup",
            path="/pickups",
        )

    def _controlled_write(
        self,
        *,
        prepared: PreparedOperation,
        approval: Mapping[str, Any],
        expected_operation: str,
        path: str,
    ) -> dict[str, Any]:
        self.config.assert_environment_safe()
        self._assert_prepared(prepared, expected_operation, path)
        self._assert_production_draft_lock(prepared)
        if expected_operation == "create_shipment":
            self._assert_pickup_disabled(prepared)
        approval_id = self.approval_guard.authorise(
            approval=approval,
            operation=expected_operation,
            environment=self.config.environment,
            payload=prepared.envelope,
        )
        ledger = (
            self.approval_guard.ledger
            if self.approval_guard.is_durable
            else None
        )
        now = self.approval_guard.clock()
        if ledger is not None:
            try:
                ledger.begin_submission(
                    approval_id=approval_id,
                    operation=expected_operation,
                    environment=self.config.environment.value,
                    payload_sha256=prepared.payload_sha256,
                    started_at=now,
                )
            except SubmissionAlreadyRecorded as error:
                raise MyDHLAPIError(str(error)) from error
        try:
            result = self._json(
                "POST",
                path,
                retryable=False,
                json=dict(prepared.dhl_payload),
            )
        except Exception as error:
            if ledger is not None:
                ledger.mark_outcome_unknown(
                    operation=expected_operation,
                    environment=self.config.environment.value,
                    payload_sha256=prepared.payload_sha256,
                    error_summary=self._redact(f"{type(error).__name__}: {error}"),
                    occurred_at=self.approval_guard.clock(),
                )
            raise
        if ledger is not None:
            carrier_reference = result.get("shipmentTrackingNumber") or result.get(
                "dispatchConfirmationNumber"
            )
            ledger.complete_submission(
                operation=expected_operation,
                environment=self.config.environment.value,
                payload_sha256=prepared.payload_sha256,
                carrier_reference=(
                    str(carrier_reference) if carrier_reference is not None else None
                ),
                completed_at=self.approval_guard.clock(),
            )
        return result

    def _assert_prepared(
        self, prepared: PreparedOperation, expected_operation: str, path: str
    ) -> None:
        if prepared.operation != expected_operation:
            raise ValueError(
                f"Prepared operation {prepared.operation!r} cannot be sent to {path}"
            )
        if prepared.environment is not self.config.environment:
            raise ValueError("Prepared operation environment does not match DHL client")

    def _assert_production_draft_lock(self, prepared: PreparedOperation) -> None:
        if self.config.environment is not DHLEnvironment.PRODUCTION:
            return
        draft_id = str(prepared.draft.get("id", ""))
        if draft_id != self.config.allowed_production_draft_id:
            raise ValueError(
                "Production draft is not the single DHL_PRODUCTION_ALLOWED_DRAFT_ID"
            )

    @staticmethod
    def _assert_pickup_disabled(prepared: PreparedOperation) -> None:
        pickup = prepared.dhl_payload.get("pickup")
        if not isinstance(pickup, Mapping) or pickup.get("isRequested") is not False:
            raise ValueError("Shipment payload must explicitly disable pickup")

    def _json(
        self,
        method: str,
        path: str,
        *,
        retryable: bool,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            path,
            retryable=retryable,
            **kwargs,
        )
        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError as error:
            raise MyDHLAPIError(f"MyDHL returned invalid JSON for {path}") from error
        if not isinstance(payload, dict):
            raise MyDHLAPIError(f"MyDHL returned a non-object response for {path}")
        application_status = payload.get("status")
        try:
            is_problem = int(application_status) >= 400
        except (TypeError, ValueError):
            is_problem = False
        if is_problem:
            details = payload.get("additionalDetails") or payload.get("detail")
            raise MyDHLAPIError(
                self._redact(
                    f"MyDHL validation failed with status {application_status}: {details}"
                )
            )
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        retryable: bool,
        **kwargs: Any,
    ) -> requests.Response:
        url = f"{self.config.base_url}{path}"
        attempts = self.config.max_read_retries + 1 if retryable else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.config.timeout,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if not retryable:
                    raise MyDHLAPIError(
                        "MyDHL write outcome is unknown; reconcile before any retry"
                    ) from error
                if attempt + 1 == attempts:
                    break
                self._sleep(self._retry_delay(attempt, None))
                continue
            if retryable and (
                response.status_code == 429
                or response.status_code in {500, 502, 503, 504}
            ):
                if attempt + 1 == attempts:
                    last_error = MyDHLAPIError(
                        f"MyDHL returned HTTP {response.status_code} for {path}"
                    )
                    break
                self._sleep(
                    self._retry_delay(attempt, response.headers.get("Retry-After"))
                )
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                detail = self._redact(response.text[:800])
                raise MyDHLAPIError(
                    f"MyDHL returned HTTP {response.status_code} for {path}: {detail}"
                ) from error
            return response
        raise MyDHLAPIError(f"MyDHL request failed for {path}: {last_error}") from last_error

    def _redact(self, text: str) -> str:
        for sensitive in (
            self.config.username,
            self.config.password,
            self.config.account_number,
        ):
            if sensitive:
                text = text.replace(sensitive, "<redacted>")
        return text

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 120.0))
            except ValueError:
                pass
        return min(2**attempt + random.random(), 30.0)
