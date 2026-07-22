"""Authenticated client for the Zoho Desk knowledge-base API."""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv


class ZohoConfigurationError(RuntimeError):
    """Raised when required Zoho configuration is unavailable."""


class ZohoAPIError(RuntimeError):
    """Raised when a Zoho Desk request cannot be completed."""


@dataclass(frozen=True)
class ZohoConfig:
    """Configuration required for a resumable Zoho Desk article import."""

    org_id: str
    category_id: str
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    access_token: str | None = None
    accounts_url: str = "https://accounts.zoho.com"
    api_domain: str | None = None
    locale: str = "en"
    permission: str = "ALL"
    status: str = "Draft"
    timeout: float = 45.0
    max_retries: int = 4

    @classmethod
    def from_env(
        cls,
        *,
        category_id: str | None = None,
        accounts_url: str | None = None,
    ) -> "ZohoConfig":
        load_dotenv()
        org_id = os.getenv("ZOHO_ORG_ID")
        category_id = category_id or os.getenv("ZOHO_CATEGORY_ID")
        access_token = os.getenv("ZOHO_ACCESS_TOKEN")
        oauth = {
            "ZOHO_CLIENT_ID": os.getenv("ZOHO_CLIENT_ID"),
            "ZOHO_CLIENT_SECRET": os.getenv("ZOHO_CLIENT_SECRET"),
            "ZOHO_REFRESH_TOKEN": os.getenv("ZOHO_REFRESH_TOKEN"),
        }
        missing = [
            name
            for name, value in {
                "ZOHO_ORG_ID": org_id,
                "ZOHO_CATEGORY_ID": category_id,
            }.items()
            if not value
        ]
        if not access_token:
            missing.extend(name for name, value in oauth.items() if not value)
        if missing:
            raise ZohoConfigurationError(
                "Missing Zoho environment values: " + ", ".join(missing)
            )
        permission = os.getenv("ZOHO_ARTICLE_PERMISSION", "ALL").upper()
        status = os.getenv("ZOHO_ARTICLE_STATUS", "Draft").title()
        if permission not in {"ALL", "REGISTEREDUSERS", "AGENTS"}:
            raise ZohoConfigurationError(
                "ZOHO_ARTICLE_PERMISSION must be ALL, REGISTEREDUSERS, or AGENTS"
            )
        if status not in {"Draft", "Published", "Review"}:
            raise ZohoConfigurationError(
                "ZOHO_ARTICLE_STATUS must be Draft, Published, or Review"
            )
        return cls(
            org_id=str(org_id),
            category_id=str(category_id),
            client_id=oauth["ZOHO_CLIENT_ID"],
            client_secret=oauth["ZOHO_CLIENT_SECRET"],
            refresh_token=oauth["ZOHO_REFRESH_TOKEN"],
            access_token=access_token,
            accounts_url=(
                accounts_url
                or os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com")
            ).rstrip("/"),
            api_domain=(os.getenv("ZOHO_DESK_API_DOMAIN") or None),
            locale=os.getenv("ZOHO_ARTICLE_LOCALE", "en"),
            permission=permission,
            status=status,
        )


