# Controlled DHL Shipment Workflow

## Status

The DHL Express MyDHL REST integration has completed sandbox UAT and contains
supervised-production controls. Production remains disabled by default and must
be enabled for one reviewed Atlas draft ID at a time.

- Integration type: direct integration, internally developed for StretchSense.
- Account owner: StretchSense / Sensor Holdings Limited.
- MyDHL test access: approved 23 August 2026.
- Developer application: approved and enabled for Customer (Integration) Testing.
- Sandbox product: `exp-mydhlapi-sandbox-all-m`.
- Production access: must be separately verified in the DHL Developer Portal.
- Credentials: available only through the DHL API Developer Portal.
- Production shipment and pickup creation: default-off; no live UAT completed.
- DHL New Zealand integration support: `cisnz@dhl.com`.

The DHL account number and API credentials belong in local environment variables
or an approved secret manager. They must not be committed, written to reports, or
included in approval snapshots.

For RMA returns, Atlas normally defaults both freight and duties/taxes to the
approved StretchSense DHL account and uses DDP. Explicitly reviewed exceptions
may use another billing arrangement and Incoterm. A one-glove RMA defaults to
20 x 15 x 10 cm when dimensions are absent. An unknown glove return is described as
`faulty Motion capture glove`; model, hand and size remain separate traceability
details when known.

Production requires all of the following independent gates: the production
environment, the explicit production-enable switch, the configured approver,
a persistent SQLite approval ledger, and one exact allowed Atlas draft ID.
Changing only the environment flag is insufficient.

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

The default collection instruction is **Sender to arrange pickup**. Shipment
creation always sends `pickup.isRequested=false`, and customer-facing hand-offs
must state that the sender arranges collection directly with DHL. Atlas may book
a pickup only as a separate operation with a separate explicit approval.

RMA customs defaults are a pro forma invoice, export reason `Faulty - return
for repair/assessment`, `NO COMMERCIAL VALUE`, and the note `Value for customs
purposes only`. Each returned product/unit defaults to a customs value of USD
50. Any exception remains visible in the frozen review payload and requires a
new explicit approval.

MyDHL limits its printable export-reason field to 30 characters. Atlas maps the
approved full wording to `Faulty - repair/assessment` in that field and retains
`Faulty - return for repair/assessment` in the line-item customs detail. The
invoice separately prints the export type as `RETURN`.

For declarable shipments using a DHL-generated customs invoice, Atlas requests
Paperless Trade with `valueAddedServices.serviceCode="WY"`. A reviewed
destination exception may set `customs.paperless_trade=false`; the frozen
payload and approval hash make that exception visible before submission.

Each declarable line item carries separate tariff classifications: `hs_code`
is the 6- or 8-digit outbound export commodity code, and `inbound_hs_code` is
the destination's 8- or 10-digit import tariff code. Atlas does not derive one
from the other. For the reviewed glove return to China these are `950450` and
`9504500000`, respectively.

Atlas also rejects declarable drafts unless the total packed gross weight is
greater than the total line-item commodity net weight, matching DHL NZ's
readiness requirement for invoice weights.

## Current implementation

```text
schemas/shipment-draft.schema.json       Carrier-neutral shipment input
schemas/shipment-approval.schema.json    Exact one-use operation approval
schemas/rma-email-review.schema.json     Review-only Outlook intake contract
schemas/shipment-email-review.schema.json Review-only ordinary shipment intake
workflows/dhl/config.py                  Test/production configuration gate
workflows/dhl/workflow.py                Validation and frozen request snapshot
workflows/dhl/controls.py                Canonical hash and approval consumption
workflows/dhl/ledger.py                  Durable approvals, writes and outcomes
workflows/dhl/mapper.py                  Atlas-to-MyDHL v3.3 request mapping
workflows/dhl/client.py                  MyDHL HTTP boundary
workflows/dhl/documents.py               Document decoding and draft-only manifest
workflows/dhl/outlook_intake.py          Explicit email-to-shipment review boundary
tests/test_dhl_workflow.py               Offline safety and contract tests
```

## Outlook-to-RMA intake

The reusable Outlook trigger is the exact category **Create DHL RMA**. Atlas
accepts only one explicitly categorised message at a time; it does not scan or
monitor the inbox. The Outlook connector or operator supplies that message and
an extracted candidate shipment draft to `build_outlook_rma_review()`.

The intake step:

1. verifies the Outlook message ID, sender, subject and received timestamp;
2. fingerprints the source, including a hash of its body, without copying the
   raw email body into the review manifest;
3. applies the controlled RMA and collection defaults;
4. identifies missing shipment, address, package and customs fields;
5. validates a complete candidate against `shipment-draft.schema.json`; and
6. returns a schema-valid review manifest with every DHL/write flag set to
   `false`.

A `ready_for_validation` result is not shipment approval. The unchanged
candidate must first pass `confirm_outlook_rma_review()`, then proceed through
the existing DHL serviceability, rating, preflight, frozen-payload and explicit
approval controls. Label generation remains a shipment-creation write and can
never be triggered solely by an email category.

The intended operator sequence is:

```text
Apply "Create DHL RMA" category
  -> fetch that one Outlook message
  -> extract candidate fields with evidence
  -> review and complete missing fields
  -> confirm canonical shipment draft
  -> DHL validation/rates/preflight
  -> Dan approves exact payload hash
  -> create shipment without pickup
  -> create unsent Outlook reply draft with documents
```

## Outlook-to-shipment intake

Ordinary supplier, production-material and one-off delivery requests use the
exact Outlook category **Create DHL Shipment**. They are kept separate from
RMA intake so return-specific assumptions cannot leak into commercial or
production shipments.

