from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from atlas.plugin_manager import PluginManager, UnsupportedLifecycleMethod
from atlas.publisher import validate_publisher_input
from atlas.schema_validator import AtlasSchemaValidator, AtlasValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def valid_knowledge() -> dict:
    return {
        "schema_version": "1.0",
        "id": "test-1",
        "title": "Test article",
        "slug": "test-article",
        "status": "published",
        "content": {"format": "markdown", "body": "Body"},
        "source": {"connector": "test", "external_id": "1"},
        "timestamps": {"ingested_at": "2026-07-22T00:00:00+00:00"},
        "extensions": {"test": {"original_type": "article"}},
    }


class SchemaValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = AtlasSchemaValidator(PROJECT_ROOT / "schemas")

    def test_valid_knowledge_object_passes(self) -> None:
        item = valid_knowledge()
        self.assertIs(self.validator.validate_object(item, "knowledge.schema.json"), item)

    def test_missing_required_field_fails(self) -> None:
        item = valid_knowledge()
        del item["title"]
        with self.assertRaisesRegex(AtlasValidationError, "title") as raised:
            self.validator.validate_object(item, "knowledge.schema.json")
        self.assertEqual(raised.exception.object_id, "test-1")

    def test_invalid_slug_fails(self) -> None:
        item = valid_knowledge()
        item["slug"] = "Not A Slug"
        with self.assertRaises(AtlasValidationError) as raised:
            self.validator.validate_object(item, "knowledge.schema.json")
        self.assertEqual(raised.exception.json_path, "$.slug")
        self.assertTrue(raised.exception.schema_path.startswith("$.properties.slug"))


class PluginManagerTests(unittest.TestCase):
    def test_dynamically_discovered_connector_loads(self) -> None:
        manager = PluginManager(PROJECT_ROOT)
        connector = manager.get_connector("confluence")
        self.assertEqual(connector.name, "Confluence")
        self.assertEqual(connector.package_schema, "knowledge.schema.json")

    def test_unsupported_lifecycle_method_fails_clearly(self) -> None:
        manager = PluginManager(PROJECT_ROOT)
        with self.assertRaisesRegex(UnsupportedLifecycleMethod, "does not support 'mirror'"):
            manager.call("helpscout", "mirror")

    def test_packaged_connector_output_is_validated_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "sources" / "fixture").mkdir(parents=True)
            (root / "schemas").mkdir()
            schema = json.loads((PROJECT_ROOT / "schemas" / "knowledge.schema.json").read_text())
            (root / "schemas" / "knowledge.schema.json").write_text(json.dumps(schema))
            (root / "sources" / "fixture" / "connector.yaml").write_text(
                "name: Fixture\nversion: '1.0'\ntype: source\n"
                "supports: [package]\nmodules:\n  package: package.py\n"
                "outputs:\n  package:\n    schema: knowledge.schema.json\n"
            )
            invalid = deepcopy(valid_knowledge())
            invalid["slug"] = "invalid slug"
            (root / "sources" / "fixture" / "package.py").write_text(
                f"def package():\n    return {invalid!r}\n"
            )
            manager = PluginManager(root)
            with self.assertRaises(AtlasValidationError):
                manager.call("fixture", "package")

    def test_confluence_package_returns_valid_objects(self) -> None:
        manager = PluginManager(PROJECT_ROOT)
        items = manager.call(
            "confluence",
            "package",
            ingested_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
        self.assertGreater(len(items), 0)
        self.assertEqual(items[0]["source"]["connector"], "confluence")


class PublisherBoundaryTests(unittest.TestCase):
    def test_publisher_input_is_schema_validated(self) -> None:
        item = valid_knowledge()
        validated = validate_publisher_input(item)
        self.assertEqual(validated, [item])

        del item["source"]
        with self.assertRaises(AtlasValidationError):
            validate_publisher_input(item)


if __name__ == "__main__":
    unittest.main()