class ZohoDeskClient:
    """Small Zoho Desk API boundary with OAuth refresh and bounded retries."""

    def __init__(
        self,
        config: ZohoConfig,
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "Project-Atlas/0.3"}
        )
        self._sleep = sleeper
        self._access_token = config.access_token
        self._api_domain = config.api_domain.rstrip("/") if config.api_domain else None

    def authenticate(self) -> None:
        """Obtain an access token and the account's regional API domain."""
        if self._access_token and self._api_domain:
            return
        if self._access_token and not self._api_domain:
            raise ZohoConfigurationError(
                "ZOHO_DESK_API_DOMAIN is required when ZOHO_ACCESS_TOKEN is used"
            )
        if not all(
            (self.config.client_id, self.config.client_secret, self.config.refresh_token)
        ):
            raise ZohoConfigurationError(
                "Zoho OAuth requires client ID, client secret, and refresh token"
            )
        response = self.session.post(
            f"{self.config.accounts_url}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self.config.refresh_token,
            },
            timeout=self.config.timeout,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise ZohoAPIError("Zoho OAuth returned invalid JSON") from error
        if response.status_code >= 400 or not payload.get("access_token"):
            detail = str(payload.get("error") or response.text[:300])
            raise ZohoAPIError(f"Zoho OAuth refresh failed: {detail}")
        self._access_token = str(payload["access_token"])
        # Zoho's generic OAuth response may return www.zohoapis.<region>, but
        # Zoho Desk uses its own desk.zoho.<region> API host. An explicitly
        # configured Desk domain therefore takes precedence.
        api_domain = self.config.api_domain or payload.get("api_domain")
        if not api_domain:
            raise ZohoAPIError("Zoho OAuth response did not include api_domain")
        self._api_domain = str(api_domain).rstrip("/")

    def iter_articles(self, category_id: str) -> Iterator[dict[str, Any]]:
        offset = 1
        while True:
            payload = self._json(
                "GET",
                "/api/v1/articles",
                params={"categoryId": category_id, "from": offset, "limit": 50},
            )
            records = payload.get("data", []) if isinstance(payload, dict) else []
            if not isinstance(records, list):
                raise ZohoAPIError("Zoho article list response did not contain a data list")
            yield from records
            if len(records) < 50:
                return
            offset += len(records)

    def get_article(self, article_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/articles/{article_id}")

    def get_category_tree(self, root_category_id: str) -> dict[str, Any]:
        return self._json(
            "GET",
            f"/api/v1/kbRootCategories/{root_category_id}/categoryTree",
        )

    def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/api/v1/articles", json=payload)

    def update_article(self, article_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json("PATCH", f"/api/v1/articles/{article_id}", json=payload)

    def upload_attachment(
        self,
        article_id: str,
        locale: str,
        source: Path,
    ) -> dict[str, Any]:
        with source.open("rb") as stream:
            return self._json(
                "POST",
                f"/api/v1/articles/{article_id}/translations/{locale}/attachments",
                params={"attachmentType": "file"},
                files={"file": (source.name, stream)},
            )

    def iter_attachments(
        self, article_id: str, locale: str
    ) -> Iterator[dict[str, Any]]:
        offset = 1
        # Zoho currently caps article-translation attachment pages at ten,
        # even when a larger limit is requested.
        page_size = 10
        while True:
            payload = self._json(
                "GET",
                f"/api/v1/articles/{article_id}/translations/{locale}/attachments",
                params={"from": offset, "limit": page_size},
            )
            records = payload.get("data", [])
            if not isinstance(records, list):
                raise ZohoAPIError("Zoho attachment response did not contain a data list")
            yield from records
            if len(records) < page_size:
                return
            offset += len(records)

    def delete_attachment(
        self, article_id: str, locale: str, attachment_id: str
    ) -> None:
        """Dissociate one Atlas attachment from an article translation."""
        self._json(
            "POST",
            f"/api/v1/articles/{article_id}/translations/{locale}/dissociateAttachments",
            json={"attachmentIds": [attachment_id]},
        )

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError as error:
            raise ZohoAPIError(f"Zoho returned invalid JSON for {path}") from error
        if not isinstance(payload, dict):
            raise ZohoAPIError(f"Zoho returned a non-object response for {path}")
        return payload

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        self.authenticate()
        assert self._access_token and self._api_domain
        url = f"{self._api_domain}{path}"
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            headers = dict(kwargs.pop("headers", {}))
            headers.update(
                {
                    "Authorization": f"Zoho-oauthtoken {self._access_token}",
                    "orgId": self.config.org_id,
                }
            )
            for file_value in (kwargs.get("files") or {}).values():
                candidate = file_value[-1] if isinstance(file_value, tuple) else file_value
                if hasattr(candidate, "seek"):
                    candidate.seek(0)
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.config.timeout,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.config.max_retries:
                    break
                self._sleep(self._retry_delay(attempt, None))
                continue
            if response.status_code == 401 and self.config.refresh_token and attempt == 0:
                self._access_token = None
                self._api_domain = None
                self.authenticate()
                continue
            if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                if attempt == self.config.max_retries:
                    last_error = ZohoAPIError(
                        f"Zoho returned HTTP {response.status_code} for {path}"
                    )
                    break
                self._sleep(self._retry_delay(attempt, response.headers.get("Retry-After")))
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                detail = response.text[:800]
                for secret in (
                    self._access_token,
                    self.config.client_secret,
                    self.config.refresh_token,
                ):
                    if secret:
                        detail = detail.replace(secret, "<redacted>")
                raise ZohoAPIError(
                    f"Zoho returned HTTP {response.status_code} for {path}: {detail}"
                ) from error
            return response
        raise ZohoAPIError(f"Zoho request failed for {path}: {last_error}") from last_error

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 120.0))
            except ValueError:
                pass
        return min(2**attempt + random.random(), 30.0)
