from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from atlas.schema_validator import AtlasSchemaValidator
from sources.confluence.discover import discover
from sources.confluence.inventory import inventory
from sources.confluence.mirror import mirror
from sources.confluence.package import package, package_assets
from sources.confluence.transform import storage_to_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeConfluenceClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            root_page_id="root", space_id=None, base_url="https://example.atlassian.net"
        )
        self.page_fetches = 0

    def get_page(self, page_id: str, *, body_format: str | None = None) -> dict:
        if body_format is None:
            return self._page(page_id)
        self.page_fetches += 1
        page = self._page(page_id)
        page["body"] = {
            "storage": {
                "representation": "storage",
                "value": (
                    "<h1>Guide</h1><p>Hello</p>"
                    '<ac:image><ri:attachment ri:filename="diagram.png"></ri:attachment></ac:image>'
                ),
            }
        }
        return page

    def iter_pages(self, space_id: str):
        yield self._page("root")
        yield self._page("child", parent="root", title="Child")
        yield self._page("grandchild", parent="child", title="Grandchild")
        yield self._page("unrelated", parent=None, title="Unrelated")

    def iter_attachments(self, page_id: str):
        yield {
            "id": f"asset-{page_id}",
            "pageId": page_id,
            "title": "diagram.png",
            "mediaType": "image/png",
            "fileSize": 3,
            "downloadLink": f"/download/{page_id}",
            "version": {"number": 1},
        }

    def absolute_url(self, value: str) -> str:
        prefix = "/wiki" if value.startswith("/rest/") else ""
        return f"https://example.atlassian.net{prefix}{value}"

    def download(self, source_url: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"png")
        return "469568e448007e1e3a3cf64ede91e6072b6b61212d09c15d8a25c5e5a0227e12", 3

    @staticmethod
    def _page(page_id: str, parent: str | None = None, title: str = "Root") -> dict:
        return {
            "id": page_id,
            "title": title,
            "parentId": parent,
            "spaceId": "space",
            "status": "current",
            "createdAt": "2026-01-01T00:00:00Z",
            "authorId": "author",
            "position": 1,
            "version": {"number": 1, "createdAt": "2026-01-02T00:00:00Z"},
            "_links": {
                "base": "https://example.atlassian.net",
                "webui": f"/wiki/spaces/KB/pages/{page_id}",
            },
        }


class ConfluencePipelineTests(unittest.TestCase):
    def test_discovery_is_scoped_to_root_tree(self) -> None:
        result = discover(client=FakeConfluenceClient())
        self.assertEqual([page["id"] for page in result["pages"]], ["root", "child", "grandchild"])

    def test_transform_rewrites_attachment_and_preserves_macro(self) -> None:
        result = storage_to_markdown(
            '<table><tr><td><a href="https://example.com"><ac:image><ri:attachment ri:filename="guide.png"></ri:attachment></ac:image></a></td></tr></table>'
            '<ac:link><ri:page ri:content-title="Child"></ri:page><ac:plain-text-link-body>Next</ac:plain-text-link-body></ac:link>'
            '<ac:structured-macro ac:name="unknown"><ac:plain-text-body>Current</ac:plain-text-body></ac:structured-macro>',
            [{"title": "guide.png", "asset_id": "asset-1", "download_status": "available"}],
            {"Child": "atlas-knowledge://confluence-child"},
        )
        self.assertIn("atlas-asset://asset-1", result.markdown)
        self.assertIn("atlas-knowledge://confluence-child", result.markdown)
        self.assertIn("Confluence macro: unknown", result.markdown)
        self.assertIn("Unsupported Confluence macro", result.warnings[0])

    def test_transform_rewrites_download_and_drawio_macros(self) -> None:
        result = storage_to_markdown(
            '<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name"><ri:attachment ri:filename="profile.zip"></ri:attachment></ac:parameter></ac:structured-macro>'
            '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">model.drawio</ac:parameter></ac:structured-macro>',
            [
                {"title": "profile.zip", "asset_id": "zip-1", "download_status": "available"},
                {"title": "model.drawio", "asset_id": "drawio-1", "download_status": "available"},
                {"title": "model.drawio.png", "asset_id": "preview-1", "download_status": "available"},
            ],
        )
        self.assertIn("atlas-asset://zip-1", result.markdown)
        self.assertIn("atlas-asset://drawio-1", result.markdown)
        self.assertIn("atlas-asset://preview-1", result.markdown)
        self.assertEqual(result.warnings, ())

    def test_mirror_inventory_and_package_are_repeatable(self) -> None:
        client = FakeConfluenceClient()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            staging = root / "staging"
            inventory_dir = root / "inventory"
            found = discover(client=client)
            first = mirror(found, client=client, staging_dir=staging)
            second = mirror(found, client=client, staging_dir=staging)
            self.assertEqual(first["mirrored_pages"], 3)
            self.assertEqual(second["resumed_pages"], 3)
            self.assertEqual(client.page_fetches, 3)
            inventoried = inventory(second, staging_dir=staging, output_dir=inventory_dir)
            self.assertEqual(inventoried["item_count"], 3)
            objects = list(package(staging_dir=staging))
            assets = list(package_assets(staging_dir=staging))
            validator = AtlasSchemaValidator(PROJECT_ROOT / "schemas")
            validator.validate_collection(objects, "knowledge.schema.json")
            validator.validate_collection(assets, "asset.schema.json")
            self.assertEqual(len(objects), 3)
            self.assertEqual(len(assets), 3)
            self.assertIn("atlas-asset://confluence-attachment-asset-root", objects[2]["content"]["body"])
            self.assertTrue((inventory_dir / "pages.csv").is_file())
            self.assertTrue(json.loads((staging / "crawl-report.json").read_text())["successful"])


if __name__ == "__main__":
    unittest.main()
