"""Package mirrored Confluence pages as canonical Atlas knowledge objects."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from sources.confluence.mirror import DEFAULT_STAGING
from sources.confluence.transform import storage_to_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = PROJECT_ROOT / "inventory" / "confluence" / "pages.csv"


def package(
    inventory_path: str | Path | None = None,
    *,
    staging_dir: str | Path | None = None,
    ingested_at: datetime | None = None,
    page_ids: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield canonical objects from a raw mirror, falling back to legacy inventory."""
    timestamp = (ingested_at or datetime.now(timezone.utc)).isoformat()
    staging = Path(staging_dir or DEFAULT_STAGING)
    selected = set(map(str, page_ids)) if page_ids is not None else None
    page_dirs = [
        path
        for path in sorted((staging / "pages").glob("*"))
        if path.is_dir() and (selected is None or path.name in selected)
    ]
    if page_dirs:
        yield from _package_mirror(page_dirs, timestamp, staging)
        return
    yield from _package_inventory(Path(inventory_path or DEFAULT_INVENTORY), timestamp)


def package_assets(
    *,
    staging_dir: str | Path | None = None,
    page_ids: list[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield canonical asset records from successfully mirrored attachments."""
    staging = Path(staging_dir or DEFAULT_STAGING)
    selected = set(map(str, page_ids)) if page_ids is not None else None
    for page_dir in sorted((staging / "pages").glob("*")):
        if selected is not None and page_dir.name not in selected:
            continue
        attachments_path = page_dir / "attachments.json"
        if not attachments_path.is_file():
            continue
        for attachment in _read_json(attachments_path, []):
            if attachment.get("download_status") != "available":
                continue
            yield {
                "schema_version": "1.0",
                "id": attachment["asset_id"],
                "filename": str(attachment.get("title") or Path(attachment["local_path"]).name),
                "media_type": str(attachment.get("mediaType") or "application/octet-stream"),
                "size_bytes": int(attachment.get("size_bytes") or attachment.get("fileSize") or 0),
                "sha256": attachment["sha256"],
                "local_path": (page_dir / attachment["local_path"]).relative_to(staging).as_posix(),
                "source": {
                    "connector": "confluence",
                    "external_id": str(attachment["id"]),
                    "container_external_id": str(attachment.get("pageId") or page_dir.name),
                    "url": _source_url(attachment),
                },
                "status": "available",
                "extensions": {
                    "confluence": {
                        "version": attachment.get("version", {}).get("number"),
                        "comment": attachment.get("comment"),
                    }
                },
            }


def _package_mirror(
    page_dirs: list[Path], timestamp: str, staging: Path
) -> Iterator[dict[str, Any]]:
    pages = {
        page_dir.name: _read_json(page_dir / "page.json")
        for page_dir in page_dirs
        if (page_dir / "page.json").is_file()
    }
    children: dict[str, list[str]] = {}
    discovery = _read_json(staging / "discovery.json", {})
    pages_by_title = dict(discovery.get("link_targets", {}))
    pages_by_title.update(
        {
            str(page.get("title") or ""): f"atlas-knowledge://confluence-{page_id}"
            for page_id, page in pages.items()
        }
    )
    for page_id, page in pages.items():
        parent_id = str(page.get("parentId") or "")
        if parent_id in pages:
            children.setdefault(parent_id, []).append(page_id)
    for page_id in sorted(pages, key=lambda value: (str(pages[value].get("title", "")).casefold(), value)):
        page = pages[page_id]
        page_dir = next(path for path in page_dirs if path.name == page_id)
        storage_html = (page_dir / "body.storage.html").read_text(encoding="utf-8")
        attachments = _read_json(page_dir / "attachments.json", [])
        transformed = storage_to_markdown(
            storage_html,
            attachments,
            pages_by_title,
            current_page_id=page_id,
            current_page_title=str(page.get("title") or ""),
            confluence_base_url=discovery.get("base_url") or os.getenv("ATLASSIAN_BASE_URL"),
        )
        parent_id = str(page.get("parentId") or "") or None
        relationships = []
        if parent_id and parent_id in pages:
            relationships.append({"type": "parent", "target_id": f"confluence-{parent_id}"})
        relationships.extend(
            {"type": "child", "target_id": f"confluence-{child_id}"}
            for child_id in sorted(children.get(page_id, []))
        )
        version = page.get("version", {})
        yield {
            "schema_version": "1.0",
            "id": f"confluence-{page_id}",
            "title": str(page.get("title") or f"Confluence page {page_id}"),
            "slug": _slug(str(page.get("title") or ""), page_id),
            "status": _status(str(page.get("status") or "")),
            "content": {"format": "markdown", "body": transformed.markdown, "language": "en"},
            "source": {
                "connector": "confluence",
                "external_id": page_id,
                "url": _source_url(page),
                "space": str(page.get("spaceId") or ""),
                "parent_external_id": parent_id,
                "revision": version.get("number"),
            },
            "timestamps": {
                "created_at": _date_time(page.get("createdAt")),
                "updated_at": _date_time(version.get("createdAt")),
                "ingested_at": timestamp,
            },
            "taxonomy": {"tags": [], "products": [], "audiences": []},
            "assets": sorted(
                attachment["asset_id"]
                for attachment in attachments
                if attachment.get("download_status") == "available"
            ),
            "relationships": relationships,
            "checksums": {
                "source": hashlib.sha256(storage_html.encode("utf-8")).hexdigest(),
                "content": hashlib.sha256(transformed.markdown.encode("utf-8")).hexdigest(),
            },
            "extensions": {
                "confluence": {
                    "author_id": page.get("authorId"),
                    "owner_id": page.get("ownerId"),
                    "position": page.get("position"),
                    "source_status": page.get("status"),
                    "body_representation": "storage",
                    "transform_warnings": list(transformed.warnings),
                }
            },
        }


def _package_inventory(path: Path, timestamp: str) -> Iterator[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as inventory_file:
        for row in csv.DictReader(inventory_file):
            external_id = row["Page ID"].strip()
            title = row["Title"].strip()
            yield {
                "schema_version": "1.0",
                "id": f"confluence-{external_id}",
                "title": title,
                "slug": _slug(title, external_id),
                "status": _status(row.get("Status", "")),
                "content": {"format": "markdown", "body": "", "language": "en"},
                "source": {
                    "connector": "confluence",
                    "external_id": external_id,
                    "parent_external_id": (row.get("Parent ID") or "").strip() or None,
                    "revision": (row.get("Version") or "").strip() or None,
                },
                "timestamps": {
                    "created_at": _date_time(row.get("Created")),
                    "updated_at": _date_time(row.get("Updated")),
                    "ingested_at": timestamp,
                },
                "taxonomy": {"tags": [], "products": [], "audiences": []},
                "assets": [],
                "relationships": [],
                "extensions": {"confluence": {"source_status": row.get("Status")}},
            }


def _source_url(item: dict[str, Any]) -> str:
    links = item.get("_links", {})
    value = item.get("webuiLink") or links.get("webui") or links.get("download") or item.get("downloadLink") or ""
    if str(value).startswith(("http://", "https://")):
        return str(value)
    base = links.get("base") or os.getenv("ATLASSIAN_BASE_URL", "")
    return urljoin(str(base).rstrip("/") + "/", str(value).lstrip("/")) if base and value else ""


def _slug(title: str, external_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or f"page-{external_id}"


def _date_time(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:-1] + "+00:00" if text.endswith("Z") else text


def _status(value: str) -> str:
    return {"current": "published", "draft": "draft", "archived": "archived"}.get(
        value.casefold(), "review"
    )


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
