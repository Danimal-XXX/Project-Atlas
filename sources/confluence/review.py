"""Quality review for canonical Confluence knowledge packages."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atlas.schema_validator import AtlasSchemaValidator
from sources.confluence.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def review(
    *,
    canonical_dir: str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and inspect all canonical Confluence articles and assets."""
    canonical = Path(canonical_dir or PROJECT_ROOT / "knowledge_base" / "confluence")
    validator = AtlasSchemaValidator(PROJECT_ROOT / "schemas")
    articles = []
    assets = {}
    for path in sorted((canonical / "assets").glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        validator.validate_object(item, "asset.schema.json")
        assets[item["id"]] = item
    for path in sorted((canonical / "articles").glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        validator.validate_object(item, "knowledge.schema.json")
        articles.append(item)
    article_ids = {item["id"] for item in articles}
    findings = []
    details = []
    for item in articles:
        issues = []
        body = item["content"]["body"]
        if not body.strip():
            issues.append("empty_body")
        if "[Confluence macro:" in body:
            issues.append("unconverted_macro")
        if "confluence-page-title://" in body:
            issues.append("unresolved_page_uri")
        warnings = item.get("extensions", {}).get("confluence", {}).get("transform_warnings", [])
        issues.extend(f"transform_warning: {warning}" for warning in warnings)
        missing_assets = [asset_id for asset_id in item.get("assets", []) if asset_id not in assets]
        missing_relationships = [
            relation["target_id"]
            for relation in item.get("relationships", [])
            if relation["target_id"] not in article_ids
        ]
        issues.extend(f"missing_asset: {value}" for value in missing_assets)
        issues.extend(f"missing_relationship: {value}" for value in missing_relationships)
        findings.extend({"article_id": item["id"], "finding": issue} for issue in issues)
        details.append(
            {
                "id": item["id"],
                "title": item["title"],
                "content_characters": len(body),
                "asset_count": len(item.get("assets", [])),
                "relationship_count": len(item.get("relationships", [])),
                "finding_count": len(issues),
            }
        )
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "article_count": len(articles),
        "asset_count": len(assets),
        "articles_with_findings": len({finding["article_id"] for finding in findings}),
        "finding_count": len(findings),
        "finding_types": dict(Counter(finding["finding"].split(":", 1)[0] for finding in findings)),
        "findings": findings,
        "articles": details,
        "passed": not findings,
    }
    destination = Path(output_path or canonical / "review-report.json")
    atomic_write_json(destination, result)
    return result
