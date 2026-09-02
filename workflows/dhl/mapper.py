"""Deterministic mapping from an Atlas shipment draft to MyDHL API requests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from workflows.dhl.workflow import ShipmentWorkflow


class MyDHLMappingError(ValueError):
    """Raised when a validated Atlas draft cannot be mapped without guessing."""


class MyDHLMapper:
    """Build MyDHL v3.3 request objects without retaining credentials or rates."""

    def __init__(self, workflow: ShipmentWorkflow | None = None) -> None:
        self.workflow = workflow or ShipmentWorkflow()

    def build_rate_request(
        self,
        draft: Mapping[str, Any],
        *,
        planned_shipping_date_and_time: str,
        account_number: str,
    ) -> dict[str, Any]:
        validated = self.workflow.validate_draft(draft)
        request: dict[str, Any] = {
            "customerDetails": {
                "shipperDetails": self._rate_address(validated["sender"]),
                "receiverDetails": self._rate_address(validated["recipient"]),
            },
            "accounts": [self._shipper_account(account_number)],
            "plannedShippingDateAndTime": self._required_text(
                planned_shipping_date_and_time, "planned shipping date and time"
            ),
            "unitOfMeasurement": "metric",
            "isCustomsDeclarable": validated["customs"]["declarable"],
            "packages": self._packages(validated["packages"], include_description=False),
        }
        if validated["customs"]["declarable"]:
            request["monetaryAmount"] = [
                {
                    "typeCode": "declaredValue",
                    "value": self._declared_value(validated),
                    "currency": validated["customs"]["currency"],
                }
            ]
        return request

    def build_shipment_request(
        self,
        draft: Mapping[str, Any],
        *,
        planned_shipping_date_and_time: str,
        product_code: str,
        account_number: str,
        invoice_number: str | None = None,
        invoice_date: str | None = None,
    ) -> dict[str, Any]:
        validated = self.workflow.validate_draft(draft)
        if validated["pickup_requested"]:
            raise MyDHLMappingError(
                "Shipment creation cannot request pickup; use the separate pickup workflow"
            )
        customs = validated["customs"]
        declarable = customs["declarable"]
        request: dict[str, Any] = {
            "plannedShippingDateAndTime": self._required_text(
                planned_shipping_date_and_time, "planned shipping date and time"
            ),
            "pickup": {"isRequested": False},
            "productCode": self._required_text(product_code, "product code", max_length=6),
            "getRateEstimates": False,
            "accounts": [self._shipper_account(account_number)],
            "outputImageProperties": self._document_options(validated),
            "customerDetails": {
                "shipperDetails": self._shipment_party(validated["sender"]),
                "receiverDetails": self._shipment_party(validated["recipient"]),
            },
            "content": {
                "packages": self._packages(
                    validated["packages"], include_description=True
                ),
                "isCustomsDeclarable": declarable,
                "description": self._content_description(validated),
                "incoterm": customs["incoterm"],
                "unitOfMeasurement": "metric",
            },
        }
        if declarable:
            if not invoice_number or not invoice_date:
                raise MyDHLMappingError(
                    "Declarable shipments require an invoice number and invoice date"
                )
            self._validate_invoice_date(invoice_date)
            request["content"].update(
                {
                    "declaredValue": self._declared_value(validated),
                    "declaredValueCurrency": customs["currency"],
                    "exportDeclaration": self._export_declaration(
                        validated,
                        invoice_number=self._required_text(
                            invoice_number, "invoice number"
                        ),
                        invoice_date=invoice_date,
                    ),
                }
            )
        return request

    @staticmethod
    def _shipper_account(account_number: str) -> dict[str, str]:
        return {
            "typeCode": "shipper",
            "number": MyDHLMapper._required_text(account_number, "account number"),
        }

    @staticmethod
    def _rate_address(party: Mapping[str, Any]) -> dict[str, Any]:
        address = party["address"]
        result = {
            "postalCode": address["postal_code"],
            "cityName": address["city"],
            "countryCode": address["country_code"],
            "addressLine1": address["address_line_1"],
        }
        MyDHLMapper._copy_optional_address_fields(result, address, rates=True)
        return result

    @staticmethod
    def _shipment_party(party: Mapping[str, Any]) -> dict[str, Any]:
        address = party["address"]
        postal_address = {
            "postalCode": address["postal_code"],
            "cityName": address["city"],
            "countryCode": address["country_code"],
            "addressLine1": address["address_line_1"],
        }
        MyDHLMapper._copy_optional_address_fields(postal_address, address, rates=False)
        return {
            "postalAddress": postal_address,
            "contactInformation": {
                "email": party["email"],
                "phone": party["phone"],
                "companyName": party["company_name"],
                "fullName": party["contact_name"],
            },
            "typeCode": "business",
        }

    @staticmethod
    def _copy_optional_address_fields(
        target: dict[str, Any], address: Mapping[str, Any], *, rates: bool
    ) -> None:
        field_map = {
            "address_line_2": "addressLine2",
            "suburb": "countyName",
            "state_or_province": "provinceCode" if rates else "provinceName",
        }
        for source, destination in field_map.items():
            if address.get(source):
                target[destination] = address[source]

    @staticmethod
    def _packages(
        packages: list[Mapping[str, Any]], *, include_description: bool
    ) -> list[dict[str, Any]]:
        mapped = []
        for package in packages:
            item: dict[str, Any] = {
                "weight": package["weight_kg"],
                "dimensions": {
                    "length": package["length_cm"],
                    "width": package["width_cm"],
                    "height": package["height_cm"],
                },
            }
            if include_description:
                item["description"] = package["description"][:70]
            mapped.append(item)
        return mapped

    @staticmethod
    def _document_options(draft: Mapping[str, Any]) -> dict[str, Any]:
        image_options: list[dict[str, Any]] = [
            {
                "typeCode": "label",
                "templateName": "ECOM26_84_A4_001",
                "fitLabelsToA4": True,
            },
            {
                "typeCode": "waybillDoc",
                "isRequested": True,
                "hideAccountNumber": True,
                "numberOfCopies": 1,
            },
        ]
        if draft["customs"]["declarable"]:
            invoice = {
                "typeCode": "invoice",
                "isRequested": True,
                "invoiceType": (
                    "proforma"
                    if draft["customs"]["invoice_type"] == "proforma"
                    else "commercial"
                ),
            }
            if draft["customs"]["invoice_type"] == "return":
                invoice["templateName"] = "RET_COM_INVOICE_A4_01"
            image_options.append(invoice)
        return {
            "printerDPI": 300,
            "encodingFormat": "pdf",
            "imageOptions": image_options,
        }

    @staticmethod
    def _export_declaration(
        draft: Mapping[str, Any], *, invoice_number: str, invoice_date: str
    ) -> dict[str, Any]:
        customs = draft["customs"]
        export_reason = customs["export_reason_type"]
        line_items = []
        for number, item in enumerate(customs["line_items"], start=1):
            line_items.append(
                {
                    "number": number,
                    "description": item["description"],
                    "price": item["unit_value"],
                    "quantity": {
                        "value": item["quantity"],
                        "unitOfMeasurement": "PCS",
                    },
                    "commodityCodes": [
                        {"typeCode": "outbound", "value": item["hs_code"]}
                    ],
                    "exportReasonType": export_reason,
                    "manufacturerCountry": item["country_of_origin"],
                    "weight": {"netValue": item["net_weight_kg"]},
                    "customerReferences": [
                        {"typeCode": "AFE", "value": item["product_ref"]}
                    ],
                }
            )
        invoice: dict[str, Any] = {
            "number": invoice_number,
            "date": invoice_date,
            "totalNetWeight": sum(
                item["net_weight_kg"] for item in customs["line_items"]
            ),
            "totalGrossWeight": sum(
                package["weight_kg"] for package in draft["packages"]
            ),
        }
        if draft.get("rma_number"):
            invoice["customerReferences"] = [
                {"typeCode": "RMA", "value": draft["rma_number"]}
            ]
        declaration: dict[str, Any] = {
            "lineItems": line_items,
            "invoice": invoice,
            "exportReasonType": export_reason,
        }
        if customs.get("original_export_reference"):
            declaration["exportReference"] = customs["original_export_reference"]
        return declaration

    @staticmethod
    def _declared_value(draft: Mapping[str, Any]) -> float:
        return sum(
            item["quantity"] * item["unit_value"]
            for item in draft["customs"]["line_items"]
        )

    @staticmethod
    def _content_description(draft: Mapping[str, Any]) -> str:
        descriptions = [package["description"] for package in draft["packages"]]
        return "; ".join(descriptions)[:70]

    @staticmethod
    def _required_text(value: str, field: str, *, max_length: int | None = None) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MyDHLMappingError(f"{field} is required")
        cleaned = value.strip()
        if max_length is not None and len(cleaned) > max_length:
            raise MyDHLMappingError(f"{field} must not exceed {max_length} characters")
        return cleaned

    @staticmethod
    def _validate_invoice_date(value: str) -> None:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise MyDHLMappingError("invoice date must use YYYY-MM-DD") from error
