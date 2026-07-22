"""Faithfully mirror discovered Confluence pages and attachments into staging."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sources.confluence.client import ConfluenceClient, ConfluenceConfig
from sources.confluence.discover import discover
from sources.confluence.utils import (
    atomic_write_json,
    atomic_write_text,
    page_body,
    safe_filename,
    sha256_text,
)


DEFAULT_STAGING = Path(__file__).resolve().parents[2] / "staging" / "confluence"


def mirror(
    discovery: dict[str, Any] | None = None,
    *,
    root_page_id: str | None = None,
    client: ConfluenceClient | None = None,
    staging_dir: str | Path | None = None,
    include_attachments: bool = True,
    resume: bool = True,
    dry_run: bool = False,
    max_pages: int | None = None,
    fail_fast: bool = False,
) -> dict[str, Any]:
    """Mirror page JSON, storage HTML, and binary attachments with a crawl report."""
    api = client or ConfluenceClient(ConfluenceConfig.from_env(root_page_id=root_page_id))
    found = discovery or discover(
        root_page_id=root_page_id,
        client=api,
        max_pages=max_pages,
    )
    pages = list(found.get("pages", []))
    if max_pages is not None:
        pages = pages[: max(0, max_pages)]
    staging = Path(staging_dir or DEFAULT_STAGING)
    report: dict[str, Any] = {
        "connector": "confluence",
        "root_page_id": found.get("root_page_id"),
        "space_id": found.get("space_id"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "requested_pages": len(pages),
        "mirrored_pages": 0,
        "resumed_pages": 0,
        "attachments_downloaded": 0,
        "attachments_resumed": 0,
        "failures": [],
        "pages": [],
    }
    for summary in pages:
        page_id = str(summary["id"])
        try:
            result = _mirror_page(
                api,
                summary,
                staging,
                include_attachments=include_attachments,
                resume=resume,
                dry_run=dry_run,
            )
            report["pages"].append(result)
            report["mirrored_pages"] += int(result["status"] == "mirrored")
            report["resumed_pages"] += int(result["status"] == "resumed")
            report["attachments_downloaded"] += result["attachments_downloaded"]
            report["attachments_resumed"] += result["attachments_resumed"]
            report["failures"].extend(result.get("failures", []))
        except Exception as error:
            failure = {"page_id": page_id, "message": str(error), "type": type(error).__name__}
            report["failures"].append(failure)
            if fail_fast:
                raise
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["successful"] = not report["failures"]
    if not dry_run:
        atomic_write_json(staging / "discovery.json", found)
        atomic_write_json(staging / "crawl-report.json", report)
    return report


def _mirror_page(
    client: ConfluenceClient,
    summary: dict[str, Any],
    staging: Path,
    *,
    include_attachments: bool,
    resume: bool,
    dry_run: bool,
) -> dict[str, Any]:
    page_id = str(summary["id"])
    page_dir = staging / "pages" / page_id
    page_path = page_dir / "page.json"
    body_path = page_dir / "body.storage.html"
    current_version = str(summary.get("version", {}).get("number", ""))
    page_resumed = False
    page: dict[str, Any]
    body: str
    if resume and page_path.exists() and body_path.exists():
        existing = _read_json(page_path)
        existing_version = str(existing.get("version", {}).get("number", ""))
        if current_version and existing_version == current_version:
            page = existing
            body = body_path.read_text(encoding="utf-8")
            page_resumed = True
    if dry_run:
        return {
            "page_id": page_id,
            "status": "planned",
            "version": current_version,
            "attachments_downloaded": 0,
            "attachments_resumed": 0,
        }
    if not page_resumed:
        page = client.get_page(page_id, body_format="storage")
        body = page_body(page)
    attachments: list[dict[str, Any]] = []
    downloaded = 0
    resumed = 0
    failures: list[dict[str, str]] = []
    if include_attachments:
        for attachment in client.iter_attachments(page_id):
            enriched, was_downloaded = _mirror_attachment(client, attachment, page_dir, resume)
            attachments.append(enriched)
            downloaded += int(was_downloaded)
            resumed += int(not was_downloaded and enriched["download_status"] == "available")
            if enriched["download_status"] in {"failed", "unavailable"}:
                failures.append(
                    {
                        "page_id": page_id,
                        "attachment_id": str(enriched.get("id", "")),
                        "message": str(enriched.get("download_error", "Attachment unavailable")),
                        "type": "AttachmentDownloadError",
                    }
                )
    atomic_write_json(page_path, page)
    atomic_write_text(body_path, body)
    atomic_write_json(page_dir / "attachments.json", attachments)
    atomic_write_json(
        page_dir / "mirror-metadata.json",
        {
            "page_id": page_id,
            "mirrored_at": datetime.now(timezone.utc).isoformat(),
            "source_checksum": sha256_text(body),
            "attachment_count": len(attachments),
        },
    )
    return {
        "page_id": page_id,
        "status": "resumed" if page_resumed else "mirrored",
        "version": str(page.get("version", {}).get("number", "")),
        "attachments_downloaded": downloaded,
        "attachments_resumed": resumed,
        "failures": failures,
    }


def _mirror_attachment(
    client: ConfluenceClient,
    attachment: dict[str, Any],
    page_dir: Path,
    resume: bool,
) -> tuple[dict[str, Any], bool]:
    attachment_id = str(attachment["id"])
    filename = safe_filename(str(attachment.get("title") or attachment_id))
    relative_path = Path("attachments") / f"{attachment_id}-{filename}"
    destination = page_dir / relative_path
    expected_size = attachment.get("fileSize")
    enriched = dict(attachment)
    enriched.update(
        {
            "asset_id": f"confluence-attachment-{attachment_id}",
            "local_path": relative_path.as_posix(),
            "download_status": "missing",
        }
    )
    if resume and destination.is_file() and (
        expected_size is None or destination.stat().st_size == int(expected_size)
    ):
        import hashlib

        enriched["sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
        enriched["size_bytes"] = destination.stat().st_size
        enriched["download_status"] = "available"
        return enriched, False
    download_link = (
        attachment.get("downloadLink")
        or attachment.get("_links", {}).get("download")
    )
    if not download_link:
        enriched["download_status"] = "unavailable"
        enriched["download_error"] = "Confluence returned no download link"
        return enriched, False
    try:
        digest, size = client.download(client.absolute_url(str(download_link)), destination)
    except Exception as error:
        enriched["download_status"] = "failed"
        enriched["download_error"] = str(error)
        return enriched, False
    enriched["sha256"] = digest
    enriched["size_bytes"] = size
    enriched["download_status"] = "available"
    return enriched, True


def _read_json(path: Path, default: Any = None) -> Any:
    import json

    if not path.exists():
        return default
    with path.open(encoding="utf-8") as source:
        return json.load(source)
