# Atlas Development Guide

This document defines the permanent development rules for Project Atlas. Apply it to all core modules, source connectors, publishers, schemas, migrations, and tests.

## Purpose

Atlas is a reusable knowledge-preservation and migration framework. It discovers knowledge in source systems, mirrors it faithfully, inventories it, converts it into a canonical Atlas representation, and publishes it to destination systems without making those destinations the source of truth.

Atlas must preserve enough source context to audit, repeat, and verify every migration.

## Guiding Principle

> Every piece of knowledge should exist only once.

Canonical knowledge belongs in Atlas. Source mirrors preserve evidence; they are not competing canonical copies. Publisher output is derived and disposable: it must be possible to regenerate it from canonical Atlas data.

When several sources describe the same knowledge, retain every source record and provenance link, but create or select one canonical knowledge object rather than maintaining duplicate articles.

## Architecture

Keep responsibilities separated:

- `atlas/`: shared orchestration, discovery, downloading, inventory, validation, packaging, and publishing infrastructure. Core code must not contain source- or destination-specific business logic.
- `sources/`: thin ingestion connectors such as Confluence, HelpScout, and WordPress. A source reads external data and produces Atlas-compatible records.
- `publishers/`: destination adapters such as Zoho, Markdown, or Cloudflare R2. A publisher consumes canonical Atlas objects and must not modify them.
- `schemas/`: versioned contracts for canonical knowledge, inventories, manifests, assets, and related data.
- `inventory/`: discovery manifests describing what exists at each source and its processing state.
- `staging/`: reproducible working data and faithful source mirrors. Staging content is not canonical.
- `knowledge_base/`: the canonical, normalized knowledge repository.
- `output/`: generated packages, reports, and publisher artifacts. Output must be safe to recreate.

Dependencies flow toward shared Atlas contracts. Connectors and publishers may depend on `atlas/` and `schemas/`; the core engine must not depend on individual connectors or publishers.

## Canonical Pipeline

Every source follows the same lifecycle:

```text
discover -> mirror -> inventory -> package -> publish
```

1. `discover` identifies available spaces, collections, pages, records, and assets without transforming them.
2. `mirror` captures source content and assets faithfully into staging.
3. `inventory` records discovered items, hierarchy, status, checksums, and locations in a machine-readable manifest.
4. `package` normalizes mirrored records into schema-valid canonical Atlas objects.
5. `publish` renders canonical objects for a destination and verifies the result.

Stages must be independently repeatable and idempotent where the source or destination permits it. Do not bypass inventory or schema validation to move data directly from a source into a publisher.

## Source Connector Interface

Each source connector must expose a consistent interface, directly or through an adapter:

```python
discover(config) -> DiscoveryResult
mirror(discovery, config) -> MirrorResult
inventory(mirror_result, config) -> InventoryResult
package(inventory_result, config) -> PackageResult
```

A connector must:

- declare its name, version, entry point, capabilities, and outputs in `connector.yaml`;
- use shared Atlas services for common work instead of duplicating them;
- preserve page hierarchy, source identifiers, URLs, revisions, timestamps, authorship, permissions metadata when relevant, and attachment relationships;
- support resumable or incremental processing when practical;
- return structured results and actionable errors rather than relying on console output;
- never mutate the source system during ingestion unless an explicitly named operation requires it.

Source-specific fields that do not belong in the canonical model go into the schema-approved extensions area.

### Connector manifest contract

Source plugins are discovered from `sources/*/connector.yaml`. A connector advertises only lifecycle methods that are currently callable. Every advertised method maps to a Python module containing a same-named function. A package stage must explicitly declare its output schema:

```yaml
name: Confluence
version: "1.0"
type: source
supports:
  - package
modules:
  package: package.py
outputs:
  inventory:
    path: inventory/confluence/pages.csv
  package:
    path: knowledge_base
    schema: knowledge.schema.json
```

`PluginManager.call("confluence", "package")` accepts a single object or an iterable from the connector. It materializes iterable results and validates every item with Draft 2020-12 before returning anything downstream. Validation failures identify the object ID, JSON path, schema path, and reason. A publisher must consume these validated results or validate canonical input itself; direct connector-to-publisher paths are prohibited.

