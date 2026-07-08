from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from collections import Counter
import csv
import json

PACKAGES_DIR = Path("knowledge_base/output/packages")
OUTPUT_CSV = Path("knowledge_base/output/logs/asset_inventory.csv")
OUTPUT_JSON = Path("knowledge_base/output/logs/asset_inventory.json")


def classify_url(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    if not url:
        return "empty"

    if "dropbox.com" in domain:
        return "dropbox"

    if "atlassian.net" in domain or "confluence" in domain:
        return "confluence"

    if "youtube.com" in domain or "youtu.be" in domain:
        return "video"

    if "stretchsense.my.site.com" in domain and "/s/article/" in path:
        return "salesforce_article"

    if "stretchsense.my.site.com" in domain and "/s/topic/" in path:
        return "salesforce_topic"

    if "stretchsense.com" in domain:
        return "stretchsense_site"

    if path.endswith((".pdf", ".zip", ".exe", ".dmg", ".pkg", ".csv", ".xlsx", ".docx")):
        return "download"

    if domain:
        return "external"

    return "relative_or_internal"


def main():
    inventory = []

    for article_dir in sorted(PACKAGES_DIR.iterdir()):
        article_html = article_dir / "article.html"
        metadata_json = article_dir / "metadata.json"

        if not article_html.exists():
            continue

        metadata = {}
        if metadata_json.exists():
            metadata = json.loads(metadata_json.read_text(encoding="utf-8"))

        title = metadata.get("title", article_dir.name)
        source_url = metadata.get("source_url", "")

        soup = BeautifulSoup(article_html.read_text(encoding="utf-8"), "html.parser")

        for link in soup.find_all("a"):
            href = link.get("href", "").strip()
            text = link.get_text(" ", strip=True)
            link_type = classify_url(href)

            inventory.append({
                "article_title": title,
                "article_folder": article_dir.name,
                "source_url": source_url,
                "link_text": text,
                "url": href,
                "type": link_type,
            })

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "article_title",
                "article_folder",
                "source_url",
                "link_text",
                "url",
                "type",
            ],
        )
        writer.writeheader()
        writer.writerows(inventory)

    OUTPUT_JSON.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    type_counts = Counter(item["type"] for item in inventory)

    print("\nSummary by link type:")
    for link_type, count in sorted(type_counts.items()):
        print(f"{link_type}: {count}")
    print("Asset discovery complete")
    print(f"Links found: {len(inventory)}")
    print(f"CSV: {OUTPUT_CSV}")
    print(f"JSON: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()