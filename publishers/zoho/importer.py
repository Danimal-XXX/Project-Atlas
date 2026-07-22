"""Import a validated Atlas HTML bundle into Zoho Desk Knowledge Base."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin
from zipfile import ZIP_DEFLATED, ZipFile

from bs4 import BeautifulSoup

from atlas.publisher import validate_publisher_input
from sources.confluence.utils import atomic_write_json

from .client import ZohoConfig, ZohoDeskClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = PROJECT_ROOT / "output" / "zoho" / "confluence"
DEFAULT_CANONICAL = PROJECT_ROOT / "knowledge_base" / "confluence"
DEFAULT_STATE = PROJECT_ROOT / "inventory" / "zoho" / "confluence-import-state.json"
ZOHO_ATTACHMENT_LIMIT = 50
ZOHO_ATTACHMENT_SIZE_LIMIT = 20 * 1024 * 1024


class ZohoImporterError(RuntimeError):
    """Raised when a bundle cannot be safely imported or verified."""


class ZohoArticleClient(Protocol):
    def iter_articles(self, category_id: str): ...
    def get_article(self, article_id: str) -> dict[str, Any]: ...
    def create_article(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def update_article(self, article_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    def upload_attachment(
        self, article_id: str, locale: str, source: Path
    ) -> dict[str, Any]: ...
    def iter_attachments(self, article_id: str, locale: str): ...
    def delete_attachment(
        self, article_id: str, locale: str, attachment_id: str
    ) -> None: ...


@dataclass(frozen=True)
class BundleArticle:
    atlas_id: str
    title: str
    slug: str
    html_path: Path
    html_sha256: str
    canonical: dict[str, Any]


def import_bundle(
    *,
    apply: bool = False,
    config: ZohoConfig | None = None,
    client: ZohoArticleClient | None = None,
    bundle_dir: str | Path | None = None,
    canonical_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    link_state_paths: list[str | Path] | None = None,
    asset_base_url: str | None = None,
    upload_assets: bool = True,
    adopt_existing: bool = True,
    force: bool = False,
    limit: int | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Plan or apply a resumable, idempotent Atlas-to-Zoho import.

    Dry-run mode performs every local checksum and schema check but never reads
    credentials, contacts Zoho, or changes import state.
    """
    bundle = Path(bundle_dir or DEFAULT_BUNDLE)
    canonical = Path(canonical_dir or DEFAULT_CANONICAL)
    state_file = Path(state_path or DEFAULT_STATE)
    manifest, all_articles, assets = _load_and_validate_bundle(bundle, canonical)
    articles = list(all_articles)
    if only:
        articles = [article for article in articles if article.atlas_id in only]
        unknown = only - {article.atlas_id for article in articles}
        if unknown:
            raise ZohoImporterError("Unknown Atlas article IDs: " + ", ".join(sorted(unknown)))
    if limit is not None:
        if limit < 1:
            raise ZohoImporterError("limit must be at least 1")
        articles = articles[:limit]
    referenced_assets = {
        asset_id
        for article in articles
        for asset_id in article.canonical.get("assets", [])
    }
    dry_run_report = {
        "mode": "dry-run",
        "source_connector": manifest["source_connector"],
        "article_count": len(articles),
        "asset_count": len(referenced_assets),
        "checksums_valid": True,
        "schema_valid": True,
        "remote_contacted": False,
        "actions": [
            {"atlas_id": article.atlas_id, "title": article.title, "action": "compare"}
            for article in articles
        ],
        "note": "Run with --apply after Zoho credentials and destination IDs are configured.",
    }
    if not apply:
        return dry_run_report

    config = config or ZohoConfig.from_env()
    client = client or ZohoDeskClient(config)
    state = _load_state(state_file, config, manifest["source_connector"])
    remote_articles = list(client.iter_articles(config.category_id))
    remote_by_id = {str(item.get("id")): item for item in remote_articles if item.get("id")}
    remote_by_permalink = {
        str(item.get("permalink")): item
        for item in remote_articles
        if item.get("permalink")
    }
    remote_by_title = {
        str(item.get("title")).strip().casefold(): item
        for item in remote_articles
        if item.get("title")
    }
    article_state: dict[str, dict[str, Any]] = state["articles"]
    actions: list[dict[str, Any]] = []

    # Establish every destination ID first so internal Atlas links can be
    # rewritten to final Zoho article URLs during the second pass.
    for article in articles:
        record = article_state.setdefault(article.atlas_id, {})
        remote = _resolve_remote(
            article,
            record,
            remote_by_id=remote_by_id,
            remote_by_permalink=remote_by_permalink,
            remote_by_title=remote_by_title,
            adopt_existing=adopt_existing,
        )
        if remote is None:
            payload = _article_payload(
                article,
                answer=_extract_answer(article.html_path),
                config=config,
                status="Draft",
                include_locale=True,
            )
            remote = client.create_article(payload)
            action = "created"
        else:
            action = (
                "skipped"
                if not force and record.get("html_sha256") == article.html_sha256
                else "updated"
            )
        zoho_id = str(remote.get("id") or record.get("zoho_id") or "")
        if not zoho_id:
            raise ZohoImporterError(f"Zoho did not return an article ID for {article.atlas_id}")
        record.update(
            {
                "zoho_id": zoho_id,
                "title": article.title,
                "permalink": str(remote.get("permalink") or article.slug),
                "portal_url": _remote_url(remote, record),
                "pending": action != "skipped",
                "assets": record.get("assets", {}),
            }
        )
        if not record["portal_url"]:
            fetched = client.get_article(zoho_id)
            record["portal_url"] = _remote_url(fetched, remote, record)
        remote_by_id[zoho_id] = remote
        _save_state(state_file, state)
        actions.append({"atlas_id": article.atlas_id, "zoho_id": zoho_id, "action": action})

    link_state = dict(article_state)
    for link_state_path in link_state_paths or []:
        candidate = Path(link_state_path)
        if not candidate.is_file():
            raise ZohoImporterError(f"Link-state file not found: {candidate}")
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        records = payload.get("articles", {})
        if not isinstance(records, dict):
            raise ZohoImporterError(f"Invalid link-state file: {candidate}")
        link_state.update(records)
    filename_to_url = _article_url_map(
        all_articles,
        link_state,
        remote_by_permalink=remote_by_permalink,
        remote_by_title=remote_by_title,
    )
    for article, action in zip(articles, actions):
        record = article_state[article.atlas_id]
        if action["action"] == "skipped":
            continue
        zoho_id = str(record["zoho_id"])
        source_answer = _extract_answer(article.html_path)
        required_assets, inline_assets = _required_article_assets(
            source_answer,
            assets=assets,
            bundle=bundle,
        )
        asset_urls: dict[str, str] = {
            Path(asset["path"]).name: _data_url(bundle / asset["path"])
            for asset in inline_assets
        }
        existing_attachments = {
            str(item.get("name")): _attachment_result(item)
            for item in client.iter_attachments(zoho_id, config.locale)
            if item.get("name")
        }
        required_filenames = {
            _attachment_filename(bundle / asset["path"]) for asset in required_assets
        }
        inline_filenames = {Path(asset["path"]).name for asset in inline_assets}
        atlas_filenames = {
            _attachment_filename(bundle / asset["path"]) for asset in assets.values()
        }
        stale_filenames = sorted(
            (set(existing_attachments) & atlas_filenames)
            - required_filenames
            - inline_filenames
        )
        for filename in stale_filenames:
            attachment = existing_attachments[filename]
            attachment_id = attachment.get("resource_id")
            if not attachment_id:
                raise ZohoImporterError(
                    f"Cannot identify stale Atlas attachment for cleanup: {filename}"
                )
            client.delete_attachment(zoho_id, config.locale, str(attachment_id))
            existing_attachments.pop(filename, None)
            for asset_id, saved in list(record["assets"].items()):
                if saved.get("filename") == filename:
                    record["assets"].pop(asset_id)
            _save_state(state_file, state)

        for asset in required_assets:
            asset_id = str(asset["id"])
            asset_path = bundle / asset["path"]
            upload_filename = _attachment_filename(asset_path)
            if asset_base_url:
                asset_urls[Path(asset["path"]).name] = urljoin(
                    asset_base_url.rstrip("/") + "/", Path(asset["path"]).name
                )
                continue
            if not upload_assets:
                raise ZohoImporterError(
                    f"{article.atlas_id} has assets; enable uploads or set --asset-base-url"
                )
            saved = record["assets"].get(asset_id, {})
            if saved.get("sha256") == asset["sha256"] and saved.get("url"):
                attachment = saved
            elif upload_filename in existing_attachments:
                attachment = existing_attachments[upload_filename]
                attachment.update({"sha256": asset["sha256"], "filename": upload_filename})
                record["assets"][asset_id] = attachment
                _save_state(state_file, state)
            else:
                with _attachment_source(asset_path) as upload_path:
                    response = client.upload_attachment(zoho_id, config.locale, upload_path)
                attachment = _attachment_result(response)
                if not attachment.get("url"):
                    raise ZohoImporterError(
                        f"Zoho did not return a URL for attachment {asset_id}"
                    )
                attachment.update({"sha256": asset["sha256"], "filename": upload_filename})
                record["assets"][asset_id] = attachment
                _save_state(state_file, state)
            asset_urls[Path(asset["path"]).name] = str(attachment["url"])

        answer = _rewrite_answer(
            source_answer,
            filename_to_url=filename_to_url,
            asset_urls=asset_urls,
        )
        payload = _article_payload(article, answer=answer, config=config, status=config.status)
        remote = client.update_article(zoho_id, payload)
        verified = client.get_article(zoho_id)
        _verify_article(article, config, zoho_id, verified)
        record.update(
            {
                "html_sha256": article.html_sha256,
                "status": config.status,
                "portal_url": _remote_url(verified, remote, record),
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "pending": False,
            }
        )
        _save_state(state_file, state)

    return {
        "mode": "apply",
        "source_connector": manifest["source_connector"],
        "article_count": len(articles),
        "asset_count": len(referenced_assets),
        "remote_contacted": True,
        "created": sum(item["action"] == "created" for item in actions),
        "updated": sum(item["action"] == "updated" for item in actions),
        "skipped": sum(item["action"] == "skipped" for item in actions),
        "actions": actions,
        "state_path": str(state_file),
    }


