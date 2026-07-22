"""Discover the Confluence page tree selected for ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sources.confluence.client import ConfluenceClient, ConfluenceConfig


class ConfluenceDiscoveryError(RuntimeError):
    """Raised when a requested page tree cannot be discovered safely."""


def discover(
    *,
    root_page_id: str | None = None,
    space_id: str | None = None,
    client: ConfluenceClient | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Return accessible pages belonging to the selected root's descendant tree."""
    api = client or ConfluenceClient(
        ConfluenceConfig.from_env(root_page_id=root_page_id, space_id=space_id)
    )
    selected_root = root_page_id or api.config.root_page_id
    if not selected_root:
        raise ConfluenceDiscoveryError(
            "A root page ID is required via root_page_id or CONFLUENCE_HOMEPAGE_ID"
        )
    root = api.get_page(str(selected_root))
    selected_space = space_id or api.config.space_id or root.get("spaceId")
    if not selected_space:
        raise ConfluenceDiscoveryError(
            f"Could not determine a space ID for root page {selected_root}"
        )
    pages_by_id = {str(page["id"]): page for page in api.iter_pages(str(selected_space))}
    pages_by_id[str(root["id"])] = {**pages_by_id.get(str(root["id"]), {}), **root}
    scoped = [
        page
        for page in pages_by_id.values()
        if _belongs_to_tree(str(page["id"]), str(selected_root), pages_by_id)
    ]
    scoped.sort(key=lambda page: _sort_key(page, str(selected_root), pages_by_id))
    link_targets = {
        str(page.get("title") or ""): api.absolute_url(str(page.get("_links", {}).get("webui") or ""))
        for page in pages_by_id.values()
        if page.get("title") and page.get("_links", {}).get("webui")
    }
    if max_pages is not None:
        scoped = scoped[: max(0, max_pages)]
    return {
        "connector": "confluence",
        "root_page_id": str(selected_root),
        "space_id": str(selected_space),
        "base_url": api.config.base_url,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(scoped),
        "pages": scoped,
        "link_targets": link_targets,
    }


def _belongs_to_tree(
    page_id: str, root_page_id: str, pages_by_id: dict[str, dict[str, Any]]
) -> bool:
    current: str | None = page_id
    visited: set[str] = set()
    while current and current not in visited:
        if current == root_page_id:
            return True
        visited.add(current)
        page = pages_by_id.get(current)
        current = str(page.get("parentId")) if page and page.get("parentId") else None
    return False


def _sort_key(
    page: dict[str, Any], root_page_id: str, pages_by_id: dict[str, dict[str, Any]]
) -> tuple[int, tuple[str, ...], int, str]:
    lineage = []
    current = str(page["id"])
    visited: set[str] = set()
    while current != root_page_id and current not in visited:
        visited.add(current)
        current_page = pages_by_id.get(current, {})
        lineage.append(str(current_page.get("title", "")).casefold())
        parent = current_page.get("parentId")
        if not parent:
            break
        current = str(parent)
    lineage.reverse()
    return (
        len(lineage),
        tuple(lineage),
        int(page.get("position") or 0),
        str(page.get("id")),
    )
