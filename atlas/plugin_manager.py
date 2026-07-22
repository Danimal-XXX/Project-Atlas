"""Dynamic discovery and execution of Atlas source connectors."""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from atlas.schema_validator import AtlasSchemaValidator


LIFECYCLE_METHODS = ("discover", "mirror", "inventory", "package")


class PluginError(RuntimeError):
    """Base exception for connector discovery and execution failures."""


class ConnectorConfigurationError(PluginError):
    """Raised when a connector manifest is incomplete or inconsistent."""


class UnsupportedLifecycleMethod(PluginError):
    """Raised when a connector does not declare a requested lifecycle stage."""


@dataclass(frozen=True)
class SourceConnector:
    """Validated source connector metadata."""

    name: str
    version: str
    root: Path
    manifest: Mapping[str, Any]
    supports: tuple[str, ...]
    modules: Mapping[str, str]
    package_schema: str | None

    @property
    def key(self) -> str:
        return self.root.name


class PluginManager:
    """Discover source plugins and invoke their declared lifecycle methods."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        validator: AtlasSchemaValidator | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.sources_directory = self.project_root / "sources"
        self.validator = validator or AtlasSchemaValidator(self.project_root / "schemas")
        self._connectors: dict[str, SourceConnector] | None = None
        self._modules: dict[tuple[str, str], ModuleType] = {}

    def discover_sources(self, *, refresh: bool = False) -> dict[str, SourceConnector]:
        """Discover and validate every ``sources/*/connector.yaml`` manifest."""
        if self._connectors is not None and not refresh:
            return dict(self._connectors)
        connectors: dict[str, SourceConnector] = {}
        for manifest_path in sorted(self.sources_directory.glob("*/connector.yaml")):
            connector = self._read_connector(manifest_path)
            if connector.key in connectors:
                raise ConnectorConfigurationError(f"Duplicate connector key: {connector.key}")
            connectors[connector.key] = connector
        self._connectors = connectors
        return dict(connectors)

    def get_connector(self, connector_name: str) -> SourceConnector:
        try:
            return self.discover_sources()[connector_name]
        except KeyError as error:
            available = ", ".join(self.discover_sources()) or "none"
            raise PluginError(
                f"Unknown source connector {connector_name!r}; available: {available}"
            ) from error

    def call(self, connector_name: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a declared lifecycle method and validate package output."""
        connector = self.get_connector(connector_name)
        if method not in LIFECYCLE_METHODS:
            raise UnsupportedLifecycleMethod(f"Unknown source lifecycle method: {method}")
        if method not in connector.supports:
            raise UnsupportedLifecycleMethod(
                f"Connector {connector_name!r} does not support {method!r}; "
                f"declared methods: {', '.join(connector.supports) or 'none'}"
            )
        module = self._load_module(connector, method)
        function = getattr(module, method, None)
        if not callable(function):
            raise ConnectorConfigurationError(
                f"Connector {connector_name!r} declares {method!r}, but "
                f"{connector.modules[method]!r} has no callable {method}()"
            )
        result = function(*args, **kwargs)
        if method == "package":
            return self._validate_package_output(result, connector)
        return result

    def _validate_package_output(self, result: Any, connector: SourceConnector) -> Any:
        schema_name = connector.package_schema
        if not schema_name:
            raise ConnectorConfigurationError(
                f"Connector {connector.key!r} supports package but declares no output schema"
            )
        if isinstance(result, Mapping):
            return self.validator.validate_object(result, schema_name)
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
            return self.validator.validate_collection(result, schema_name)
        raise ConnectorConfigurationError(
            f"Connector {connector.key!r} package() must return an object or iterable of objects"
        )

    def _read_connector(self, manifest_path: Path) -> SourceConnector:
        with manifest_path.open(encoding="utf-8") as manifest_file:
            manifest = yaml.safe_load(manifest_file) or {}
        required = ("name", "version", "type", "supports")
        missing = [field for field in required if field not in manifest]
        if missing:
            raise ConnectorConfigurationError(
                f"{manifest_path}: missing required fields: {', '.join(missing)}"
            )
        if manifest["type"] != "source":
            raise ConnectorConfigurationError(
                f"{manifest_path}: type must be 'source', got {manifest['type']!r}"
            )
        supports = tuple(manifest["supports"] or ())
        invalid = sorted(set(supports) - set(LIFECYCLE_METHODS))
        if invalid:
            raise ConnectorConfigurationError(
                f"{manifest_path}: unsupported lifecycle methods: {', '.join(invalid)}"
            )
        declared_modules = manifest.get("modules") or {}
        entrypoint = manifest.get("entrypoint")
        modules = {
            method: declared_modules.get(method) or entrypoint or f"{method}.py"
            for method in supports
        }
        for method, relative_path in modules.items():
            module_path = (manifest_path.parent / relative_path).resolve()
            if manifest_path.parent.resolve() not in module_path.parents or not module_path.is_file():
                raise ConnectorConfigurationError(
                    f"{manifest_path}: module for {method!r} not found: {relative_path}"
                )
        package_schema = manifest.get("outputs", {}).get("package", {}).get("schema")
        if "package" in supports and not package_schema:
            raise ConnectorConfigurationError(
                f"{manifest_path}: package lifecycle requires outputs.package.schema"
            )
        return SourceConnector(
            name=str(manifest["name"]),
            version=str(manifest["version"]),
            root=manifest_path.parent.resolve(),
            manifest=manifest,
            supports=supports,
            modules=modules,
            package_schema=package_schema,
        )

    def _load_module(self, connector: SourceConnector, method: str) -> ModuleType:
        cache_key = (connector.key, method)
        if cache_key in self._modules:
            return self._modules[cache_key]
        module_path = connector.root / connector.modules[method]
        module_name = f"atlas_source_{connector.key}_{method}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ConnectorConfigurationError(f"Unable to load connector module: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._modules[cache_key] = module
        return module

    def describe_method(self, connector_name: str, method: str) -> inspect.Signature:
        """Return the signature of a supported lifecycle method."""
        connector = self.get_connector(connector_name)
        if method not in connector.supports:
            raise UnsupportedLifecycleMethod(
                f"Connector {connector_name!r} does not support {method!r}"
            )
        function = getattr(self._load_module(connector, method), method, None)
        if not callable(function):
            raise ConnectorConfigurationError(f"Missing callable {method}()")
        return inspect.signature(function)