def _load_and_validate_bundle(
    bundle: Path, canonical: Path
) -> tuple[dict[str, Any], list[BundleArticle], dict[str, dict[str, Any]]]:
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise ZohoImporterError(f"Zoho bundle manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("issues"):
        raise ZohoImporterError("Zoho bundle contains unresolved publisher issues")
    canonical_objects = {
        item["id"]: item
        for item in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((canonical / "articles").glob("*.json"))
        )
    }
    validate_publisher_input(list(canonical_objects.values()))
    articles: list[BundleArticle] = []
    for item in manifest.get("articles", []):
        path = bundle / item["path"]
        _verify_file(path, item["sha256"])
        canonical_object = canonical_objects.get(item["id"])
        if not canonical_object:
            raise ZohoImporterError(f"Canonical article is missing: {item['id']}")
        articles.append(
            BundleArticle(
                atlas_id=item["id"],
                title=item["title"],
                slug=canonical_object["slug"],
                html_path=path,
                html_sha256=item["sha256"],
                canonical=canonical_object,
            )
        )
    assets: dict[str, dict[str, Any]] = {}
    for item in manifest.get("assets", []):
        _verify_file(bundle / item["path"], item["sha256"])
        assets[item["id"]] = item
    if len(articles) != manifest.get("article_count"):
        raise ZohoImporterError("Zoho bundle article count does not match its manifest")
    if len(assets) != manifest.get("asset_count"):
        raise ZohoImporterError("Zoho bundle asset count does not match its manifest")
    return manifest, articles, assets


