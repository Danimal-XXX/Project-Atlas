"""Build reproducible CSV and JSON inventories from a Confluence mirror."""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sources.confluence.mirror import DEFAULT_STAGING
from sources.confluence.utils import atomic_write_json, atomic_write_text, page_body, sha256_text


DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "inventory" / "confluence"
FIELDS = [
    "Page ID",
    "Title",
    "Parent ID",
    "Status",
    "Created",
    "Updated",
    "Version",
    "Author ID",
    "Space ID",
    "Source URL",
    "Body Path",
    "Attachment Count",
    "Mirror Status",
    "Content SHA256",
]


def inventory(
    mirror_result: dict[str, Any] | None = None,
    *,
    staging_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    page_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create inventory files without transforming mirrored source content."""
    staging = Path(staging_dir or DEFAULT_STAGING)
    output = Path(output_dir or DEFAULT_OUTPUT)
    selected = set(map(str, page_ids)) if page_ids is not None else None
    rows = [
        _row(page_dir, staging)
        for page_dir in sorted((staging / "pages").glob("*"))
        if selected is None or page_dir.name in selected
    ]
    rows = [row for row in rows if row is not None]
    rows.sort(key=lambda row: (row["Title"].casefold(), row["Page ID"]))
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "pages.csv"
    temporary = csv_path.with_name(
        f"{csv_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path)
    document = {
        "schema_version": "1.0",
        "connector": "confluence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root_page_id": (mirror_result or {}).get("root_page_id"),
        "space_id": (mirror_result or {}).get("space_id"),
        "item_count": len(rows),
        "items": rows,
    }
    atomic_write_json(output / "pages.json", document)
    return document


def _row(page_dir: Path, staging: Path) -> dict[str, Any] | None:
    page_path = page_dir / "page.json"
    if not page_path.is_file():
        return None
    page = json.loads(page_path.read_text(encoding="utf-8"))
    body = page_body(page)
    attachments_path = page_dir / "attachments.json"
    attachments = (
        json.loads(attachments_path.read_text(encoding="utf-8"))
        if attachments_path.is_file()
        else []
    )
    base = page.get("_links", {}).get("base", "")
    webui = page.get("_links", {}).get("webui", "")
    source_url = f"{base}{webui}" if base and webui else webui
    return {
        "Page ID": str(page.get("id", "")),
        "Title": str(page.get("title", "")),
        "Parent ID": str(page.get("parentId") or ""),
        "Status": str(page.get("status", "")),
        "Created": str(page.get("createdAt") or ""),
        "Updated": str(page.get("version", {}).get("createdAt") or ""),
        "Version": str(page.get("version", {}).get("number") or ""),
        "Author ID": str(page.get("authorId") or ""),
        "Space ID": str(page.get("spaceId") or ""),
        "Source URL": source_url,
        "Body Path": (page_dir / "body.storage.html").relative_to(staging).as_posix(),
        "Attachment Count": len(attachments),
        "Mirror Status": "mirrored",
        "Content SHA256": sha256_text(body),
    }
