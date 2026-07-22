"""End-to-end, schema-enforced Confluence crawl command."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atlas.plugin_manager import PluginManager
from atlas.schema_validator import AtlasSchemaValidator
from sources.confluence.package import package_assets
from sources.confluence.links import resolve_link_targets
from sources.confluence.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def crawl(
    *,
    root_page_id: str | None = None,
    max_pages: int | None = None,
    include_attachments: bool = True,
    resume: bool = True,
    dry_run: bool = False,
    staging_dir: str | Path | None = None,
    inventory_dir: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run discovery through validated canonical persistence."""
    manager = PluginManager(PROJECT_ROOT)
    staging = Path(staging_dir or PROJECT_ROOT / "staging" / "confluence")
    inventory_output = Path(inventory_dir or PROJECT_ROOT / "inventory" / "confluence")
    canonical = Path(knowledge_dir or PROJECT_ROOT / "knowledge_base" / "confluence")
    discovered = manager.call(
        "confluence", "discover", root_page_id=root_page_id, max_pages=max_pages
    )
    mirrored = manager.call(
        "confluence",
        "mirror",
        discovered,
        staging_dir=staging,
        include_attachments=include_attachments,
        resume=resume,
        dry_run=dry_run,
        max_pages=max_pages,
    )
    if dry_run:
        return {"discovery": discovered, "mirror": mirrored, "dry_run": True}
    link_resolution = resolve_link_targets(staging_dir=staging)
    inventoried = manager.call(
        "confluence",
        "inventory",
        mirrored,
        staging_dir=staging,
        output_dir=inventory_output,
        page_ids=[str(page["id"]) for page in discovered["pages"]],
    )
    selected_page_ids = [str(page["id"]) for page in discovered["pages"]]
    objects = manager.call(
        "confluence", "package", staging_dir=staging, page_ids=selected_page_ids
    )
    validator = AtlasSchemaValidator(PROJECT_ROOT / "schemas")
    validator.validate_object(inventoried, "inventory.schema.json")
    assets = validator.validate_collection(
        package_assets(staging_dir=staging, page_ids=selected_page_ids),
        "asset.schema.json",
    )
    for item in objects:
        atomic_write_json(canonical / "articles" / f"{item['id']}.json", item)
    for asset in assets:
        atomic_write_json(canonical / "assets" / f"{asset['id']}.json", asset)
    manifest = {
        "schema_version": "1.0",
        "connector": "confluence",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root_page_id": discovered["root_page_id"],
        "space_id": discovered["space_id"],
        "object_count": len(objects),
        "asset_count": len(assets),
        "objects": [item["id"] for item in objects],
        "assets": [asset["id"] for asset in assets],
        "crawl_successful": bool(mirrored["successful"]),
        "failures": mirrored["failures"],
    }
    validator.validate_object(manifest, "manifest.schema.json")
    atomic_write_json(canonical / "manifest.json", manifest)
    return {
        "discovery": discovered,
        "mirror": mirrored,
        "inventory": inventoried,
        "link_resolution": link_resolution,
        "manifest": manifest,
        "dry_run": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-page-id")
    parser.add_argument("--limit", type=int, dest="max_pages")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-attachments", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--staging-dir")
    parser.add_argument("--inventory-dir")
    parser.add_argument("--knowledge-dir")
    arguments = parser.parse_args()
    result = crawl(
        root_page_id=arguments.root_page_id,
        max_pages=arguments.max_pages,
        include_attachments=not arguments.no_attachments,
        resume=not arguments.no_resume,
        dry_run=arguments.dry_run,
        staging_dir=arguments.staging_dir,
        inventory_dir=arguments.inventory_dir,
        knowledge_dir=arguments.knowledge_dir,
    )
    manifest = result.get("manifest")
    if manifest:
        print(
            f"Crawled {manifest['object_count']} pages and {manifest['asset_count']} assets; "
            f"success={manifest['crawl_successful']}"
        )
        return 0 if manifest["crawl_successful"] else 1
    print(f"Dry run planned {result['discovery']['page_count']} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
