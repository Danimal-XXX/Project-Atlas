# Controlled DHL Shipment Workflow

## Status

The DHL Express MyDHL REST integration is in test-only development.

- Integration type: direct integration, internally developed for StretchSense.
- Account owner: StretchSense / Sensor Holdings Limited.
- MyDHL test access: approved 23 August 2026.
- Developer application: approved and enabled for Customer (Integration) Testing.
- Sandbox product: `exp-mydhlapi-sandbox-all-m`.
- Production access: not requested.
- Credentials: available only through the DHL API Developer Portal.
- Production shipment and pickup creation: disabled.
- DHL New Zealand integration support: `cisnz@dhl.com`.

The DHL account number and API credentials belong in local environment variables
or an approved secret manager. They must not be committed, written to reports, or
included in approval snapshots.

Production is structurally blocked in the current code even if the environment
flag is changed. Removing that block requires a reviewed code change after the
durable approval ledger and production-readiness controls are complete.

## Scope

The workflow will support:

1. RMA or shipment intake using a carrier-neutral Atlas draft.
2. Validation of sender, recipient, package, customs, value and destination data.
3. DHL address serviceability checks.
4. Transient retrieval of available products and rates.
5. A frozen review snapshot of the exact intended DHL request.
6. Dan's explicit, expiring, one-use approval for shipment creation.
7. Label and customs-document extraction into a controlled output bundle.
8. A separate approval for pickup creation, if required.
9. An Outlook draft hand-off with attachments and no automatic send operation.

## Current implementation

```text
schemas/shipment-draft.schema.json       Carrier-neutral shipment input
schemas/shipment-approval.schema.json    Exact one-use operation approval
workflows/dhl/config.py                  Test/production configuration gate
workflows/dhl/workflow.py                Validation and frozen request snapshot
workflows/dhl/controls.py                Canonical hash and approval consumption
workflows/dhl/mapper.py                  Atlas-to-MyDHL v3.3 request mapping
workflows/dhl/client.py                  MyDHL HTTP boundary
workflows/dhl/documents.py               Document decoding and draft-only manifest
tests/test_dhl_workflow.py               Offline safety and contract tests
```

The client exposes address validation and rates as non-chargeable preparation
operations. Shipment and pickup writes require separate `PreparedOperation`
objects and separate approvals. Write requests are never automatically retried:
if a connection fails after submission, the outcome is treated as unknown and
must be reconciled before another attempt.

DHL product `Q` (Medical Express) is prohibited by StretchSense policy. It may
appear in a transient rate response, but the mapper will reject it for shipment
creation. Standard international test shipments use product `P` (Express
Worldwide) unless another approved service is selected.

PDF output uses DHL's default `ECOM26_84_001` transport label and `ARCH_8X4`
waybill templates. Labels, waybills and the landscape returns invoice are
returned as separate document images to avoid incompatible printable widths.

Approval records contain hashes of the validated Atlas draft and exact MyDHL
payload, not duplicated addresses, contact details or DHL account numbers.

## Local configuration

Copy `.env.example` to `.env` and populate these values privately:

```dotenv
DHL_API_USERNAME=<portal-issued test username>
DHL_API_PASSWORD=<portal-issued test password>
DHL_ACCOUNT_NUMBER=<approved StretchSense account>
DHL_ENVIRONMENT=test
DHL_ENABLE_PRODUCTION=false
DHL_APPROVER_ID=dan.walker
```

Do not send the credentials in email, chat, screenshots, fixtures or issue
comments. The approved test credentials are viewed under the developer portal's
application page.

## Development and UAT sequence

1. Complete offline schema, transformation and approval tests.
2. Install the test credentials privately.
3. Make one read-only address validation call using synthetic contact data.
4. Make one test rates call and display the result transiently.
5. Build a test shipment payload from approved, non-production fixtures.
6. Call `/shipments?validateDataOnly=true` in the test environment. This checks
   shipment-data compliance without creating a shipment, waybill or label.
7. After Dan explicitly approves the exact test payload, validate label and
   customs-document decoding in DHL's test environment.
8. Complete UAT using approved return locations and product/customs master data.
9. Perform a separate production-readiness review before requesting or enabling production.

No go-live date should be set until return routing, customs valuation, dangerous
goods wording, account permissions, duplicate prevention and the durable approval
ledger have been verified.

## Information still required

- Approved NZ, UK, US, Hong Kong and China return locations and routing rules.
- Product-level customs descriptions, origins, weights, dimensions and values.
- Confirmed HS classifications and dangerous-goods declaration wording.
- RMA valuation, Incoterm, importer/exporter and Return Goods Relief rules.
- Approval expiry and any permitted rate-variance threshold.
- Outlook subject/body template, mailbox and attachment naming convention.

Historical shipping documents are not approved master data. Conflicting GST,
VAT, address and tariff values must remain quarantined until verified.
