from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from playwright.sync_api import Page, sync_playwright


MEDIA_LIBRARY_URL = (
    "https://dev.stretchsense.com/wp-admin/upload.php?mode=list"
)

SESSION_DIR = Path(".atlas_browser_session")
OUTPUT_DIR = Path("output/wordpress/media_library")
FILES_DIR = OUTPUT_DIR / "files"
LOGS_DIR = OUTPUT_DIR / "logs"
DEBUG_DIR = OUTPUT_DIR / "debug"

DISCOVERY_JSON = LOGS_DIR / "media_discovery.json"
INVENTORY_JSON = LOGS_DIR / "media_inventory.json"
INVENTORY_CSV = LOGS_DIR / "media_inventory.csv"

EXPECTED_ITEMS = 1397

# Keep this at 5 for the first test. Change it to None for the full run.
MAX_ITEMS: int | None = None

# Set this to True to build the inventory without downloading files.
DISCOVER_ONLY = False

REQUEST_TIMEOUT_SECONDS = 300
CHUNK_SIZE = 1024 * 1024


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def safe_filename(value: str) -> str:
    value = unquote(value)
    value = re.sub(r'[\\/:*?"<>|]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "unnamed-asset"


def attachment_id_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    return query.get("post", [""])[0]


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()

    with file_path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)

    return digest.hexdigest()


def filename_from_response(
    response: requests.Response,
    fallback_url: str,
) -> str:
    disposition = response.headers.get("content-disposition", "")

    match = re.search(
        r'filename\*?=(?:UTF-8\'\'|["\']?)([^"\';]+)',
        disposition,
        re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1))

    response_name = Path(urlparse(response.url).path).name
    if response_name:
        return safe_filename(response_name)

    fallback_name = Path(urlparse(fallback_url).path).name
    if fallback_name:
        return safe_filename(fallback_name)

    content_type = response.headers.get("content-type", "").split(";")[0]
    extension = mimetypes.guess_extension(content_type) or ""
    return f"unnamed-asset{extension}"


def create_authenticated_session(context) -> requests.Session:
    session = requests.Session()

    for cookie in context.cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) "
                "AppleWebKit/537.36 Chrome Safari/537.36"
            )
        }
    )

    return session


