from __future__ import annotations

import base64
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from atlas.schema_validator import AtlasSchemaValidator, AtlasValidationError
from workflows.dhl.client import MyDHLAPIError, MyDHLClient
from workflows.dhl.config import (
    DHLConfig,
    DHLConfigurationError,
    DHLEnvironment,
    TEST_BASE_URL,
)
from workflows.dhl.controls import ApprovalGuard, ApprovalRejected
from workflows.dhl.documents import (
    MAX_OUTLOOK_ATTACHMENT_BYTES,
    ShipmentDocumentError,
    build_outlook_draft_manifest,
    extract_shipment_documents,
)
from workflows.dhl.mapper import MyDHLMapper, MyDHLMappingError
from workflows.dhl.workflow import ShipmentWorkflow


NOW = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)


def valid_draft(*, pickup_requested: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "id": "rma-12895",
        "shipment_type": "rma_return",
        "rma_number": "RMA-12895",
        "reason": "Evaluation return",
        "billing_account_ref": "stretchsense-nz-primary",
        "sender": {
            "company_name": "Example Customer",
            "contact_name": "Test Sender",
            "phone": "+1 555 0100",
            "email": "sender@example.com",
            "address": {
                "address_line_1": "1 Example Street",
                "city": "San Francisco",
                "state_or_province": "CA",
                "postal_code": "94105",
                "country_code": "US",
            },
        },
        "recipient": {
            "company_name": "StretchSense",
            "contact_name": "Test Receiver",
            "phone": "+64 9 555 0100",
            "email": "receiver@example.com",
            "address": {
                "address_line_1": "1 Example Road",
                "city": "Auckland",
                "postal_code": "1010",
                "country_code": "NZ",
            },
        },
        "packages": [
            {
                "description": "XR gloves for evaluation",
                "weight_kg": 1.2,
                "length_cm": 30,
                "width_cm": 20,
                "height_cm": 10,
            }
        ],
        "customs": {
            "declarable": True,
            "currency": "USD",
            "incoterm": "DAP",
            "invoice_type": "return",
            "export_reason_type": "return",
            "line_items": [
                {
                    "product_ref": "TEST-GLOVE",
                    "description": "Wearable motion capture gloves",
                    "quantity": 1,
                    "unit_value": 100,
                    "hs_code": "9504500000",
                    "country_of_origin": "CN",
                    "net_weight_kg": 0.8,
                }
            ],
        },
        "pickup_requested": pickup_requested,
    }


def approval_for(prepared, *, approval_id: str = "approval-1") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "id": approval_id,
        "operation": prepared.operation,
        "environment": prepared.environment.value,
        "payload_sha256": prepared.payload_sha256,
        "approved_by": "dan.walker",
        "approved_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=30)).isoformat(),
    }


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.auth = None
        self.headers: dict[str, str] = {}
        self.responses = list(responses or [FakeResponse({"ok": True})])
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ShipmentDraftSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = AtlasSchemaValidator()

    def test_valid_return_draft_passes(self) -> None:
        draft = valid_draft()
        self.assertIs(
            self.validator.validate_object(draft, "shipment-draft.schema.json"),
            draft,
        )

    def test_invalid_hs_code_is_rejected(self) -> None:
        draft = valid_draft()
        draft["customs"]["line_items"][0]["hs_code"] = "9504.50"
        with self.assertRaisesRegex(AtlasValidationError, "hs_code"):
            self.validator.validate_object(draft, "shipment-draft.schema.json")

    def test_declarable_shipment_requires_line_items(self) -> None:
        draft = valid_draft()
        draft["customs"]["line_items"] = []
        with self.assertRaises(AtlasValidationError):
            self.validator.validate_object(draft, "shipment-draft.schema.json")

    def test_declarable_line_item_requires_net_weight(self) -> None:
        draft = valid_draft()
        del draft["customs"]["line_items"][0]["net_weight_kg"]
        with self.assertRaisesRegex(AtlasValidationError, "net_weight_kg"):
            self.validator.validate_object(draft, "shipment-draft.schema.json")


class MyDHLMapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapper = MyDHLMapper()

    def test_rate_request_maps_addresses_packages_and_declared_value(self) -> None:
        request = self.mapper.build_rate_request(
            valid_draft(),
            planned_shipping_date_and_time="2026-09-03T09:00:00 GMT+12:00",
            account_number="test-account",
        )
        self.assertEqual(request["accounts"][0]["number"], "test-account")
        self.assertEqual(
            request["customerDetails"]["shipperDetails"]["provinceCode"], "CA"
        )
        self.assertEqual(request["packages"][0]["weight"], 1.2)
        self.assertEqual(request["monetaryAmount"][0]["value"], 100)

    def test_return_shipment_forces_no_pickup_and_return_documents(self) -> None:
        request = self.mapper.build_shipment_request(
            valid_draft(),
            planned_shipping_date_and_time="2026-09-03T09:00:00 GMT+12:00",
            product_code="P",
            account_number="test-account",
            invoice_number="RMA-12895",
            invoice_date="2026-09-02",
        )
        self.assertEqual(request["pickup"], {"isRequested": False})
        invoice_option = request["outputImageProperties"]["imageOptions"][2]
        self.assertEqual(invoice_option["templateName"], "RET_COM_INVOICE_A4_01")
        declaration = request["content"]["exportDeclaration"]
        self.assertEqual(declaration["exportReasonType"], "return")
        self.assertEqual(
            declaration["invoice"]["customerReferences"][0]["typeCode"], "RMA"
        )

    def test_declarable_shipment_requires_invoice_identity(self) -> None:
        with self.assertRaisesRegex(MyDHLMappingError, "invoice number"):
            self.mapper.build_shipment_request(
                valid_draft(),
                planned_shipping_date_and_time="2026-09-03T09:00:00 GMT+12:00",
                product_code="P",
                account_number="test-account",
            )


class DHLConfigurationTests(unittest.TestCase):
    def test_test_is_the_default_safe_environment(self) -> None:
        config = DHLConfig(username="user", password="secret", account_number="123")
        config.assert_environment_safe()
        self.assertEqual(config.base_url, TEST_BASE_URL)

    def test_production_is_blocked_without_explicit_enablement(self) -> None:
        config = DHLConfig(
            username="user",
            password="secret",
            account_number="123",
            environment=DHLEnvironment.PRODUCTION,
        )
        with self.assertRaisesRegex(DHLConfigurationError, "production is disabled"):
            config.assert_environment_safe()

    def test_production_remains_blocked_after_configuration(self) -> None:
        config = DHLConfig(
            username="user",
            password="secret",
            account_number="123",
            environment=DHLEnvironment.PRODUCTION,
            production_enabled=True,
            approver_id="dan.walker",
        )
        with self.assertRaisesRegex(
            DHLConfigurationError, "structurally disabled"
        ):
            config.assert_environment_safe()


class ShipmentDocumentTests(unittest.TestCase):
    def test_documents_are_decoded_for_draft_only_handoff(self) -> None:
        documents = extract_shipment_documents(
            {
                "shipmentTrackingNumber": "TEST-123",
                "documents": [
                    {
                        "imageFormat": "PDF",
                        "typeCode": "label",
                        "content": base64.b64encode(b"test-pdf").decode("ascii"),
                    }
                ],
            }
        )
        self.assertEqual(documents[0].content, b"test-pdf")
        manifest = build_outlook_draft_manifest(
            mailbox="returns@example.com",
            to=["customer@example.com"],
            subject="Your DHL return label",
            body="Please find your return label attached.",
            documents=documents,
        )
        self.assertEqual(manifest["operation"], "create_draft")
        self.assertIs(manifest["send"], False)
        self.assertEqual(manifest["attachments"][0]["size_bytes"], 8)

    def test_invalid_base64_is_rejected(self) -> None:
        with self.assertRaisesRegex(ShipmentDocumentError, "valid base64"):
            extract_shipment_documents(
                {
                    "shipmentTrackingNumber": "TEST-123",
                    "documents": [
                        {
                            "imageFormat": "PDF",
                            "typeCode": "label",
                            "content": "not base64!",
                        }
                    ],
                }
            )

    def test_oversized_outlook_attachment_is_rejected(self) -> None:
        encoded = base64.b64encode(
            b"x" * (MAX_OUTLOOK_ATTACHMENT_BYTES + 1)
        ).decode("ascii")
        with self.assertRaisesRegex(ShipmentDocumentError, "3 MB"):
            extract_shipment_documents(
                {
                    "shipmentTrackingNumber": "TEST-123",
                    "documents": [
                        {
                            "imageFormat": "PDF",
                            "typeCode": "invoice",
                            "content": encoded,
                        }
                    ],
                }
            )

class ApprovalControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = ShipmentWorkflow()
        self.prepared = self.workflow.prepare(
            draft=valid_draft(),
            dhl_payload={
                "plannedShipmentDateAndTime": "2026-09-03T09:00:00 GMT+12:00",
                "pickup": {"isRequested": False},
            },
            operation="create_shipment",
        )
        self.guard = ApprovalGuard(
            expected_approver="dan.walker",
            clock=lambda: NOW + timedelta(minutes=1),
        )

    def test_exact_approval_is_consumed_once(self) -> None:
        approval = approval_for(self.prepared)
        self.guard.authorise(
            approval=approval,
            operation=self.prepared.operation,
            environment=self.prepared.environment,
            payload=self.prepared.envelope,
        )
        with self.assertRaisesRegex(ApprovalRejected, "already been consumed"):
            self.guard.authorise(
                approval=approval,
                operation=self.prepared.operation,
                environment=self.prepared.environment,
                payload=self.prepared.envelope,
            )

    def test_payload_change_invalidates_approval(self) -> None:
        changed = dict(self.prepared.envelope)
        changed["dhl_payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(ApprovalRejected, "payload hash"):
            self.guard.authorise(
                approval=approval_for(self.prepared),
                operation=self.prepared.operation,
                environment=self.prepared.environment,
                payload=changed,
            )

    def test_shipment_and_pickup_must_be_prepared_separately(self) -> None:
        with self.assertRaisesRegex(ValueError, "separate pickup operation"):
            self.workflow.prepare(
                draft=valid_draft(pickup_requested=True),
                dhl_payload={"shipment": "payload"},
                operation="create_shipment",
            )

    def test_shipment_payload_must_explicitly_disable_pickup(self) -> None:
        with self.assertRaisesRegex(ValueError, "pickup.isRequested"):
            self.workflow.prepare(
                draft=valid_draft(),
                dhl_payload={"pickup": {"isRequested": True}},
                operation="create_shipment",
            )


class MyDHLClientTests(unittest.TestCase):
    def _client(self, session: FakeSession) -> MyDHLClient:
        config = DHLConfig(
            username="test-user",
            password="test-password",
            account_number="test-account",
        )
        return MyDHLClient(
            config,
            approval_guard=ApprovalGuard(
                expected_approver="dan.walker",
                clock=lambda: NOW + timedelta(minutes=1),
            ),
            session=session,
            sleeper=lambda _: None,
        )

    def test_address_validation_uses_test_endpoint(self) -> None:
        session = FakeSession([FakeResponse({"address": [{"serviceArea": "AKL"}]})])
        result = self._client(session).validate_address(
            {"type": "delivery", "countryCode": "NZ", "postalCode": "1010"}
        )
        self.assertIn("address", result)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(
            session.calls[0]["url"], f"{TEST_BASE_URL}/address-validate"
        )

    def test_invalid_approval_prevents_network_contact(self) -> None:
        session = FakeSession()
        workflow = ShipmentWorkflow()
        prepared = workflow.prepare(
            draft=valid_draft(),
            dhl_payload={"shipment": "payload", "pickup": {"isRequested": False}},
            operation="create_shipment",
        )
        approval = approval_for(prepared)
        approval["payload_sha256"] = "0" * 64
        with self.assertRaises(ApprovalRejected):
            self._client(session).create_shipment(prepared, approval)
        self.assertEqual(session.calls, [])

    def test_approved_test_shipment_is_submitted_once_without_retry(self) -> None:
        session = FakeSession([FakeResponse({"shipmentTrackingNumber": "TEST-1"})])
        workflow = ShipmentWorkflow()
        prepared = workflow.prepare(
            draft=valid_draft(),
            dhl_payload={"shipment": "payload", "pickup": {"isRequested": False}},
            operation="create_shipment",
        )
        result = self._client(session).create_shipment(
            prepared,
            approval_for(prepared),
        )
        self.assertEqual(result["shipmentTrackingNumber"], "TEST-1")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["url"], f"{TEST_BASE_URL}/shipments")

    def test_validate_data_only_needs_no_approval_and_uses_test_query(self) -> None:
        session = FakeSession([FakeResponse({"status": "valid"})])
        prepared = ShipmentWorkflow().prepare(
            draft=valid_draft(),
            dhl_payload={"shipment": "payload", "pickup": {"isRequested": False}},
            operation="create_shipment",
        )
        result = self._client(session).validate_shipment(prepared)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(session.calls[0]["params"], {"validateDataOnly": "true"})
        self.assertEqual(session.calls[0]["url"], f"{TEST_BASE_URL}/shipments")

    def test_unknown_write_outcome_is_never_retried(self) -> None:
        session = FakeSession([requests.ConnectionError("connection lost")])
        workflow = ShipmentWorkflow()
        prepared = workflow.prepare(
            draft=valid_draft(),
            dhl_payload={"shipment": "payload", "pickup": {"isRequested": False}},
            operation="create_shipment",
        )
        with self.assertRaisesRegex(MyDHLAPIError, "outcome is unknown"):
            self._client(session).create_shipment(
                prepared,
                approval_for(prepared),
            )
        self.assertEqual(len(session.calls), 1)


if __name__ == "__main__":
    unittest.main()