def _resolve_remote(
    article: BundleArticle,
    record: dict[str, Any],
    *,
    remote_by_id: dict[str, dict[str, Any]],
    remote_by_permalink: dict[str, dict[str, Any]],
    remote_by_title: dict[str, dict[str, Any]],
    adopt_existing: bool,
) -> dict[str, Any] | None:
    zoho_id = str(record.get("zoho_id") or "")
    if zoho_id and zoho_id in remote_by_id:
        return remote_by_id[zoho_id]
    if not adopt_existing:
        return None
    return remote_by_permalink.get(article.slug) or remote_by_title.get(
        article.title.strip().casefold()
    )


def _article_payload(
    article: BundleArticle,
    *,
    answer: str,
    config: ZohoConfig,
    status: str,
    include_locale: bool = False,
) -> dict[str, Any]:
    tags = article.canonical.get("taxonomy", {}).get("tags", [])
    payload: dict[str, Any] = {
        "title": article.title,
        "answer": answer,
        "categoryId": config.category_id,
        "status": status,
        "permission": config.permission,
        "permalink": article.slug,
        "tags": tags,
    }
    if include_locale:
        payload["locale"] = config.locale
    return payload


def _extract_answer(path: Path) -> str:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    body = soup.body or soup
    first_heading = body.find("h1")
    if first_heading and first_heading.get_text(" ", strip=True) == (soup.title.string if soup.title else ""):
        first_heading.decompose()
    return "".join(str(child) for child in body.contents).strip()


