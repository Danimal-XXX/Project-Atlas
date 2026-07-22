"""Publish validated canonical Atlas objects as a portable Zoho HTML bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown
from bs4 import BeautifulSoup

from atlas.publisher import validate_publisher_input
from atlas.schema_validator import AtlasSchemaValidator
from sources.confluence.utils import atomic_write_json, atomic_write_text, safe_filename


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANONICAL = PROJECT_ROOT / "knowledge_base" / "confluence"
DEFAULT_STAGING = PROJECT_ROOT / "staging" / "confluence"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "zoho" / "confluence"


def publish(
    objects: list[dict[str, Any]] | None = None,
    *,
    canonical_dir: str | Path | None = None,
    staging_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Generate validated HTML articles and one checksum-verified asset repository."""
    canonical = Path(canonical_dir or DEFAULT_CANONICAL)
    staging = Path(staging_dir or DEFAULT_STAGING)
    output = Path(output_dir or DEFAULT_OUTPUT)
    loaded = objects if objects is not None else _load_json_objects(canonical / "articles")
    articles = list(validate_publisher_input(loaded))
    validator = AtlasSchemaValidator(PROJECT_ROOT / "schemas")
    asset_records = {
        item["id"]: item
        for item in _load_json_objects(canonical / "assets")
        if validator.validate_object(item, "asset.schema.json")
    }
    referenced_asset_ids = sorted(
        {asset_id for article in articles for asset_id in article.get("assets", [])}
    )
    missing_assets = [asset_id for asset_id in referenced_asset_ids if asset_id not in asset_records]
    if missing_assets:
        raise ValueError("Canonical assets are missing: " + ", ".join(missing_assets))
    article_paths = {
        article["id"]: f"{article['slug']}--{article['id']}.html" for article in articles
    }
    asset_paths = {
        asset_id: _asset_filename(asset_records[asset_id]) for asset_id in referenced_asset_ids
    }
    output_articles = output / "articles"
    output_assets = output / "assets"
    article_manifest = []
    issues: list[str] = []
    for article in articles:
        html, article_issues = _article_html(
            article,
            article_paths=article_paths,
            asset_paths=asset_paths,
        )
        issues.extend(f"{article['id']}: {issue}" for issue in article_issues)
        destination = output_articles / article_paths[article["id"]]
        atomic_write_text(destination, html)
        article_manifest.append(
            {
                "id": article["id"],
                "title": article["title"],
                "path": destination.relative_to(output).as_posix(),
                "sha256": _sha256(destination),
            }
        )
    asset_manifest = []
    for asset_id in referenced_asset_ids:
        record = asset_records[asset_id]
        source = staging / record["local_path"]
        destination = output_assets / asset_paths[asset_id]
        _atomic_copy(source, destination)
        checksum = _sha256(destination)
        if checksum != record["sha256"]:
            raise ValueError(f"Published asset checksum mismatch: {asset_id}")
        asset_manifest.append(
            {
                "id": asset_id,
                "path": destination.relative_to(output).as_posix(),
                "sha256": checksum,
                "size_bytes": destination.stat().st_size,
            }
        )
    manifest = {
        "schema_version": "1.0",
        "publisher": "zoho-html",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_connector": "confluence",
        "article_count": len(article_manifest),
        "asset_count": len(asset_manifest),
        "articles": article_manifest,
        "assets": asset_manifest,
        "issues": issues,
    }
    validator.validate_object(manifest, "publisher-manifest.schema.json")
    atomic_write_json(output / "manifest.json", manifest)
    atomic_write_text(output / "IMPORT_INSTRUCTIONS.md", _instructions(manifest))
    return manifest


def _article_html(
    article: dict[str, Any],
    *,
    article_paths: dict[str, str],
    asset_paths: dict[str, str],
) -> tuple[str, list[str]]:
    body = markdown.markdown(
        article["content"]["body"],
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )
    soup = BeautifulSoup(body, "html.parser")
    issues = []
    for code in list(soup.select("pre > code")):
        image_match = re.fullmatch(
            r"!\[(.*?)\]\((atlas-asset://[^)]+)\)",
            code.get_text(strip=True),
        )
        if image_match:
            image = soup.new_tag("img")
            image["alt"] = image_match.group(1)
            image["src"] = image_match.group(2)
            code.parent.replace_with(image)
    for tag in soup.find_all(["a", "img"]):
        attribute = "src" if tag.name == "img" else "href"
        value = str(tag.get(attribute) or "")
        knowledge_match = re.match(r"^atlas-knowledge://([^#]+)(#.*)?$", value)
        asset_match = re.match(r"^atlas-asset://([^#]+)(#.*)?$", value)
        if knowledge_match:
            target_id, anchor = knowledge_match.groups()
            target = article_paths.get(target_id)
            if target:
                tag[attribute] = f"{target}{anchor or ''}"
            else:
                issues.append(f"unresolved knowledge link {target_id}")
        elif asset_match:
            asset_id, anchor = asset_match.groups()
            target = asset_paths.get(asset_id)
            if target:
                tag[attribute] = f"../assets/{target}{anchor or ''}"
            else:
                issues.append(f"unresolved asset link {asset_id}")
    source_url = article["source"].get("url", "")
    source = (
        f'<p class="atlas-source"><a href="{source_url}">Original Confluence page</a></p>'
        if source_url
        else ""
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="atlas-id" content="{article['id']}">
  <title>{_escape(article['title'])}</title>
  <style>
    body {{ font-family: Arial, sans-serif; line-height: 1.55; max-width: 1000px; margin: 0 auto; padding: 32px; color: #202124; }}
    img {{ max-width: 100%; height: auto; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d7dce1; padding: 8px; vertical-align: top; }}
    pre {{ overflow-x: auto; padding: 12px; background: #f5f7f9; }}
    .atlas-source {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #d7dce1; font-size: 0.9em; }}
  </style>
</head>
<body>
  <h1>{_escape(article['title'])}</h1>
  {soup}
  {source}
</body>
</html>
"""
    return document, issues


def _asset_filename(asset: dict[str, Any]) -> str:
    original = safe_filename(asset["filename"], fallback=asset["id"])
    suffix = "".join(Path(original).suffixes)
    return f"{asset['id']}{suffix}"


def _load_json_objects(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def _atomic_copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Canonical asset binary not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _escape(value: str) -> str:
    import html

    return html.escape(value, quote=True)


def _instructions(manifest: dict[str, Any]) -> str:
    return f"""# Zoho HTML import bundle

This bundle was generated from schema-valid Atlas canonical objects.

- Articles: {manifest['article_count']} (`articles/`)
- Assets: {manifest['asset_count']} (`assets/`)
- Publisher issues: {len(manifest['issues'])}

Keep `articles/` and `assets/` together so relative links remain valid. `manifest.json` records every exported file and checksum. The bundle is derived output; canonical knowledge remains under `knowledge_base/confluence`.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-dir")
    parser.add_argument("--staging-dir")
    parser.add_argument("--output-dir")
    arguments = parser.parse_args()
    manifest = publish(
        canonical_dir=arguments.canonical_dir,
        staging_dir=arguments.staging_dir,
        output_dir=arguments.output_dir,
    )
    print(
        f"Published {manifest['article_count']} Zoho HTML articles and "
        f"{manifest['asset_count']} assets; issues={len(manifest['issues'])}"
    )
    return 0 if not manifest["issues"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
