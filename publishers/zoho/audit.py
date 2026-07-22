"""Audit Atlas-created Zoho Desk drafts and their rewritten links."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from sources.confluence.utils import atomic_write_json

from .client import ZohoConfig, ZohoDeskClient
from .importer import DEFAULT_BUNDLE, DEFAULT_CANONICAL, ZohoImporterError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATES = [
    PROJECT_ROOT / "inventory" / "zoho" / "confluence-import-state.json",
    PROJECT_ROOT / "inventory" / "zoho" / "confluence-open-sdk-import-state.json",
]
DEFAULT_REPORT = PROJECT_ROOT / "inventory" / "zoho" / "confluence-audit.json"


def audit_import(
    *,
    state_paths: list[str | Path] | None = None,
    bundle_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    report_path: str | Path | None = None,
    client: ZohoDeskClient | None = None,
) -> dict[str, Any]:
    states = [_read_json(Path(path)) for path in (state_paths or DEFAULT_STATES)]
    records: dict[str, dict[str, Any]] = {}
    categories: dict[str, str] = {}
    for state in states:
        category_id = str(state.get("category_id") or "")
        for atlas_id, record in state.get("articles", {}).items():
            if atlas_id in records:
                raise ZohoImporterError(f"Duplicate Atlas ID across state files: {atlas_id}")
            records[atlas_id] = record
            categories[atlas_id] = category_id

    canonical_root = Path(canonical_dir or DEFAULT_CANONICAL)
    canonical = {
        item["id"]: item
        for item in (
            _read_json(path)
            for path in sorted((canonical_root / "articles").glob("*.json"))
        )
    }
    bundle = Path(bundle_dir or DEFAULT_BUNDLE)
    manifest = _read_json(bundle / "manifest.json")
    bundle_articles = {item["id"]: bundle / item["path"] for item in manifest["articles"]}
    filename_to_id = {path.name: atlas_id for atlas_id, path in bundle_articles.items()}
    source_to_id = {
        str(item.get("source", {}).get("url")): atlas_id
        for atlas_id, item in canonical.items()
        if item.get("source", {}).get("url")
    }
    portal_urls = {
        atlas_id: str(record.get("portal_url") or "")
        for atlas_id, record in records.items()
    }

    if client is None:
        first_category = next(iter(categories.values()), None)
        if not first_category:
            raise ZohoImporterError("No Zoho import records were found to audit")
        client = ZohoDeskClient(ZohoConfig.from_env(category_id=first_category))

    results: list[dict[str, Any]] = []
    for atlas_id, record in sorted(records.items()):
        issues: list[str] = []
        zoho_id = str(record.get("zoho_id") or "")
        if not zoho_id:
            results.append(
                {"atlas_id": atlas_id, "zoho_id": None, "issues": ["missing Zoho ID"]}
            )
            continue
        remote = client.get_article(zoho_id)
        if record.get("pending"):
            issues.append("import state is pending")
        if remote.get("status") != "Draft":
            issues.append(f"status is {remote.get('status')!r}, expected 'Draft'")
        if str(remote.get("categoryId")) != categories[atlas_id]:
            issues.append("category does not match import state")
        expected_title = canonical.get(atlas_id, {}).get("title")
        if remote.get("title") != expected_title:
            issues.append("title does not match canonical object")

        answer = str(remote.get("answer") or "")
        soup = BeautifulSoup(answer, "html.parser")
        remote_links = {
            str(tag.get("href") or "").partition("#")[0]
            for tag in soup.find_all("a")
            if tag.get("href")
        }
        for tag in soup.find_all(["a", "img"]):
            attribute = "src" if tag.name == "img" else "href"
            value = str(tag.get(attribute) or "")
            clean = value.partition("#")[0]
            if value.startswith("../assets/") or "atlas-asset://" in value:
                issues.append(f"unresolved asset link: {value}")
            if (
                clean
                and not clean.startswith(("http://", "https://", "mailto:", "data:"))
                and Path(clean).suffix == ".html"
            ):
                issues.append(f"unresolved article link: {value}")
            fallback_target = source_to_id.get(clean)
            if fallback_target in records and fallback_target != atlas_id:
                issues.append(f"Confluence fallback remains for {fallback_target}")
        for image in soup.find_all("img"):
            source = str(image.get("src") or "")
            if not (
                source.startswith("data:image/")
                or ("/translations/" in source and "/attachments/" in source)
            ):
                issues.append(f"unexpected image host: {source}")

        local = BeautifulSoup(bundle_articles[atlas_id].read_text(encoding="utf-8"), "html.parser")
        for link in local.find_all("a"):
            href = str(link.get("href") or "")
            target_id = filename_to_id.get(Path(href.partition("#")[0]).name)
            if target_id in records:
                expected_url = portal_urls[target_id]
                if not expected_url or expected_url not in remote_links:
                    issues.append(f"missing Zoho link to {target_id}")

        attachments = list(client.iter_attachments(zoho_id, "en"))
        if len(attachments) > 50:
            issues.append(f"attachment count exceeds Zoho limit: {len(attachments)}")
        results.append(
            {
                "atlas_id": atlas_id,
                "zoho_id": zoho_id,
                "title": expected_title,
                "status": remote.get("status"),
                "category_id": categories[atlas_id],
                "attachment_count": len(attachments),
                "issues": sorted(set(issues)),
            }
        )

    excluded = sorted(set(canonical) - set(records))
    issue_count = sum(len(item["issues"]) for item in results)
    report = {
        "schema_version": "1.0",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "canonical_article_count": len(canonical),
        "zoho_draft_count": len(records),
        "excluded_canonical_ids": excluded,
        "passed": issue_count == 0,
        "issue_count": issue_count,
        "articles": results,
    }
    atomic_write_json(Path(report_path or DEFAULT_REPORT), report)
    return report


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ZohoImporterError(f"Required audit file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ZohoImporterError(f"Audit file must contain an object: {path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", action="append", default=[])
    parser.add_argument("--bundle-dir")
    parser.add_argument("--canonical-dir")
    parser.add_argument("--report-path")
    arguments = parser.parse_args()
    report = audit_import(
        state_paths=arguments.state_path or None,
        bundle_dir=arguments.bundle_dir,
        canonical_dir=arguments.canonical_dir,
        report_path=arguments.report_path,
    )
    print(
        f"Zoho audit: drafts={report['zoho_draft_count']}, "
        f"issues={report['issue_count']}, passed={report['passed']}."
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