Canonical publishers use `atlas.publisher.validate_publisher_input()` at their public boundary before writes or network requests. Older file-package migration scripts predate the canonical object contract; treat them as legacy adapters until they are migrated, and do not use them as a bypass for new connector pipelines.

### Confluence reference lifecycle

Confluence is the reference implementation of the complete source contract:

- `discover()` resolves the space from a configured root page and includes only that page and its accessible descendants.
- `mirror()` saves unmodified API JSON, storage-format HTML, attachment metadata, and attachment binaries using atomic writes.
- `inventory()` derives reproducible CSV and JSON inventories from the raw mirror.
- `package()` transforms storage HTML into Markdown, rewrites attachments to stable `atlas-asset://` references, and emits canonical knowledge objects.
- `sources.confluence.crawl` orchestrates these stages, validates inventory, knowledge, assets, and the final manifest, then persists canonical output.

Confluence crawls are resumable by page version and attachment size/checksum. HTTP 429 and transient server failures use bounded retry with backoff. Every run writes a crawl report; missing or failed attachments remain visible and make the run unsuccessful rather than being silently omitted.

### Zoho reference publisher

`publishers.zoho.publish` is the reference canonical publisher. It:

- accepts only schema-valid Atlas knowledge objects;
- validates canonical asset records before any output is generated;
- renders Markdown, tables, code blocks, and links as HTML;
- rewrites `atlas-knowledge://` references to bundle-local article paths;
- rewrites `atlas-asset://` references to one deduplicated asset directory;
- verifies copied asset checksums;
- emits a schema-valid publisher manifest and reports unresolved links as issues.

The HTML bundle is derived, disposable output. It must never replace the canonical objects in `knowledge_base/`. Uploading to the Zoho API is a separate authenticated operation requiring an explicit destination mapping and post-publish verification.

`publishers.zoho.importer` provides that authenticated operation. It is dry-run
by default, validates the complete bundle before contacting Zoho, refreshes OAuth
tokens using the configured regional account server, and uses the `api_domain`
returned by Zoho. The importer creates draft skeletons first, then uploads assets,
rewrites internal links to destination URLs, applies the requested status, and
fetches every changed article for verification. Its durable import state maps
Atlas IDs and checksums to Zoho IDs so interrupted runs resume and later runs
update or skip instead of duplicating articles. Existing articles may be adopted
only by exact permalink or case-insensitive exact title match.

## Publisher Interface

Each publisher must expose a consistent interface, directly or through an adapter:

```python
publish(objects, config) -> PublishResult
verify(publish_result, config) -> VerificationResult
checksum(item) -> str
manifest(publish_result) -> PublisherManifest
```

A publisher must:

- accept schema-valid canonical Atlas objects rather than connector-specific data;
- map stable Atlas IDs to destination IDs so reruns update instead of duplicate;
- preserve canonical relationships and asset references as far as the destination supports;
- report unsupported fields or lossy transformations;
- produce a manifest of attempted, created, updated, skipped, and failed items;
- avoid modifying `knowledge_base/` or source mirrors;
- verify published content instead of treating a successful request as proof of completion.

Publishing should be idempotent and dry-run capable wherever practical.

## Schema-First Rules

- Define or update the applicable versioned schema before introducing a new canonical field or object type.
- Validate connector package output before writing it to `knowledge_base/`.
- Validate canonical input before publishing.
- Keep schemas source-neutral and destination-neutral.
- Do not silently discard unknown, invalid, or unsupported data. Reject it with a useful error or preserve it in an approved extensions field.
- Treat schema changes as API changes. Document compatibility, migrations, and version increments.
- Tests must include valid examples and representative invalid examples for each schema.

## Provenance and Stable IDs

Every canonical knowledge object must have a stable Atlas ID that remains unchanged across recrawls, content edits, path changes, and republishes.

Stable IDs must not be based only on titles, slugs, list positions, or mutable URLs. Maintain explicit mappings between:

- Atlas ID;
- source connector and source external ID;
- original source URL and revision;
- destination system and destination ID.

Preserve source provenance, including available creation and modification timestamps, author, hierarchy, revision, source checksum, and ingestion timestamp. Never overwrite source identifiers or represent enriched metadata as if it came from the source.

