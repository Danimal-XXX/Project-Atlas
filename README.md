Project Atlas
Version: 0.1.0 (Foundation)

Project Atlas transforms organisational knowledge into portable, reusable, and publishable assets. Crawl once. Publish anywhere.

# Project Atlas

> Every piece of knowledge should exist only once.

Project Atlas is an automated knowledge migration and management platform designed to preserve, organise and enrich company knowledge.

Originally developed to migrate the StretchSense HelpScout knowledge base, Atlas has evolved into a reusable framework capable of extracting, packaging, indexing and transforming organisational knowledge from multiple sources into a structured, searchable repository.

---

# Vision

Atlas aims to become the single source of truth for organisational knowledge.

Instead of information being scattered across emails, help desks, shared drives, chat histories and employee memory, Atlas captures knowledge once and makes it available everywhere.

The long-term goal is to build a living organisational knowledge system that supports operations, engineering, manufacturing, logistics, software, customer support and future AI-powered assistants.

---

# Core Principles

- Every piece of knowledge should exist only once.
- Automate repetitive work.
- Preserve institutional knowledge.
- Make information searchable.
- Build reusable tools instead of one-off scripts.
- Prefer structured data over manual processes.
- Version everything.
- Document everything.

---

# Current Capabilities

- Crawl HelpScout knowledge bases
- Download article content
- Capture screenshots
- Download embedded images
- Generate clean HTML packages
- Produce plain-text versions
- Create metadata and manifests
- Build searchable output packages
- Generate migration reports
- Track assets and statistics

---

# Project Structure

```
Project Atlas
│
├── crawler/
├── knowledge_base/
│   ├── source/
│   ├── output/
│   └── packages/
│
├── docs/
├── scripts/
├── tests/
├── README.md
└── PHILOSOPHY.md
```

(The structure will evolve as Atlas grows.)

---

# Workflow

```
Knowledge Source
        │
        ▼
   Crawl Content
        │
        ▼
 Download Assets
        │
        ▼
 Normalise Data
        │
        ▼
 Package Articles
        │
        ▼
 Generate Metadata
        │
        ▼
Searchable Knowledge Base
```

## Source plugins and schema enforcement

Atlas discovers source connectors through `sources/*/connector.yaml`. Manifests declare the connector identity, the lifecycle methods it actually supports (`discover`, `mirror`, `inventory`, and `package`), the module for each method, and the schema produced by packaging.

Package output may be one canonical object or an iterable of objects. The core plugin manager validates every packaged object against its declared Draft 2020-12 JSON Schema before returning it to the canonical knowledge pipeline. Invalid objects are rejected with their object ID and precise data/schema paths. Source-only metadata belongs under `extensions`; publishers consume validated canonical objects rather than connector-specific records.

See `ATLAS.md` for the complete connector manifest example and development contract.

## Confluence crawl

The Confluence connector now implements the full source lifecycle. It scopes discovery to a selected root page, preserves raw page JSON and storage-format HTML, downloads attachments, produces CSV/JSON inventory files, transforms content into Markdown, validates canonical knowledge and asset records, and writes a package manifest.

Configure `.env` from `.env.example`, then preview the selected tree without writing files:

```bash
.venv/bin/python -m sources.confluence.crawl \
  --root-page-id 431226993 \
  --limit 3 \
  --dry-run
```

Run the complete resumable crawl with:

```bash
.venv/bin/python -m sources.confluence.crawl \
  --root-page-id 431226993
```

Raw evidence is written under `staging/confluence`, inventory under `inventory/confluence`, and validated canonical objects under `knowledge_base/confluence`. Reruns reuse unchanged page bodies and attachment files. Use `--no-resume` to force retrieval or `--no-attachments` for a metadata/body-only run.

Review the canonical package with:

```bash
.venv/bin/python -c "from sources.confluence.review import review; print(review())"
```

The review validates all canonical articles and assets and reports empty content, unresolved source constructs, missing assets, and broken relationships in `knowledge_base/confluence/review-report.json`.

## Zoho HTML publishing

Generate a portable Zoho-ready HTML bundle from validated canonical objects:

```bash
.venv/bin/python -m publishers.zoho.publish
```

The publisher validates every article and asset before creating:

```text
output/zoho/confluence/
├── articles/              # HTML articles with rewritten internal links
├── assets/                # One deduplicated copy of every referenced asset
├── manifest.json          # Published paths, sizes, and checksums
└── IMPORT_INSTRUCTIONS.md
```

The bundle can be checked without credentials or network access:

```bash
.venv/bin/python -m publishers.zoho.importer
```

After the Zoho OAuth values, organisation ID, and category ID are configured in
`.env`, test one article as a draft before importing the complete set:

```bash
.venv/bin/python -m publishers.zoho.importer \
  --apply \
  --category-id <writable-zoho-category-id> \
  --accounts-url https://accounts.zoho.eu \
  --status Draft \
  --only confluence-509247712

.venv/bin/python -m publishers.zoho.importer \
  --apply \
  --category-id <writable-zoho-category-id>
```

The importer adopts an existing article with the same permalink or exact title,
so manually imported articles are updated rather than duplicated. It uploads and
associates article assets, rewrites internal article links to Zoho portal URLs,
verifies each result, and records Atlas-to-Zoho IDs and checksums in
`inventory/zoho/confluence-import-state.json`. Reruns skip unchanged verified
articles. `--apply` is always required for Zoho writes; the default status is
`Draft`.

## Controlled DHL workflow

Atlas includes a test-only foundation for StretchSense's DHL Express MyDHL API
direct integration. It validates a carrier-neutral shipment draft, freezes and
hashes the exact carrier request, and requires an expiring one-use approval for
each shipment or pickup write. Production is disabled by default and write
requests are not automatically retried when their outcome may be unknown.

See `docs/DHL_WORKFLOW.md` for configuration, safety boundaries, the MyDHL v3.3
request mapper, safe test preflight and remaining master-data requirements. No
DHL credentials or account numbers are stored in the repository.

Outlook RMA intake uses the explicit `Create DHL RMA` message category. It
creates a review-only candidate, reports missing fields and retains only a
source fingerprint rather than the raw email body. The category can never
create a DHL shipment, label, pickup or charge by itself.

---

# Roadmap

### Phase 1
- HelpScout migration
- Packaging
- Asset preservation

### Phase 2
- Documentation indexing
- Cross-linking
- Search improvements
- Incremental updates

### Phase 3
- Multiple source connectors
- Google Drive
- SharePoint
- Confluence
- Notion
- Zendesk

### Phase 4

Atlas becomes the operational knowledge platform for the organisation.

Potential modules include:

- Operations Handbook
- Company Wiki
- Product Documentation
- Manufacturing Procedures
- Logistics Playbooks
- Engineering Knowledge Base
- Decision Register
- Meeting Archive
- Release Notes
- AI Knowledge Assistant

---

# Technologies

- Python
- BeautifulSoup
- Requests
- Selenium
- Git
- Markdown
- JSON

Future:

- SQLite
- Vector Search
- LLM Integration
- Local AI Search
- OCR
- PDF processing

---

# Philosophy

Atlas exists to preserve organisational memory.

Processes change.
People change.
Software changes.

Knowledge should not.

See **docs/PHILOSOPHY.md**.

---

# Status

Current stage:

**Phase 1 - Active Development**

Primary objective:

Complete migration of the StretchSense HelpScout knowledge base and establish the reusable framework for future knowledge migrations.

---

*"Build the system once. Use it forever."*
