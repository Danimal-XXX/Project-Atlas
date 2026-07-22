from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from publishers.zoho.publish import publish


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ZohoPublisherTests(unittest.TestCase):
    def test_canonical_objects_publish_with_rewritten_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical = root / "canonical"
            staging = root / "staging"
            output = root / "output"
            (canonical / "articles").mkdir(parents=True)
            (canonical / "assets").mkdir()
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
                    "external_id": "a1",
                    "container_external_id": "1",
                    "url": "https://example.com/a1",
                },
                "status": "available",
            }
            article = {
                "schema_version": "1.0",
                "id": "article-1",
                "title": "Article One",
                "slug": "article-one",
                "status": "published",
                "content": {
                    "format": "markdown",
                    "body": "![Image](atlas-asset://asset-1) [Self](atlas-knowledge://article-1)\n\n    ![Indented](atlas-asset://asset-1)",
                },
                "source": {"connector": "confluence", "external_id": "1"},
                "timestamps": {"ingested_at": "2026-07-22T00:00:00+00:00"},
                "assets": ["asset-1"],
                "extensions": {},
            }
            (canonical / "assets/asset-1.json").write_text(json.dumps(asset))
            (canonical / "articles/article-1.json").write_text(json.dumps(article))
            manifest = publish(canonical_dir=canonical, staging_dir=staging, output_dir=output)
            self.assertEqual(manifest["article_count"], 1)
            self.assertEqual(manifest["asset_count"], 1)
            self.assertEqual(manifest["issues"], [])
            html = next((output / "articles").glob("*.html")).read_text()
            self.assertIn("../assets/asset-1.png", html)
            self.assertIn("article-one--article-1.html", html)
            self.assertNotIn("atlas-asset://", html)
            self.assertTrue((output / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
