# Atlas to Zoho Desk importer

The Zoho publisher has two stages:

1. `publish.py` generates and verifies a portable HTML bundle.
2. `importer.py` creates or updates those articles in Zoho Desk.

The importer is a dry run unless `--apply` is present. It never stores OAuth
access tokens, client secrets, or refresh tokens in import reports.

## Destination

Set the destination to a writable Zoho Desk knowledge-base section. Root
categories may organise sections but do not necessarily accept articles:

```text
ZOHO_CATEGORY_ID=<writable-section-id>
```

## Required `.env` values

```dotenv
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
ZOHO_ORG_ID=
ZOHO_CATEGORY_ID=
ZOHO_ACCOUNTS_URL=https://accounts.zoho.eu
ZOHO_DESK_API_DOMAIN=https://desk.zoho.eu
ZOHO_ARTICLE_LOCALE=en
ZOHO_ARTICLE_PERMISSION=ALL
ZOHO_ARTICLE_STATUS=Draft
```

The refresh token must include these least-privilege Zoho Desk scopes:

```text
Desk.articles.READ,Desk.articles.CREATE,Desk.articles.UPDATE
```

`ZOHO_DESK_API_DOMAIN` is used only with a manually supplied access token or as
a fallback. Normal refresh-token authentication uses the regional `api_domain`
returned by Zoho.

## Safe workflow

Validate the complete local package without credentials or network calls:

```bash
.venv/bin/python -m publishers.zoho.importer
```

Import one article as a draft:

```bash
.venv/bin/python -m publishers.zoho.importer \
  --apply \
  --category-id <writable-zoho-category-id> \
  --accounts-url https://accounts.zoho.eu \
  --status Draft \
  --only confluence-509247712
```

After reviewing that article in Zoho, import the remaining bundle:

```bash
.venv/bin/python -m publishers.zoho.importer \
  --apply \
  --category-id 233447000001584019 \
  --accounts-url https://accounts.zoho.eu
```

The importer checks the destination category for exact permalink and title
matches before creating anything. This is how it adopts earlier manual imports.
It saves destination IDs, asset upload results, checksums, and verification times
to `inventory/zoho/confluence-import-state.json`. Keep this file: it makes later
runs resumable and prevents duplicate articles.

Use `--no-adopt-existing` only when matching manual articles must be left alone.
Use `--force` after adding linked destination articles so already imported drafts
are rendered again with their new Zoho URLs.
Use `--asset-base-url` to reference assets already published at a public base URL
instead of uploading and attaching them to each Zoho article.

Zoho limits each article to 50 attachments and each attachment to 20 MB. The
importer uploads only files actually referenced by the article, embeds the
smallest images when a page exceeds 50 references, and losslessly ZIP-compresses
oversized downloads. This keeps migrated content inside Zoho.

R2 remains an optional deployment for content explicitly approved for public
hosting:

```bash
.venv/bin/python -m publishers.zoho.assets
```

Pass the reported public base URL to `importer.py --asset-base-url`. R2 uploads
are checksum-verified and resumable.

After importing all categories, audit the live drafts before publishing:

```bash
.venv/bin/python -m publishers.zoho.audit
```

The audit checks Draft status, title and category mappings, attachment limits,
image hosts, unresolved local links, Confluence fallbacks between migrated
articles, and expected Zoho-to-Zoho links. Evidence is saved to
`inventory/zoho/confluence-audit.json`.
