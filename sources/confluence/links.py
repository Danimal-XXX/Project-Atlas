"""Resolve cross-space page links referenced by mirrored Confluence storage HTML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from sources.confluence.client import ConfluenceClient, ConfluenceConfig
from sources.confluence.mirror import DEFAULT_STAGING
from sources.confluence.utils import atomic_write_json


def resolve_link_targets(
    *,
    staging_dir: str | Path | None = None,
    client: ConfluenceClient | None = None,
) -> dict[str, Any]:
    """Resolve only page titles referenced by the mirrored crawl."""
    staging = Path(staging_dir or DEFAULT_STAGING)
    discovery_path = staging / "discovery.json"
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    targets = dict(discovery.get("link_targets", {}))
    references = _referenced_pages(staging)
    api = client or ConfluenceClient(ConfluenceConfig.from_env())
    resolved = []
    unresolved = []
    for title, space_key in sorted(references.items()):
        if title in targets:
            continue
        matches = [
            page
            for page in api.find_pages_by_title(title)
            if str(page.get("title", "")).casefold() == title.casefold()
        ]
        selected = _select_match(matches, space_key)
        if not selected:
            unresolved.append(title)
            continue
        webui = selected.get("_links", {}).get("webui")
        if not webui:
            unresolved.append(title)
            continue
        targets[title] = api.absolute_url(str(webui))
        resolved.append({"title": title, "page_id": str(selected["id"]), "url": targets[title]})
    discovery["link_targets"] = targets
    discovery["cross_space_links"] = resolved
    discovery["unresolved_link_titles"] = unresolved
    atomic_write_json(discovery_path, discovery)
    return {"resolved": resolved, "unresolved": unresolved}


def _referenced_pages(staging: Path) -> dict[str, str | None]:
    references: dict[str, str | None] = {}
    for path in sorted(staging.glob("pages/*/body.storage.html")):
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        for page in soup.find_all(
            lambda tag: isinstance(tag, Tag) and tag.name == "ri:page"
        ):
            title = _attribute(page, "content-title")
            if title:
                references[title] = _attribute(page, "space-key")
    return references


def _select_match(
    matches: list[dict[str, Any]], space_key: str | None
) -> dict[str, Any] | None:
    if not matches:
        return None
    if space_key:
        for match in matches:
            webui = str(match.get("_links", {}).get("webui", ""))
            if f"/spaces/{space_key}/" in webui:
                return match
    return matches[0] if len(matches) == 1 else None


def _attribute(tag: Tag, local_name: str) -> str | None:
    for key, value in tag.attrs.items():
        if str(key) == local_name or str(key).endswith(f":{local_name}"):
            return str(value)
    return None
