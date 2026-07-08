from pathlib import Path
from playwright.sync_api import sync_playwright
from package_article import package_article
import json
import time

URL_FILE = Path("knowledge_base/output/article_urls.txt")
LOG_DIR = Path("knowledge_base/output/logs")
REPORT_FILE = LOG_DIR / "migration_report.json"

MAX_ARTICLES = 999
RETRIES = 2

LOG_DIR.mkdir(parents=True, exist_ok=True)


def read_urls() -> list[str]:
    if not URL_FILE.exists():
        raise FileNotFoundError(f"Could not find {URL_FILE}")

    return [
        line.strip()
        for line in URL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main():
    urls = read_urls()[:MAX_ARTICLES]

    report = {
        "project": "Project Atlas",
        "articles_requested": len(urls),
        "articles_packaged": 0,
        "articles_failed": 0,
        "results": [],
    }

    start_time = time.time()

    print("====================================")
    print("Project Atlas")
    print("Knowledge Base Migration")
    print("====================================")
    print(f"Articles to package: {len(urls)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})

        for index, url in enumerate(urls, start=1):
            print(f"\n[{index}/{len(urls)}] {url}")

            success = False
            last_error = None

            for attempt in range(1, RETRIES + 2):
                try:
                    print(f"Attempt {attempt}...")
                    metadata = package_article(page, url)

                    report["articles_packaged"] += 1
                    report["results"].append({
                        "url": url,
                        "status": "success",
                        "metadata": metadata,
                    })

                    print(f"✓ Packaged: {metadata['title']}")
                    success = True
                    break

                except Exception as e:
                    last_error = str(e)
                    print(f"✗ Failed attempt {attempt}: {last_error}")

            if not success:
                report["articles_failed"] += 1
                report["results"].append({
                    "url": url,
                    "status": "failed",
                    "error": last_error,
                })
                print("⚠ Skipped after retries")

        browser.close()

    report["duration_seconds"] = round(time.time() - start_time, 2)

    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n====================================")
    print("Migration test complete")
    print("====================================")
    print(f"Packaged: {report['articles_packaged']}")
    print(f"Failed: {report['articles_failed']}")
    print(f"Report: {REPORT_FILE}")


if __name__ == "__main__":
    main()