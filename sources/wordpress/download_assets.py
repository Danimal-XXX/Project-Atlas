from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


DOWNLOAD_PAGE = (
    "https://dev.stretchsense.com/my-account/software-downloads/"
)

SESSION_DIR = Path(".atlas_browser_session")

OUTPUT_DIR = Path("output/wordpress/software_downloads")
FILES_DIR = OUTPUT_DIR / "files"
LOGS_DIR = OUTPUT_DIR / "logs"

INVENTORY_CSV = LOGS_DIR / "software_download_inventory.csv"
INVENTORY_JSON = LOGS_DIR / "software_download_inventory.json"
PAGE_HTML = LOGS_DIR / "software_downloads_page.html"
PAGE_SCREENSHOT = LOGS_DIR / "software_downloads_page.png"

MAX_DOWNLOADS: int | None = None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def safe_filename(value: str) -> str:
    value = unquote(value)
    value = re.sub(r'[\\/:*?"<>|]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or "downloaded-file"


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()

    with file_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


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


def filename_from_headers(
    content_disposition: str,
    fallback: str,
) -> str:
    match = re.search(
        r'filename\*?=(?:UTF-8\'\'|["\']?)([^"\';]+)',
        content_disposition,
        re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1))

    return safe_filename(fallback)


def login_and_open_download_page(page: Page) -> None:
    page.goto(DOWNLOAD_PAGE, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    print()
    print("=" * 64)
    print("Project Atlas: WordPress Software Download Preservation")
    print("=" * 64)
    print()
    print("In the Chrome window:")
    print("1. Log into StretchSense if required.")
    print("2. Confirm all software downloads are visible.")
    print("3. Wait until the page has fully loaded.")
    print()

    input("When the full download page is visible, press Enter here... ")

    page.goto(DOWNLOAD_PAGE, timeout=60_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2_000)


def discover_download_links(page: Page) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    links = page.locator("a[href*='mocap-dl=']")

    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href")

        if not href:
            continue

        url = urljoin(page.url, href)

        if url in seen_urls:
            continue

        seen_urls.add(url)

        label = clean_text(
            link.inner_text()
            or link.get_attribute("title")
            or link.get_attribute("aria-label")
        )

        parent_text = clean_text(
            link.locator("xpath=ancestor::*[self::li or self::tr or self::div][1]")
            .inner_text()
        )

        records.append(
            {
                "label": label or parent_text or f"Download {index + 1}",
                "url": url,
            }
        )

    return records


def download_file(
    page: Page,
    record: dict[str, str],
) -> tuple[Path | None, str]:
    """Trigger the authenticated download without navigating away."""

    try:
        with page.expect_download(timeout=600_000) as download_info:
            page.evaluate(
                """
                (url) => {
                    const link = document.createElement("a");
                    link.href = url;
                    link.style.display = "none";
                    document.body.appendChild(link);
                    link.click();
                    link.remove();
                }
                """,
                record["url"],
            )

        download = download_info.value

        suggested_name = safe_filename(download.suggested_filename)
        destination = unique_destination(suggested_name)

        print(f"  Downloading: {suggested_name}")
        download.save_as(destination)  # Waits until download is complete

        failure = download.failure()
        if failure:
            return None, f"download_failed: {failure}"

        return destination, "browser_download"

    except PlaywrightTimeoutError:
            page_text = page.locator("body").inner_text().lower()

            if (
                "file does not exist" in page_text
                or "/nas/content/live/stretchsense/" in page_text
            ):
                return None, "missing_server_file"

            return None, "download_timeout"

    except Exception as error:
                return None, f"download_error: {error}"

def write_inventory(records: list[dict[str, object]]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    INVENTORY_JSON.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fieldnames = [
        "sequence",
        "label",
        "source_url",
        "status",
        "method",
        "saved_filename",
        "saved_path",
        "size_bytes",
        "sha256",
        "downloaded_at_utc",
        "error",
    ]

    with INVENTORY_CSV.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    inventory: list[dict[str, object]] = []

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1600, "height": 1100},
            accept_downloads=True,
        )

        page = context.pages[0] if context.pages else context.new_page()

        login_and_open_download_page(page)

        PAGE_HTML.write_text(
            page.content(),
            encoding="utf-8",
        )

        page.screenshot(
            path=PAGE_SCREENSHOT,
            full_page=True,
        )

        links = discover_download_links(page)

        print()
        print(f"Download links discovered: {len(links)}")

        selected_links = (
            links[:MAX_DOWNLOADS]
            if MAX_DOWNLOADS is not None
            else links
        )

        print(f"Downloads selected for this run: {len(selected_links)}")
        print()

        for sequence, record in enumerate(selected_links, start=1):
            print(
                f"[{sequence}/{len(selected_links)}] "
                f"{record['label']}"
            )

            file_path, method = download_file(page, record)

            row: dict[str, object] = {
                "sequence": sequence,
                "label": record["label"],
                "source_url": record["url"],
                "status": "failed",
                "method": method,
                "saved_filename": "",
                "saved_path": "",
                "size_bytes": "",
                "sha256": "",
                "downloaded_at_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
                "error": "",
            }

            if file_path and file_path.exists():
                row.update(
                    {
                        "status": "downloaded",
                        "saved_filename": file_path.name,
                        "saved_path": str(file_path),
                        "size_bytes": file_path.stat().st_size,
                        "sha256": sha256_file(file_path),
                    }
                )

                print(
                    f"  Saved: {file_path.name} "
                    f"({file_path.stat().st_size:,} bytes)"
                )
            else:
                row["error"] = method
                print(f"  Failed: {method}")

            inventory.append(row)
            write_inventory(inventory)

        context.close()

    downloaded = sum(
        1
        for item in inventory
        if item["status"] == "downloaded"
    )

    failed = len(inventory) - downloaded

    print()
    print("=" * 64)
    print("WordPress software preservation run complete")
    print("=" * 64)
    print(f"Links processed: {len(inventory)}")
    print(f"Downloaded: {downloaded}")
    print(f"Failed: {failed}")
    print(f"Files: {FILES_DIR}")
    print(f"CSV inventory: {INVENTORY_CSV}")
    print(f"JSON inventory: {INVENTORY_JSON}")


if __name__ == "__main__":
    main()