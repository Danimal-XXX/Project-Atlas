from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import Locator, Page, sync_playwright


ADMIN_LIST_URL = (
    "https://dev.stretchsense.com/wp-admin/"
    "edit.php?post_type=mocap-download-files"
)

OUTPUT_DIR = Path("output/wordpress_assets/download_catalogue")
SESSION_DIR = Path(".atlas_browser_session")

INVENTORY_CSV = OUTPUT_DIR / "download_inventory.csv"
INVENTORY_JSON = OUTPUT_DIR / "download_inventory.json"
DEBUG_DIR = OUTPUT_DIR / "debug"

EXPECTED_ITEMS = 16


def clean_text(value: str | None) -> str:
    """Collapse whitespace and return a clean string."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def safe_value(locator: Locator) -> str:
    """Read the value or visible text from a form element."""
    try:
        tag_name = locator.evaluate("(element) => element.tagName.toLowerCase()")

        if tag_name in {"input", "textarea", "select"}:
            return clean_text(locator.input_value())

        return clean_text(locator.inner_text())
    except Exception:
        return ""


def post_id_from_url(url: str) -> str:
    """Extract the WordPress post ID from a post.php edit URL."""
    query = parse_qs(urlparse(url).query)
    return query.get("post", [""])[0]


def discover_edit_pages(page: Page) -> list[dict[str, str]]:
    """
    Discover the download-entry edit pages from the WordPress admin list.

    Handles pagination in case the page size changes later.
    """
    records: list[dict[str, str]] = []
    visited_pages: set[str] = set()
    current_url = ADMIN_LIST_URL

    while current_url and current_url not in visited_pages:
        visited_pages.add(current_url)

        print(f"Reading catalogue page: {current_url}")
        page.goto(current_url, timeout=60_000)
        page.wait_for_load_state("domcontentloaded")

        # WordPress post titles normally use row-title links.
        title_links = page.locator(
            "a.row-title[href*='post.php'][href*='action=edit']"
        )

        for index in range(title_links.count()):
            link = title_links.nth(index)
            edit_url = urljoin(page.url, link.get_attribute("href") or "")
            title = clean_text(link.inner_text())

            records.append(
                {
                    "post_id": post_id_from_url(edit_url),
                    "post_title": title,
                    "edit_url": edit_url,
                }
            )

        # WordPress's next-page button.
        next_link = page.locator(
            "a.next-page:not(.disabled), "
            "a[aria-label='Next page']:not(.disabled)"
        )

        if next_link.count() == 0:
            break

        href = next_link.first.get_attribute("href")
        current_url = urljoin(page.url, href) if href else ""

    # Deduplicate by edit URL.
    unique: dict[str, dict[str, str]] = {}
    for record in records:
        unique[record["edit_url"]] = record

    return list(unique.values())


def find_labelled_control(page: Page, label_pattern: str) -> str:
    """
    Find a form field by nearby label text.

    This is useful for fields such as Download Section where the exact
    WordPress/ACF field name may be generated dynamically.
    """
    labels = page.locator("label")

    for index in range(labels.count()):
        label = labels.nth(index)
        label_text = clean_text(label.inner_text())

        if not re.search(label_pattern, label_text, re.IGNORECASE):
            continue

        for_id = label.get_attribute("for")

        if for_id:
            control = page.locator(f"#{for_id}")
            if control.count():
                return safe_value(control.first)

        # Try the nearest enclosing field container.
        container = label.locator(
            "xpath=ancestor::*["
            "contains(@class,'acf-field') or "
            "contains(@class,'field')"
            "][1]"
        )

        if container.count():
            controls = container.locator("input, textarea, select")
            for control_index in range(controls.count()):
                value = safe_value(controls.nth(control_index))
                if value:
                    return value

    return ""


def extract_related_software(page: Page) -> list[str]:
    """Read checked related-software values where available."""
    values: list[str] = []

    checked = page.locator(
        "input[type='checkbox']:checked, "
        "input[type='radio']:checked"
    )

    for index in range(checked.count()):
        control = checked.nth(index)

        value = clean_text(control.get_attribute("value"))
        control_id = control.get_attribute("id")

        label_text = ""
        if control_id:
            label = page.locator(f"label[for='{control_id}']")
            if label.count():
                label_text = clean_text(label.first.inner_text())

        chosen = label_text or value

        if chosen and chosen not in {"1", "on", "yes"}:
            values.append(chosen)

    return list(dict.fromkeys(values))


def looks_like_file_value(value: str) -> bool:
    """Identify a filename or URL likely to represent a downloadable asset."""
    value_lower = value.lower().split("?")[0]

    extensions = (
        ".zip",
        ".exe",
        ".msi",
        ".dmg",
        ".pkg",
        ".pdf",
        ".fbx",
        ".unitypackage",
        ".bin",
        ".json",
        ".csv",
        ".xlsx",
        ".docx",
        ".7z",
        ".rar",
    )

    return value_lower.endswith(extensions)


def row_control_values(row: Locator) -> list[str]:
    """Return non-empty values from the inputs within one repeater row."""
    values: list[str] = []
    controls = row.locator("input:not([type='hidden']), textarea, select")

    for index in range(controls.count()):
        value = safe_value(controls.nth(index))
        if value:
            values.append(value)

    return values


def extract_download_rows(page: Page) -> list[dict[str, str]]:
    """
    Extract rows from the Download Files repeater.

    The script uses several selector strategies because WordPress plugins and
    ACF installations can use different HTML class names.
    """
    candidate_rows = page.locator(
        ".acf-field[data-name='download_files'] .acf-row:not(.acf-clone), "
        ".acf-repeater .acf-row:not(.acf-clone), "
        "tr.acf-row:not(.acf-clone), "
        ".download-files tr, "
        "table tbody tr"
    )

    extracted: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for index in range(candidate_rows.count()):
        row = candidate_rows.nth(index)
        values = row_control_values(row)

        if not values:
            continue

        file_value = next(
            (value for value in values if looks_like_file_value(value)),
            "",
        )

        if not file_value:
            continue

        display_name = next(
            (
                value
                for value in values
                if value != file_value
                and value.lower() not in {"yes", "no", "1", "0"}
            ),
            "",
        )

        display_status = next(
            (
                value
                for value in values
                if value.lower() in {"yes", "no", "1", "0"}
            ),
            "",
        )

        key = (display_name, file_value)
        if key in seen:
            continue

        seen.add(key)

        extracted.append(
            {
                "display_name": display_name,
                "raw_file_value": file_value,
                "display_status": display_status,
            }
        )

    return extracted


def resolve_file_value(page: Page, raw_value: str) -> tuple[str, str]:
    """
    Attempt to convert a raw filename or URL into a usable URL.

    Full URLs are accepted immediately. Relative paths are resolved against
    the admin site. Bare filenames remain unresolved until the download
    mechanism or upload path is identified.
    """
    value = raw_value.strip()

    if value.startswith(("https://", "http://")):
        return value, "direct_url"

    if value.startswith("/"):
        return urljoin(page.url, value), "relative_url"

    return "", "filename_only_unresolved"


def extract_entry(
    page: Page,
    entry: dict[str, str],
    position: int,
    total: int,
) -> list[dict[str, object]]:
    """Extract one WordPress download catalogue entry."""
    print(
        f"[{position}/{total}] "
        f"{entry['post_title']} (post {entry['post_id']})"
    )

    page.goto(entry["edit_url"], timeout=60_000)
    page.wait_for_load_state("domcontentloaded")

    # Allow ACF/JavaScript fields to finish rendering.
    page.wait_for_timeout(1_500)

    title_field = page.locator("#title")
    post_title = (
        clean_text(title_field.input_value())
        if title_field.count()
        else entry["post_title"]
    )

    download_section = find_labelled_control(
        page,
        r"download\s+section",
    )

    related_software = extract_related_software(page)
    rows = extract_download_rows(page)

    if not rows:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        debug_name = (
            f"{entry['post_id'] or position}_"
            f"{re.sub(r'[^A-Za-z0-9]+', '-', post_title).strip('-')}"
        )

        (DEBUG_DIR / f"{debug_name}.html").write_text(
            page.content(),
            encoding="utf-8",
        )

        page.screenshot(
            path=DEBUG_DIR / f"{debug_name}.png",
            full_page=True,
        )

        print("  Warning: no download rows detected; debug files saved.")

    results: list[dict[str, object]] = []

    for row_number, row in enumerate(rows, start=1):
        resolved_url, resolution_status = resolve_file_value(
            page,
            row["raw_file_value"],
        )

        results.append(
            {
                "post_id": entry["post_id"],
                "post_title": post_title,
                "edit_url": entry["edit_url"],
                "row_number": row_number,
                "display_name": row["display_name"],
                "raw_file_value": row["raw_file_value"],
                "resolved_url": resolved_url,
                "resolution_status": resolution_status,
                "display_status": row["display_status"],
                "download_section": download_section,
                "related_software": related_software,
            }
        )

        print(
            f"  Row {row_number}: "
            f"{row['display_name'] or '(no display name)'} -> "
            f"{row['raw_file_value']}"
        )

    return results


def write_outputs(records: list[dict[str, object]]) -> None:
    """Write CSV and JSON inventories."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    json_records = []
    csv_records = []

    for record in records:
        json_records.append(record)

        csv_record = dict(record)
        csv_record["related_software"] = " | ".join(
            record.get("related_software", [])
        )
        csv_records.append(csv_record)

    INVENTORY_JSON.write_text(
        json.dumps(json_records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    fieldnames = [
        "post_id",
        "post_title",
        "edit_url",
        "row_number",
        "display_name",
        "raw_file_value",
        "resolved_url",
        "resolution_status",
        "display_status",
        "download_section",
        "related_software",
    ]

    with INVENTORY_CSV.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_records)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1600, "height": 1100},
        )

        page = context.pages[0] if context.pages else context.new_page()

        page.goto(ADMIN_LIST_URL, timeout=60_000)

        print()
        print("Log into WordPress in the browser window.")
        print("Wait until the Mocap Download Files list is visible.")

        input("When you can see the download list, press Enter here... ")

        page.goto(ADMIN_LIST_URL, timeout=60_000)
        page.wait_for_load_state("domcontentloaded")

        try:
            page.locator("a.row-title").first.wait_for(timeout=30_000)
        except Exception:
            print("The download list was not detected.")
            print(f"Current browser URL: {page.url}")
            context.close()
            return

        entries = discover_edit_pages(page)

        print()
        print(f"Catalogue entries discovered: {len(entries)}")

        if len(entries) != EXPECTED_ITEMS:
            print(
                f"Warning: expected approximately {EXPECTED_ITEMS}, "
                f"but found {len(entries)}."
            )

        inventory: list[dict[str, object]] = []

        for position, entry in enumerate(entries, start=1):
            try:
                inventory.extend(
                    extract_entry(
                        page,
                        entry,
                        position,
                        len(entries),
                    )
                )
            except Exception as error:
                print(f"  Failed to inspect entry: {error}")

                inventory.append(
                    {
                        "post_id": entry["post_id"],
                        "post_title": entry["post_title"],
                        "edit_url": entry["edit_url"],
                        "row_number": "",
                        "display_name": "",
                        "raw_file_value": "",
                        "resolved_url": "",
                        "resolution_status": "entry_error",
                        "display_status": "",
                        "download_section": "",
                        "related_software": [],
                        "error": str(error),
                    }
                )

        write_outputs(inventory)

        filename_only = sum(
            1
            for item in inventory
            if item.get("resolution_status")
            == "filename_only_unresolved"
        )

        direct_urls = sum(
            1
            for item in inventory
            if item.get("resolution_status")
            in {"direct_url", "relative_url"}
        )

        print()
        print("====================================")
        print("WordPress download discovery complete")
        print("====================================")
        print(f"Catalogue entries: {len(entries)}")
        print(f"File rows found: {len(inventory)}")
        print(f"Direct/resolved URLs: {direct_urls}")
        print(f"Filename-only records: {filename_only}")
        print(f"CSV: {INVENTORY_CSV}")
        print(f"JSON: {INVENTORY_JSON}")

        context.close()


if __name__ == "__main__":
    main()