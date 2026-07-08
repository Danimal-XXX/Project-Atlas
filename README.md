Project Atlas
Version: 0.1.0 (Foundation)

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