`build_outlook_shipment_review()` requires the operator or extraction service
to state the shipment type, billing charges, collection arrangement and pickup
choice explicitly. It does not apply the RMA defaults for DDP, USD 50 customs
value, pro forma invoices, faulty-glove wording or 20 x 15 x 10 cm packaging.
Missing origin, destination, contents, quantity, package, customs, billing or
collection data leaves the review in `needs_review`.

The intended operator sequence is:

```text
Apply "Create DHL Shipment" category
  -> fetch that one Outlook message
  -> classify the movement and extract only evidenced fields
  -> review and complete every missing commercial and package field
  -> confirm the unchanged canonical shipment draft
  -> DHL validation/rates/preflight
  -> Dan approves the exact shipment payload hash
  -> create shipment without pickup
  -> if required, separately prepare and approve a pickup operation
  -> create an unsent Outlook hand-off draft
```

The email category is an intake trigger only. It cannot create a shipment,
pickup, label, charge or sent email.

The client exposes address validation and rates as non-chargeable preparation
operations. Shipment and pickup writes require separate `PreparedOperation`
objects and separate approvals. Write requests are never automatically retried:
if a connection fails after submission, the outcome is treated as unknown and
must be reconciled before another attempt.

The SQLite ledger reserves the exact operation before network contact. It
persists approval consumption and submission state across process restarts and
blocks a duplicate payload whether the previous state is `submitting`,
`succeeded` or `outcome_unknown`. A process crash leaves a visible `submitting`
record; any exception after the write begins leaves `outcome_unknown`. Both
require operator reconciliation against MyDHL before a new reviewed operation.

DHL product `Q` (Medical Express) is prohibited by StretchSense policy. It may
appear in a transient rate response, but the mapper will reject it for shipment
creation. Standard international test shipments use product `P` (Express
Worldwide) unless another approved service is selected.

PDF output uses DHL's default `ECOM26_84_001` transport label and `ARCH_8X4`
waybill templates. Labels, waybills and the landscape returns invoice are
returned as separate document images to avoid incompatible printable widths.

Approval records contain hashes of the validated Atlas draft and exact MyDHL
payload, not duplicated addresses, contact details or DHL account numbers.
For operational use, construct `ApprovalGuard` with `SQLiteApprovalLedger` at a
protected local path. Consumption is committed before network contact and is
durable across restarts. The ledger uniquely records both approval IDs and exact
operation/environment/payload hashes, preventing a second approval ID from
silently resubmitting the same frozen request. It stores no shipment addresses,
contacts, package contents, credentials or account numbers.

## Local configuration

Copy `.env.example` to `.env` and populate these values privately:

```dotenv
DHL_API_USERNAME=<portal-issued test username>
DHL_API_PASSWORD=<portal-issued test password>
DHL_ACCOUNT_NUMBER=<approved StretchSense account>
DHL_ENVIRONMENT=test
DHL_ENABLE_PRODUCTION=false
DHL_APPROVER_ID=dan.walker
DHL_APPROVAL_LEDGER_PATH=inventory/dhl/approval-ledger.sqlite3
DHL_PRODUCTION_ALLOWED_DRAFT_ID=
```

`DHL_ENABLE_PRODUCTION=false` is the immediate kill switch. For a controlled
production preflight, set the environment to `production`, enable production,
and set `DHL_PRODUCTION_ALLOWED_DRAFT_ID` to the exact reviewed draft ID. Atlas
will reject every other draft before network contact. Keep the allowed ID blank
whenever no production operation is actively being reviewed.

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
8. Complete UAT using approved return locations and product/customs master data,
   including Paperless Trade (`WY`) and separate inbound/outbound commodity codes.
9. Submit DHL NZ's MyDHL API Integration Readiness Checklist for review.
10. Verify production credentials and account permissions privately.
11. Enable one reviewed draft ID and run `validateDataOnly=true` in production.
12. Freeze the returned production payload and obtain a new explicit approval.
13. Create one pilot shipment without pickup, reconcile the ledger, inspect the
    non-sample documents, then return the kill switch to `false`.

No broad autonomous go-live should be set until return routing, customs
valuation, dangerous-goods wording, account permissions and operating limits
have been verified beyond the first locked pilot.

## Supervising autonomy

Atlas separates autonomy from authority. It may monitor explicitly selected
work, extract data, apply approved defaults, validate, compare rates and prepare
drafts without chargeable authority. Production authority remains bounded by:

- one allowed draft ID at a time;
- an exact, expiring payload-hash approval;
- one-use approval consumption in the durable ledger;
- duplicate and unknown-outcome blocking across restarts;
- separate shipment and pickup approvals;
- no automatic email sending; and
- the `DHL_ENABLE_PRODUCTION=false` kill switch.

The ledger is the audit source for who approved an operation, which exact hash
was submitted, when it started, its final state and any carrier reference. Do
not delete or edit ledger rows to clear a block; reconcile the operation in
MyDHL and prepare a new reviewed operation instead.

## Information still required

- Approved NZ, UK, US, Hong Kong and China return locations and routing rules.
- Product-level customs descriptions, origins, weights, dimensions and values.
- Confirmed HS classifications and dangerous-goods declaration wording.
- RMA valuation, Incoterm, importer/exporter and Return Goods Relief rules.
- Approval expiry and any permitted rate-variance threshold.
- Outlook subject/body template, mailbox and attachment naming convention.

Atlas-generated Outlook hand-offs use a controlled two-line sender signature:
the sender's name on the first line and the company name on the second. The
current defaults are `Dan Walker` and `StretchSense`.

Historical shipping documents are not approved master data. Conflicting GST,
VAT, address and tariff values must remain quarantined until verified.