def _rewrite_answer(
    answer: str,
    *,
    filename_to_url: dict[str, str],
    asset_urls: dict[str, str],
) -> str:
    soup = BeautifulSoup(answer, "html.parser")
    unresolved: list[str] = []
    for tag in soup.find_all(["a", "img"]):
        attribute = "src" if tag.name == "img" else "href"
        value = str(tag.get(attribute) or "")
        clean, marker, fragment = value.partition("#")
        filename = Path(clean).name
        replacement: str | None = None
        if clean.startswith("../assets/"):
            replacement = asset_urls.get(filename)
        elif not clean.startswith(("http://", "https://", "mailto:")) and filename.endswith(
            ".html"
        ):
            replacement = filename_to_url.get(filename)
        if replacement:
            tag[attribute] = replacement + (f"#{fragment}" if marker else "")
        elif clean.startswith("../assets/") or (
            not clean.startswith(("http://", "https://", "mailto:"))
            and filename.endswith(".html")
        ):
            unresolved.append(value)
    if unresolved:
        raise ZohoImporterError(
            "Cannot publish unresolved local links: " + ", ".join(sorted(set(unresolved)))
        )
    return str(soup)


def _required_article_assets(
    answer: str,
    *,
    assets: dict[str, dict[str, Any]],
    bundle: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return referenced attachment assets and any tiny images to embed.

    Zoho Desk permits at most 50 attachments on an article translation. If an
    article references more than 50 files, the smallest image references are
    embedded as data URLs so no asset needs to be exposed through public
    storage. Unreferenced Confluence attachments are intentionally ignored.
    """
    soup = BeautifulSoup(answer, "html.parser")
    references: dict[str, str] = {}
    for tag in soup.find_all(["a", "img"]):
        attribute = "src" if tag.name == "img" else "href"
        value = str(tag.get(attribute) or "")
        if value.startswith("../assets/"):
            references[Path(value.partition("#")[0]).name] = tag.name
    by_filename = {Path(item["path"]).name: item for item in assets.values()}
    missing = sorted(set(references) - set(by_filename))
    if missing:
        raise ZohoImporterError(
            "Article references assets missing from the bundle: " + ", ".join(missing)
        )
    required = [by_filename[name] for name in sorted(references)]
    embed_count = max(0, len(required) - ZOHO_ATTACHMENT_LIMIT)
    if not embed_count:
        return required, []
    candidates = [
        item
        for item in required
        if references[Path(item["path"]).name] == "img"
        and (mimetypes.guess_type(Path(item["path"]).name)[0] or "").startswith("image/")
    ]
    candidates.sort(key=lambda item: (bundle / item["path"]).stat().st_size)
    if len(candidates) < embed_count:
        raise ZohoImporterError(
            f"Article needs {len(required)} attachments, exceeding Zoho's "
            f"{ZOHO_ATTACHMENT_LIMIT}-attachment limit"
        )
    inline = candidates[:embed_count]
    inline_ids = {str(item["id"]) for item in inline}
    return [item for item in required if str(item["id"]) not in inline_ids], inline


def _data_url(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _attachment_filename(path: Path) -> str:
    if path.stat().st_size > ZOHO_ATTACHMENT_SIZE_LIMIT:
        return path.name + ".zip"
    return path.name


@contextmanager
def _attachment_source(path: Path) -> Iterator[Path]:
    """Yield a Zoho-sized source, losslessly zipping oversized downloads."""
    if path.stat().st_size <= ZOHO_ATTACHMENT_SIZE_LIMIT:
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="atlas-zoho-") as temporary_directory:
        archive = Path(temporary_directory) / (path.name + ".zip")
        with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=9) as bundle:
            bundle.write(path, path.name)
        if archive.stat().st_size > ZOHO_ATTACHMENT_SIZE_LIMIT:
            raise ZohoImporterError(
                f"Asset remains larger than Zoho's 20 MB limit after ZIP compression: {path}"
            )
        yield archive


def _article_url_map(
    articles: list[BundleArticle],
    state: dict[str, dict[str, Any]],
    *,
    remote_by_permalink: dict[str, dict[str, Any]],
    remote_by_title: dict[str, dict[str, Any]],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for article in articles:
        record = state.get(article.atlas_id, {})
        remote = remote_by_permalink.get(article.slug) or remote_by_title.get(
            article.title.strip().casefold()
        )
        url = record.get("portal_url") or _remote_url(remote or {})
        # A one-article trial should still have working links. Until the target
        # is imported, retain its original audited source URL.
        url = url or article.canonical.get("source", {}).get("url")
        if url:
            mapping[article.html_path.name] = str(url)
    # Only fail if one selected article links to a selected article without a URL;
    # the second-pass rewrite will identify the actual missing target precisely.
    return mapping


def _attachment_result(payload: dict[str, Any]) -> dict[str, Any]:
    candidate: Any = payload
    for key in ("data", "attachments"):
        if isinstance(candidate, dict) and key in candidate:
            candidate = candidate[key]
    if isinstance(candidate, list):
        candidate = candidate[0] if candidate else {}
    if not isinstance(candidate, dict):
        return {}
    return {
        "resource_id": candidate.get("resourceId") or candidate.get("id"),
        "url": (
            candidate.get("url")
            or candidate.get("viewUrl")
            or candidate.get("downloadUrl")
            or candidate.get("href")
        ),
        "view_url": candidate.get("viewUrl"),
        "download_url": candidate.get("downloadUrl"),
        "name": candidate.get("name"),
    }


def _verify_article(
    article: BundleArticle,
    config: ZohoConfig,
    zoho_id: str,
    remote: dict[str, Any],
) -> None:
    problems = []
    if str(remote.get("id")) != zoho_id:
        problems.append("ID")
    if str(remote.get("categoryId")) != config.category_id:
        problems.append("category")
    if str(remote.get("title")) != article.title:
        problems.append("title")
    if config.status and remote.get("status") and remote.get("status") != config.status:
        problems.append("status")
    answer = str(remote.get("answer") or "")
    soup = BeautifulSoup(answer, "html.parser")
    for tag in soup.find_all(["a", "img"]):
        attribute = "src" if tag.name == "img" else "href"
        value = str(tag.get(attribute) or "")
        if value.startswith("../assets/") or (
            not value.startswith(("http://", "https://", "mailto:", "#"))
            and Path(value.partition("#")[0]).suffix == ".html"
        ):
            problems.append("local links")
            break
    if problems:
        raise ZohoImporterError(
            f"Zoho verification failed for {article.atlas_id}: " + ", ".join(problems)
        )


def _remote_url(*values: dict[str, Any]) -> str:
    for value in values:
        for key in ("portalUrl", "portal_url", "webUrl"):
            if value.get(key):
                return str(value[key])
    return ""


def _verify_file(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise ZohoImporterError(f"Bundle file not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ZohoImporterError(f"Bundle checksum mismatch: {path}")


def _load_state(
    path: Path, config: ZohoConfig, source_connector: str
) -> dict[str, Any]:
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
        if str(state.get("category_id")) != config.category_id:
            raise ZohoImporterError(
                "Import state belongs to another Zoho category; use a different --state-path"
            )
        return state
    return {
        "schema_version": "1.0",
        "publisher": "zoho-desk",
        "source_connector": source_connector,
        "org_id": config.org_id,
        "category_id": config.category_id,
        "updated_at": None,
        "articles": {},
    }


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform authenticated Zoho writes")
    parser.add_argument("--bundle-dir")
    parser.add_argument("--canonical-dir")
    parser.add_argument("--state-path")
    parser.add_argument(
        "--link-state-path",
        action="append",
        default=[],
        help="Additional import state used to resolve cross-category article links",
    )
    parser.add_argument("--asset-base-url")
    parser.add_argument("--skip-assets", action="store_true")
    parser.add_argument("--no-adopt-existing", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-render mapped articles even when source checksums are unchanged",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--status", choices=["Draft", "Published", "Review"])
    parser.add_argument("--category-id")
    parser.add_argument("--accounts-url")
    arguments = parser.parse_args()
    config = (
        ZohoConfig.from_env(
            category_id=arguments.category_id,
            accounts_url=arguments.accounts_url,
        )
        if arguments.apply
        else None
    )
    if config and arguments.status:
        config = ZohoConfig(**{**config.__dict__, "status": arguments.status})
    report = import_bundle(
        apply=arguments.apply,
        config=config,
        bundle_dir=arguments.bundle_dir,
        canonical_dir=arguments.canonical_dir,
        state_path=arguments.state_path,
        link_state_paths=arguments.link_state_path,
        asset_base_url=arguments.asset_base_url,
        upload_assets=not arguments.skip_assets,
        adopt_existing=not arguments.no_adopt_existing,
        force=arguments.force,
        limit=arguments.limit,
        only=set(arguments.only) or None,
    )
    if report["mode"] == "dry-run":
        print(
            f"Dry run passed: {report['article_count']} articles, "
            f"{report['asset_count']} referenced assets, all local checks valid."
        )
    else:
        print(
            f"Zoho import verified: created={report['created']}, "
            f"updated={report['updated']}, skipped={report['skipped']}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