## Asset Handling

- Mirror images, documents, archives, video, and other referenced files when access and policy allow.
- Assign each asset a stable Atlas asset ID and record its source URL, media type, size, checksum, original filename, and owning or referencing objects.
- Use content checksums to identify identical binaries. Store one canonical asset where possible and retain all provenance references.
- Rewrite canonical content to reference Atlas assets, not temporary authenticated source URLs.
- Preserve original filenames as metadata, but use safe deterministic storage paths.
- Record missing, inaccessible, corrupt, or unsupported assets explicitly; never silently omit them.
- Do not execute or unpack untrusted attachments unless a controlled, validated step requires it.

## Deduplication

Deduplication must be evidence-based, reversible, and auditable.

- Exact checksums may establish identical content, but metadata and provenance from every source must still be retained.
- Similar titles, embeddings, keywords, or model scores may suggest duplicates; they must not automatically prove equivalence.
- Prefer marking duplicate candidates for review over destructive automatic merges.
- A merge selects or creates one canonical object and records aliases, contributing sources, prior Atlas IDs, and the merge decision.
- Never delete source mirrors as part of deduplication.
- When knowledge differs by product version, audience, procedure, or supported platform, model the distinction rather than forcing a merge.
- Superseded and archived knowledge remains traceable and must point to its replacement when known.

## Configuration and Secrets

- Keep non-secret configuration in documented configuration files or explicit command options.
- Load credentials and tokens from environment variables or an approved secret store.
- Never commit secrets, session cookies, API tokens, private keys, or credential-bearing URLs.
- Provide safe example configuration with placeholder values.
- Validate configuration at startup and identify missing fields without printing secret values.
- Redact secrets and sensitive personal data from logs, exceptions, fixtures, and reports.
- Set network timeouts, bounded retries, backoff, rate-limit handling, and user-agent identification where applicable.

## Testing Expectations

Every material change must include tests proportional to its risk.

- Unit-test parsing, normalization, ID generation, mapping, validation, and error handling.
- Use fixtures for external API responses; routine tests must not depend on live services.
- Add contract tests showing that every connector produces schema-valid objects and every publisher accepts canonical input.
- Test idempotency, resume behavior, pagination, rate limits, missing assets, malformed content, and partial failures where relevant.
- Include an end-to-end test for the canonical pipeline using a small deterministic fixture set.
- Assert manifests, checksums, provenance, relationships, and failure reports, not only output file existence.
- A bug fix should include a regression test whenever practical.
- Keep tests deterministic and ensure temporary data is isolated from real inventory, staging, knowledge, and output directories.

## Python Conventions

- Target the Python version declared by the repository and use a project-managed virtual environment.
- Follow PEP 8, use clear names, and keep functions and modules focused on one responsibility.
- Add type hints to public interfaces and important internal boundaries.
- Use `pathlib.Path` for paths, timezone-aware UTC datetimes, and explicit UTF-8 encoding.
- Use dataclasses or typed models for structured internal results rather than loosely shaped dictionaries at module boundaries.
- Prefer dependency injection for clients, clocks, configuration, and storage so behavior is testable.
- Use the `logging` module for operational output; do not use `print` inside reusable library code.
- Raise specific exceptions with useful context, preserving the original exception when wrapping it.
- Avoid broad exception handling, hidden global state, connector-specific branches in core modules, and import-time side effects.
- Keep network and filesystem operations behind small interfaces and make destructive operations explicit.
- Write docstrings for public modules, classes, and functions when their contract is not obvious from the signature.
- Run the repository's formatter, linter, type checker, schema validation, and test suite before completing a change.

## Definition of Done

A change is done when:

- its behavior and boundaries match this architecture;
- applicable schemas and migrations are complete;
- provenance, stable IDs, relationships, and assets are preserved;
- tests cover normal behavior and meaningful failure cases and pass locally;
- formatting, linting, typing, and validation pass using the repository's configured tools;
- configuration and user/developer documentation are updated;
- logs and errors are actionable and contain no secrets;
- generated inventory, canonical data, and output can be reproduced without manual repair;
- no source-specific logic has leaked into the core and no publisher has become a source of truth.