def login_and_open_media_library(page: Page) -> None:
    page.goto(MEDIA_LIBRARY_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    print()
    print("=" * 68)
    print("Project Atlas: WordPress Media Library Mirror")
    print("=" * 68)
    print()
    print("In Chrome:")
    print("1. Log into WordPress if required.")
    print("2. Confirm the Media Library is displayed in List view.")
    print("3. Wait until the media table is visible.")
    print()

    input("When the Media Library list is visible, press Enter here... ")

    page.goto(MEDIA_LIBRARY_URL, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1_500)


def discover_media_entries(page: Page) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen_edit_urls: set[str] = set()
    visited_pages: set[str] = set()

    current_url = MEDIA_LIBRARY_URL
    page_number = 1

    while current_url and current_url not in visited_pages:
        visited_pages.add(current_url)

        print(f"Discovering Media Library page {page_number}...")

        page.goto(current_url, timeout=60_000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(750)

        rows = page.locator("table.wp-list-table tbody tr")

        for index in range(rows.count()):
            row = rows.nth(index)

            title_link = row.locator(
                "a.row-title[href*='post.php'][href*='action=edit']"
            )

            if title_link.count() == 0:
                title_link = row.locator(
                    "a[href*='post.php'][href*='action=edit']"
                )

            if title_link.count() == 0:
                continue

            link = title_link.first
            href = link.get_attribute("href")

            if not href:
                continue

            edit_url = urljoin(page.url, href)

            if edit_url in seen_edit_urls:
                continue

            seen_edit_urls.add(edit_url)

            filename_text = ""
            filename_cell = row.locator(".column-title .filename")
            if filename_cell.count():
                filename_text = clean_text(filename_cell.first.inner_text())

            date_text = ""
            date_cell = row.locator(".column-date")
            if date_cell.count():
                date_text = clean_text(date_cell.first.inner_text())

            author_text = ""
            author_cell = row.locator(".column-author")
            if author_cell.count():
                author_text = clean_text(author_cell.first.inner_text())

            entries.append(
                {
                    "attachment_id": attachment_id_from_url(edit_url),
                    "title": clean_text(link.inner_text()),
                    "listed_filename": filename_text,
                    "listed_date": date_text,
                    "author": author_text,
                    "edit_url": edit_url,
                }
            )

        next_link = page.locator(
            "a.next-page:not(.disabled), "
            "a[aria-label='Next page']:not(.disabled)"
        )

        if next_link.count() == 0:
            break

        href = next_link.first.get_attribute("href")
        if not href:
            break

        current_url = urljoin(page.url, href)
        page_number += 1

    return entries


def first_value(page: Page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector)

        if locator.count() == 0:
            continue

        try:
            value = locator.first.input_value()
            if value:
                return clean_text(value)
        except Exception:
            pass

        try:
            value = locator.first.inner_text()
            if value:
                return clean_text(value)
        except Exception:
            pass

    return ""


def first_attribute(
    page: Page,
    selectors: list[tuple[str, str]],
) -> str:
    for selector, attribute in selectors:
        locator = page.locator(selector)

        if locator.count() == 0:
            continue

        value = locator.first.get_attribute(attribute)
        if value:
            return clean_text(value)

    return ""


def extract_original_file_url(page: Page) -> str:
    url = first_value(
        page,
        [
            "#attachment_url",
            "input[name='attachment_url']",
            "input.copy-attachment-url",
            "input[value*='/wp-content/uploads/']",
        ],
    )

    if url.startswith(("http://", "https://")):
        return url

    url = first_attribute(
        page,
        [
            ("button.copy-attachment-url", "data-clipboard-text"),
            (".copy-attachment-url", "data-clipboard-text"),
            (
                "[data-clipboard-text*='/wp-content/uploads/']",
                "data-clipboard-text",
            ),
            ("a[href*='/wp-content/uploads/']", "href"),
        ],
    )

    if url.startswith(("http://", "https://")):
        return url

    candidates = page.locator("a[href*='/wp-content/uploads/']")

    for index in range(candidates.count()):
        href = candidates.nth(index).get_attribute("href")

        if not href:
            continue

        if re.search(r"-\d+x\d+\.[A-Za-z0-9]+(?:\?.*)?$", href):
            continue

        return href

    return ""


def extract_attachment_metadata(
    page: Page,
    entry: dict[str, str],
) -> dict[str, object]:
    page.goto(entry["edit_url"], timeout=60_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(750)

    return {
        **entry,
        "title": first_value(
            page,
            ["#title", "input[name='post_title']"],
        )
        or entry["title"],
        "caption": first_value(
            page,
            [
                "#attachment_caption",
                "textarea[name='excerpt']",
                "textarea[name='post_excerpt']",
            ],
        ),
        "alt_text": first_value(
            page,
            [
                "#attachment_alt",
                "input[name='_wp_attachment_image_alt']",
            ],
        ),
        "description": first_value(
            page,
            [
                "#content",
                "textarea[name='content']",
                "textarea[name='post_content']",
            ],
        ),
        "original_url": extract_original_file_url(page),
    }


def unique_destination(filename: str) -> Path:
    destination = FILES_DIR / safe_filename(filename)

    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    counter = 2

    while True:
        candidate = FILES_DIR / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def download_asset(
    session: requests.Session,
    asset_url: str,
) -> tuple[Path | None, dict[str, object]]:
    try:
        with session.get(
            asset_url,
            stream=True,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        ) as response:
            if response.status_code == 404:
                return None, {
                    "status": "missing_server_file",
                    "http_status": 404,
                    "content_type": "",
                    "error": "HTTP 404",
                }

            if not response.ok:
                return None, {
                    "status": "failed",
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "error": f"HTTP {response.status_code}",
                }

            content_type = response.headers.get("content-type", "").lower()

            if "text/html" in content_type:
                body_sample = response.raw.read(
                    64 * 1024,
                    decode_content=True,
                )
                body_text = body_sample.decode("utf-8", errors="ignore").lower()

                if (
                    "file does not exist" in body_text
                    or "/nas/content/live/stretchsense/" in body_text
                ):
                    return None, {
                        "status": "missing_server_file",
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "error": "WordPress reports that the file is missing",
                    }

                return None, {
                    "status": "failed",
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "error": "Asset URL returned HTML",
                }

            filename = filename_from_response(response, asset_url)
            destination = FILES_DIR / filename
            expected_size = int(
                response.headers.get("content-length", "0") or 0
            )

            if destination.exists():
                existing_size = destination.stat().st_size

                if not expected_size or existing_size == expected_size:
                    return destination, {
                        "status": "skipped_existing",
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "error": "",
                    }

                destination = unique_destination(filename)

            with destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        handle.write(chunk)

            return destination, {
                "status": "downloaded",
                "http_status": response.status_code,
                "content_type": content_type,
                "error": "",
            }

    except requests.Timeout:
        return None, {
            "status": "timeout",
            "http_status": "",
            "content_type": "",
            "error": "Request timed out",
        }

    except requests.RequestException as error:
        return None, {
            "status": "failed",
            "http_status": "",
            "content_type": "",
            "error": str(error),
        }


def load_existing_inventory() -> list[dict[str, object]]:
    if not INVENTORY_JSON.exists():
        return []

    try:
        data = json.loads(INVENTORY_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_inventory(records: list[dict[str, object]]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    INVENTORY_JSON.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fieldnames = [
        "sequence",
        "attachment_id",
        "title",
        "listed_filename",
        "original_url",
        "status",
        "saved_filename",
        "saved_path",
        "size_bytes",
        "sha256",
        "content_type",
        "http_status",
        "listed_date",
        "author",
        "caption",
        "alt_text",
        "description",
        "edit_url",
        "downloaded_at_utc",
        "error",
    ]

    rows = [
        {key: record.get(key, "") for key in fieldnames}
        for record in records
    ]

    with INVENTORY_CSV.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1600, "height": 1100},
        )

        page = context.pages[0] if context.pages else context.new_page()

        login_and_open_media_library(page)

        entries = discover_media_entries(page)

        DISCOVERY_JSON.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print()
        print("=" * 68)
        print("Media discovery complete")
        print("=" * 68)
        print(f"Attachments discovered: {len(entries)}")

        if len(entries) != EXPECTED_ITEMS:
            print(
                f"Note: WordPress previously showed approximately "
                f"{EXPECTED_ITEMS:,} items."
            )

        selected_entries = (
            entries[:MAX_ITEMS]
            if MAX_ITEMS is not None
            else entries
        )

        print(f"Items selected for this run: {len(selected_entries)}")
        print()

        if DISCOVER_ONLY:
            print(f"Discovery saved to: {DISCOVERY_JSON}")
            context.close()
            return

        session = create_authenticated_session(context)
        inventory = load_existing_inventory()

        completed_ids = {
            str(item.get("attachment_id", ""))
            for item in inventory
            if item.get("status") in {"downloaded", "skipped_existing"}
        }

        for sequence, entry in enumerate(selected_entries, start=1):
            attachment_id = entry["attachment_id"]

            print(
                f"[{sequence}/{len(selected_entries)}] "
                f"{entry['title']} (attachment {attachment_id})"
            )

            if attachment_id in completed_ids:
                print("  Skipped: already recorded as complete")
                continue

            try:
                metadata = extract_attachment_metadata(page, entry)
            except Exception as error:
                row = {
                    "sequence": sequence,
                    **entry,
                    "original_url": "",
                    "status": "metadata_error",
                    "saved_filename": "",
                    "saved_path": "",
                    "size_bytes": "",
                    "sha256": "",
                    "content_type": "",
                    "http_status": "",
                    "caption": "",
                    "alt_text": "",
                    "description": "",
                    "downloaded_at_utc": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "error": str(error),
                }

                inventory.append(row)
                write_inventory(inventory)
                print(f"  Failed to read metadata: {error}")
                continue

            original_url = str(metadata.get("original_url", ""))

            if not original_url:
                debug_file = DEBUG_DIR / f"{attachment_id}-edit-page.html"
                debug_file.write_text(page.content(), encoding="utf-8")

                row = {
                    "sequence": sequence,
                    **metadata,
                    "status": "url_not_found",
                    "saved_filename": "",
                    "saved_path": "",
                    "size_bytes": "",
                    "sha256": "",
                    "content_type": "",
                    "http_status": "",
                    "downloaded_at_utc": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "error": "Could not locate the original attachment URL",
                }

                inventory.append(row)
                write_inventory(inventory)
                print("  Failed: original file URL not found")
                continue

            file_path, result = download_asset(session, original_url)

            row: dict[str, object] = {
                "sequence": sequence,
                **metadata,
                "status": result["status"],
                "saved_filename": "",
                "saved_path": "",
                "size_bytes": "",
                "sha256": "",
                "content_type": result.get("content_type", ""),
                "http_status": result.get("http_status", ""),
                "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
                "error": result.get("error", ""),
            }

            if file_path and file_path.exists():
                row.update(
                    {
                        "saved_filename": file_path.name,
                        "saved_path": str(file_path),
                        "size_bytes": file_path.stat().st_size,
                        "sha256": sha256_file(file_path),
                    }
                )

                print(
                    f"  {result['status']}: {file_path.name} "
                    f"({file_path.stat().st_size:,} bytes)"
                )
            else:
                print(
                    f"  {result['status']}: "
                    f"{result.get('error', '')}"
                )

            inventory.append(row)
            write_inventory(inventory)

        context.close()

    elapsed = time.monotonic() - started

    print()
    print("=" * 68)
    print("WordPress Media Library mirror complete")
    print("=" * 68)
    print(f"Elapsed: {elapsed / 60:.1f} minutes")
    print(f"Files: {FILES_DIR}")
    print(f"CSV inventory: {INVENTORY_CSV}")
    print(f"JSON inventory: {INVENTORY_JSON}")
    print(f"Discovery: {DISCOVERY_JSON}")


if __name__ == "__main__":
    main()