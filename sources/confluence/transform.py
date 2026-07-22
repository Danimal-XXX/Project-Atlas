"""Transform Confluence storage-format HTML into canonical Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify


@dataclass(frozen=True)
class TransformResult:
    markdown: str
    warnings: tuple[str, ...]


def storage_to_markdown(
    storage_html: str,
    attachments: list[dict[str, Any]] | None = None,
    pages_by_title: dict[str, str] | None = None,
    *,
    current_page_id: str | None = None,
    current_page_title: str | None = None,
    confluence_base_url: str | None = None,
) -> TransformResult:
    """Convert storage HTML and replace attachment references with Atlas asset URIs."""
    soup = BeautifulSoup(storage_html or "", "html.parser")
    attachment_map = _attachment_map(attachments or [])
    warnings: list[str] = []
    for macro in list(soup.find_all(_is_macro)):
        _replace_macro(soup, macro, attachment_map, warnings)
    for confluence_link in list(soup.find_all(_is_confluence_link)):
        _replace_confluence_link(
            soup,
            confluence_link,
            pages_by_title or {},
            attachment_map,
            warnings,
            current_page_id=current_page_id,
            current_page_title=current_page_title,
            confluence_base_url=confluence_base_url,
        )
    for image in list(soup.find_all(_is_confluence_image)):
        attachment = image.find(_is_attachment_tag)
        filename = _namespaced_attribute(attachment, "filename") if attachment else None
        asset = attachment_map.get(filename or "")
        replacement = soup.new_tag("img")
        replacement["alt"] = filename or "Confluence image"
        if asset:
            replacement["src"] = f"atlas-asset://{asset['asset_id']}"
        else:
            replacement["src"] = ""
            warnings.append(f"Embedded image was not mirrored: {filename or '<unknown>'}")
        image.replace_with(replacement)
    for link in soup.find_all("a"):
        href = str(link.get("href") or "")
        filename = _attachment_filename_from_url(href)
        asset = attachment_map.get(filename) if filename else None
        if asset:
            link["href"] = f"atlas-asset://{asset['asset_id']}"
    rendered = markdownify(
        str(soup),
        heading_style="ATX",
        bullets="-",
        strip=["ac:layout", "ac:layout-section", "ac:layout-cell"],
        keep_inline_images_in=["td", "th", "a", "p"],
    )
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return TransformResult(
        markdown=rendered + ("\n" if rendered else ""),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _replace_macro(
    soup: BeautifulSoup,
    macro: Tag,
    attachment_map: dict[str, dict[str, Any]],
    warnings: list[str],
) -> None:
    name = _namespaced_attribute(macro, "name") or "unknown"
    plain_body = macro.find(lambda tag: isinstance(tag, Tag) and tag.name in {
        "ac:plain-text-body", "ac:rich-text-body"
    })
    body_text = plain_body.get_text("\n", strip=True) if plain_body else macro.get_text("\n", strip=True)
    if name in {"code", "noformat"}:
        pre = soup.new_tag("pre")
        code = soup.new_tag("code")
        code.string = body_text
        pre.append(code)
        macro.replace_with(pre)
        return
    if name in {"info", "note", "warning", "tip", "panel"}:
        blockquote = soup.new_tag("blockquote")
        blockquote.append(NavigableString(f"{name.upper()}: {body_text}"))
        macro.replace_with(blockquote)
        return
    if name == "status":
        title = _macro_parameter(macro, "title") or body_text or "Status"
        badge = soup.new_tag("span")
        badge.string = f"[Status: {title}]"
        macro.replace_with(badge)
        return
    if name == "view-file":
        attachment = macro.find(_is_attachment_tag)
        filename = _namespaced_attribute(attachment, "filename") if attachment else None
        asset = attachment_map.get(filename or "")
        if asset:
            download = soup.new_tag("a", href=f"atlas-asset://{asset['asset_id']}")
            download.string = filename or "Download attachment"
            macro.replace_with(download)
            return
    if name == "drawio":
        diagram_name = _macro_parameter(macro, "diagramName")
        source_asset = attachment_map.get(diagram_name or "")
        preview_asset = attachment_map.get(f"{diagram_name}.png" if diagram_name else "")
        wrapper = soup.new_tag("div")
        if preview_asset:
            preview = soup.new_tag("img")
            preview["src"] = f"atlas-asset://{preview_asset['asset_id']}"
            preview["alt"] = diagram_name or "Draw.io diagram"
            wrapper.append(preview)
        if source_asset:
            source = soup.new_tag("a", href=f"atlas-asset://{source_asset['asset_id']}")
            source.string = f"Download {diagram_name}"
            wrapper.append(source)
        if source_asset or preview_asset:
            macro.replace_with(wrapper)
            return
    placeholder = soup.new_tag("div")
    placeholder.append(NavigableString(f"[Confluence macro: {name}]"))
    if body_text:
        placeholder.append(soup.new_tag("br"))
        placeholder.append(NavigableString(body_text))
    macro.replace_with(placeholder)
    warnings.append(f"Unsupported Confluence macro preserved as text: {name}")


def _replace_confluence_link(
    soup: BeautifulSoup,
    link: Tag,
    pages_by_title: dict[str, str],
    attachment_map: dict[str, dict[str, Any]],
    warnings: list[str],
    *,
    current_page_id: str | None,
    current_page_title: str | None,
    confluence_base_url: str | None,
) -> None:
    page = link.find(lambda tag: isinstance(tag, Tag) and tag.name == "ri:page")
    url = link.find(lambda tag: isinstance(tag, Tag) and tag.name == "ri:url")
    attachment = link.find(_is_attachment_tag)
    space = link.find(lambda tag: isinstance(tag, Tag) and tag.name == "ri:space")
    body = link.find(
        lambda tag: isinstance(tag, Tag)
        and tag.name in {"ac:plain-text-link-body", "ac:link-body"}
    )
    label = body.get_text(" ", strip=True) if body else ""
    replacement = soup.new_tag("a")
    if page:
        title = _namespaced_attribute(page, "content-title") or label
        target = pages_by_title.get(title)
        anchor = _namespaced_attribute(link, "anchor")
        replacement["href"] = f"{target}#{anchor}" if target and anchor else target or ""
        replacement.string = label or title
        if not target:
            warnings.append(f"Unresolved Confluence page link: {title}")
    elif url:
        target_url = _namespaced_attribute(url, "value") or ""
        replacement["href"] = target_url
        replacement.string = label or target_url
    elif attachment:
        filename = _namespaced_attribute(attachment, "filename") or label
        asset = attachment_map.get(filename)
        replacement["href"] = f"atlas-asset://{asset['asset_id']}" if asset else ""
        replacement.string = label or filename
        if not asset:
            warnings.append(f"Unresolved Confluence attachment link: {filename}")
    elif space:
        space_key = _namespaced_attribute(space, "space-key") or label
        replacement["href"] = (
            f"{confluence_base_url.rstrip('/')}/wiki/spaces/{space_key}"
            if confluence_base_url and space_key
            else ""
        )
        replacement.string = label or space_key
        if not replacement["href"]:
            warnings.append(f"Unresolved Confluence space link: {space_key}")
    elif (
        current_page_id
        and current_page_title
        and label
        and current_page_title.casefold().endswith(label.casefold())
    ):
        replacement["href"] = f"atlas-knowledge://confluence-{current_page_id}"
        replacement.string = label
    else:
        replacement["href"] = ""
        replacement.string = label or link.get_text(" ", strip=True)
        warnings.append("Unresolved Confluence link without page or URL target")
    link.replace_with(replacement)


def _attachment_map(attachments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for attachment in attachments:
        if attachment.get("download_status") != "available":
            continue
        for name in (attachment.get("title"), attachment.get("filename")):
            if name:
                result[str(name)] = attachment
    return result


def _attachment_filename_from_url(value: str) -> str | None:
    if "/download/attachments/" not in value:
        return None
    tail = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    from urllib.parse import unquote

    return unquote(tail) or None


def _is_macro(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name in {"ac:structured-macro", "ac:macro"}


def _is_confluence_image(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name == "ac:image"


def _is_attachment_tag(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name == "ri:attachment"


def _is_confluence_link(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name == "ac:link"


def _macro_parameter(macro: Tag, parameter_name: str) -> str | None:
    for parameter in macro.find_all(
        lambda tag: isinstance(tag, Tag) and tag.name == "ac:parameter"
    ):
        if _namespaced_attribute(parameter, "name") == parameter_name:
            return parameter.get_text(" ", strip=True)
    return None


def _namespaced_attribute(tag: Tag | None, local_name: str) -> str | None:
    if tag is None:
        return None
    for key, value in tag.attrs.items():
        if str(key) == local_name or str(key).endswith(f":{local_name}"):
            return str(value)
    return None
