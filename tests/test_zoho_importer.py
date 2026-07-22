from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from publishers.zoho.client import ZohoConfig
from publishers.zoho.importer import import_bundle
from publishers.zoho.publish import publish


class FakeZohoClient:
    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.articles = {str(item["id"]): dict(item) for item in existing or []}
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.uploaded: list[Path] = []
        self.deleted: list[str] = []
        self.attachments: dict[str, list[dict[str, Any]]] = {}

    def iter_articles(self, category_id: str):
        yield from self.articles.values()

    def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        article_id = str(1000 + len(self.articles))
        record = {
            **payload,
            "id": article_id,
            "portalUrl": f"https://example.zohodesk.com/kb/articles/{payload['permalink']}",
        }
        self.articles[article_id] = record
        self.created.append(dict(payload))
        return dict(record)

    def update_article(self, article_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.articles[article_id].update(payload)
        self.updated.append(dict(payload))
        return dict(self.articles[article_id])

    def get_article(self, article_id: str) -> dict[str, Any]:
        return dict(self.articles[article_id])

    def upload_attachment(
        self, article_id: str, locale: str, source: Path
    ) -> dict[str, Any]:
        self.uploaded.append(source)
        record = {
            "resourceId": f"resource-{len(self.uploaded)}",
            "name": source.name,
            "url": f"https://desk.zoho.eu/gallery/{source.name}",
        }
        self.attachments.setdefault(article_id, []).append(record)
        return record

    def iter_attachments(self, article_id: str, locale: str):
        yield from self.attachments.get(article_id, [])

    def delete_attachment(
        self, article_id: str, locale: str, attachment_id: str
    ) -> None:
        self.deleted.append(attachment_id)
        self.attachments[article_id] = [
            item
            for item in self.attachments.get(article_id, [])
            if str(item.get("resourceId") or item.get("id")) != attachment_id
        ]


def _article(*, with_asset: bool = False) -> dict[str, Any]:
    body = "Hello from Atlas. [External HTML](https://example.com/item.html)"
    if with_asset:
        body += "\n\n![Screenshot](atlas-asset://asset-1)"
    return {
        "schema_version": "1.0",
        "id": "article-1",
        "title": "Article One",
        "slug": "article-one",
        "status": "published",
        "content": {"format": "markdown", "body": body, "language": "en"},
        "source": {
            "connector": "confluence",
            "external_id": "1",
            "url": "https://example.atlassian.net/wiki/article-one",
        },
        "timestamps": {"ingested_at": "2026-07-22T00:00:00+00:00"},
        "taxonomy": {"tags": ["atlas"], "products": [], "audiences": []},
        "assets": ["asset-1"] if with_asset else [],
        "relationships": [],
        "extensions": {},
    }


def _make_bundle(root: Path, *, with_asset: bool = False) -> tuple[Path, Path]:
    canonical = root / "canonical"
    staging = root / "staging"
    output = root / "bundle"
    (canonical / "articles").mkdir(parents=True)
    (canonical / "assets").mkdir()
    (canonical / "articles/article-1.json").write_text(
        json.dumps(_article(with_asset=with_asset)), encoding="utf-8"
    )
    if with_asset:
        binary = staging / "pages/1/attachments/image.png"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"png")
        digest = hashlib.sha256(b"png").hexdigest()
        asset = {
            "schema_version": "1.0",
            "id": "asset-1",
            "filename": "image.png",
            "media_type": "image/png",
            "size_bytes": 3,
            "sha256": digest,
            "local_path": "pages/1/attachments/image.png",
            "source": {
                "connector": "confluence",
                "external_id": "attachment-1",
                "container_external_id": "1",
                "url": "https://example.atlassian.net/image.png",
            },
            "status": "available",
        }
        (canonical / "assets/asset-1.json").write_text(json.dumps(asset), encoding="utf-8")
    publish(canonical_dir=canonical, staging_dir=staging, output_dir=output)
    return canonical, output


class ZohoImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ZohoConfig(
            org_id="org-1",
            category_id="233447000001584019",
            access_token="token",
            api_domain="https://desk.zoho.eu",
            status="Draft",
        )

    def test_dry_run_validates_bundle_without_contacting_zoho(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical, bundle = _make_bundle(root)
            report = import_bundle(
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=root / "state.json",
            )
            self.assertEqual(report["mode"], "dry-run")
            self.assertEqual(report["article_count"], 1)
            self.assertFalse(report["remote_contacted"])
            self.assertFalse((root / "state.json").exists())

    def test_create_verify_and_rerun_skip_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical, bundle = _make_bundle(root)
            state_path = root / "state.json"
            client = FakeZohoClient()
            first = import_bundle(
                apply=True,
                config=self.config,
                client=client,
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=state_path,
            )
            self.assertEqual(first["created"], 1)
            self.assertEqual(first["updated"], 0)
            self.assertEqual(len(client.created), 1)
            self.assertEqual(len(client.updated), 1)

            self.assertTrue(state_path.is_file())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["articles"]["article-1"]["pending"])

            second = import_bundle(
                apply=True,
                config=self.config,
                client=client,
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=state_path,
            )
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(len(client.created), 1)
            self.assertEqual(len(client.updated), 1)

            forced = import_bundle(
                apply=True,
                config=self.config,
                client=client,
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=state_path,
                force=True,
            )
            self.assertEqual(forced["updated"], 1)
            self.assertEqual(len(client.updated), 2)

    def test_existing_manual_article_is_adopted_by_exact_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical, bundle = _make_bundle(root)
            client = FakeZohoClient(
                [
                    {
                        "id": "55",
                        "title": "Article One",
                        "permalink": "old-permalink",
                        "categoryId": self.config.category_id,
                        "status": "Draft",
                        "answer": "Old content",
                        "portalUrl": "https://example.zohodesk.com/kb/articles/old-permalink",
                    }
                ]
            )
            report = import_bundle(
                apply=True,
                config=self.config,
                client=client,
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=root / "state.json",
            )
            self.assertEqual(report["created"], 0)
            self.assertEqual(report["updated"], 1)
            self.assertEqual(report["actions"][0]["zoho_id"], "55")

    def test_asset_is_uploaded_and_local_url_is_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical, bundle = _make_bundle(root, with_asset=True)
            client = FakeZohoClient()
            import_bundle(
                apply=True,
                config=self.config,
                client=client,
                bundle_dir=bundle,
                canonical_dir=canonical,
                state_path=root / "state.json",
            )
            self.assertEqual(len(client.uploaded), 1)
            answer = client.updated[-1]["answer"]
            self.assertIn("https://desk.zoho.eu/gallery/asset-1.png", answer)
            self.assertIn("https://example.com/item.html", answer)
            self.assertNotIn("../assets/", answer)


if __name__ == "__main__":
    unittest.main()
