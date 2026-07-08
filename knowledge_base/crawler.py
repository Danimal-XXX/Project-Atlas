from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright

BASE_URL = "https://stretchsense.my.site.com"

TOPIC_URLS = [
    "https://stretchsense.my.site.com/defaulthelpcenter26Sep/s/topic/0TO5j000000cIVZGA2/hand-engine-release-notes",
    "https://stretchsense.my.site.com/defaulthelpcenter26Sep/s/topic/0TO5j000000cIVcGAM",
    "https://stretchsense.my.site.com/defaulthelpcenter26Sep/s/topic/0TOJ4000000PDRvOAO",
    "https://stretchsense.my.site.com/defaulthelpcenter26Sep/s/topic/0TO5j000000cIVeGAM",
    "https://stretchsense.my.site.com/defaulthelpcenter26Sep/s/topic/0TO5j000000cIVgGAM",
]

output_dir = Path("output")
output_dir.mkdir(exist_ok=True)

def clean_article_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?language=en_US"

def is_article_url(url: str) -> bool:
    return "/defaulthelpcenter26Sep/s/article/" in url

article_urls = set()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page(viewport={"width": 1600, "height": 1200})

    for topic_url in TOPIC_URLS:
        print(f"\nVisiting topic: {topic_url}")

        page.goto(topic_url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)

        links = page.locator("a")
        print(f"Found {links.count()} links")

        for i in range(links.count()):
            href = links.nth(i).get_attribute("href")
            text = links.nth(i).inner_text().strip()

            if not href:
                continue

            full_url = urljoin(BASE_URL, href)

            if is_article_url(full_url):
                clean_url = clean_article_url(full_url)
                article_urls.add(clean_url)
                print(f"  Article: {text[:80]}")

    browser.close()

article_urls = sorted(article_urls)

(output_dir / "article_urls.txt").write_text(
    "\n".join(article_urls),
    encoding="utf-8"
)

print("\n============================")
print("TOPIC CRAWL COMPLETE")
print("============================")
print(f"Article URLs found: {len(article_urls)}")
print("Saved to output/article_urls.txt")