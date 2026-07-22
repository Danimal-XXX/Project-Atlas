"""Authenticated, retrying client for the Confluence Cloud REST API v2."""

from __future__ import annotations

import os
import random
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv


class ConfluenceConfigurationError(RuntimeError):
    """Raised when required Confluence configuration is unavailable."""


class ConfluenceAPIError(RuntimeError):
    """Raised when a Confluence request fails after retries."""


@dataclass(frozen=True)
class ConfluenceConfig:
    base_url: str
    email: str
    api_token: str
    root_page_id: str | None = None
    space_id: str | None = None
    timeout: float = 30.0
    max_retries: int = 4

    @classmethod
    def from_env(
        cls,
        *,
        root_page_id: str | None = None,
        space_id: str | None = None,
    ) -> "ConfluenceConfig":
        load_dotenv()
        values = {
            "ATLASSIAN_BASE_URL": os.getenv("ATLASSIAN_BASE_URL"),
            "ATLASSIAN_EMAIL": os.getenv("ATLASSIAN_EMAIL"),
            "ATLASSIAN_API_TOKEN": os.getenv("ATLASSIAN_API_TOKEN"),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfluenceConfigurationError(
                "Missing Confluence environment values: " + ", ".join(missing)
            )
        return cls(
            base_url=str(values["ATLASSIAN_BASE_URL"]).rstrip("/"),
            email=str(values["ATLASSIAN_EMAIL"]),
            api_token=str(values["ATLASSIAN_API_TOKEN"]),
            root_page_id=root_page_id or os.getenv("CONFLUENCE_HOMEPAGE_ID"),
            space_id=space_id or os.getenv("CONFLUENCE_SPACE_ID"),
        )


class ConfluenceClient:
    """Small Confluence API boundary with pagination and bounded retries."""

    def __init__(
        self,
        config: ConfluenceConfig,
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.auth = (config.email, config.api_token)
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "Project-Atlas/0.3"}
        )
        self._sleep = sleeper

    def get_page(self, page_id: str, *, body_format: str | None = None) -> dict[str, Any]:
        params = {"body-format": body_format} if body_format else None
        return self._json("GET", f"/wiki/api/v2/pages/{page_id}", params=params)

    def get_space(self, space_id: str) -> dict[str, Any]:
        return self._json("GET", f"/wiki/api/v2/spaces/{space_id}")

    def iter_pages(self, space_id: str, *, limit: int = 250) -> Iterator[dict[str, Any]]:
        yield from self._paginate(
            f"/wiki/api/v2/spaces/{space_id}/pages",
            params={"depth": "all", "limit": limit},
        )

    def find_pages_by_title(self, title: str, *, limit: int = 25) -> Iterator[dict[str, Any]]:
        """Find accessible pages across spaces using an exact title filter."""
        yield from self._paginate(
            "/wiki/api/v2/pages",
            params={"title": title, "limit": limit},
        )

    def iter_attachments(
        self, page_id: str, *, limit: int = 250
    ) -> Iterator[dict[str, Any]]:
        yield from self._paginate(
            f"/wiki/api/v2/pages/{page_id}/attachments",
            params={"limit": limit},
        )

    def download(self, source_url: str, destination: Path) -> tuple[str, int]:
        """Download a binary atomically and return its SHA-256 and size."""
        import hashlib

        response = self._request("GET", source_url, stream=True)
        temporary = destination.with_name(
            f"{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return digest.hexdigest(), size

    def absolute_url(self, value: str) -> str:
        if value.startswith(("http://", "https://")):
            return value
        relative = value if value.startswith("/") else f"/{value}"
        if relative.startswith(("/rest/", "/download/", "/pages/", "/spaces/")):
            relative = "/wiki" + relative
        return urljoin(self.config.base_url + "/", relative.lstrip("/"))

    def _paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        url: str | None = path
        next_params = dict(params or {})
        while url:
            response = self._request("GET", url, params=next_params)
            payload = response.json()
            yield from payload.get("results", [])
            next_link = payload.get("_links", {}).get("next")
            if not next_link and response.links.get("next"):
                next_link = response.links["next"].get("url")
            url = self.absolute_url(next_link) if next_link else None
            next_params = {}

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return self._request(method, path, **kwargs).json()
        except ValueError as error:
            raise ConfluenceAPIError(f"Confluence returned invalid JSON for {path}") from error

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = self.absolute_url(path)
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.config.timeout,
                    allow_redirects=True,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.config.max_retries:
                    break
                self._sleep(self._retry_delay(attempt, None))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.config.max_retries:
                    last_error = ConfluenceAPIError(
                        f"Confluence returned HTTP {response.status_code} for {url}"
                    )
                    break
                self._sleep(self._retry_delay(attempt, response.headers.get("Retry-After")))
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                detail = response.text[:500].replace(self.config.api_token, "<redacted>")
                raise ConfluenceAPIError(
                    f"Confluence returned HTTP {response.status_code} for {url}: {detail}"
                ) from error
            return response
        raise ConfluenceAPIError(f"Confluence request failed for {url}: {last_error}") from last_error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 120.0))
            except ValueError:
                pass
        return min(2**attempt + random.random(), 30.0)
