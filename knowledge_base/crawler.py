import re
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

output_dir = Path("knowledge_base/output")
output_dir.mkdir(parents=True, exist_ok=True)


def load_all_topic_articles(page) -> None:
    """
    Expand all lazy-loaded article results on a topic page.

    Salesforce may render only the first batch of articles and expose the
    remaining results behind a Load More button.
    """
    previous_article_count = -1
    click_count = 0
    max_clicks = 50

    while click_count < max_clicks:
        article_links = page.locator("a[href*='/s/article/']")
        current_article_count = article_links.count()

        if current_article_count == previous_article_count:
            page.wait_for_timeout(1_000)

        previous_article_count = current_article_count

        load_more = page.get_by_text(
            re.compile(r"^\s*load more\s*$", re.IGNORECASE)
        )

        if load_more.count() == 0:
            break

        button = load_more.last

        try:
            if not button.is_visible():
                break
        except Exception:
            break

        print(
            f"  Clicking Load More "
            f"(currently {current_article_count} article links)"
        )

        try:
            button.scroll_into_view_if_needed()

            before_count = current_article_count

            button.click(timeout=10_000)

            try:
                page.wait_for_function(
                    """
                    ([selector, previousCount]) => {
                        const links = document.querySelectorAll(selector);
                        const buttons = [...document.querySelectorAll(
                            'button, a, span, div'
                        )];

                        const loadMoreVisible = buttons.some(element => {
                            const text = (
                                element.innerText || ''
                            ).trim().toLowerCase();

                            const style = window.getComputedStyle(element);

                            return (
                                text === 'load more'
                                && style.display !== 'none'
                                && style.visibility !== 'hidden'
                            );
                        });

                        return (
                            links.length > previousCount
                            || !loadMoreVisible
                        );
                    }
                    """,
                    arg=[
                        "a[href*='/s/article/']",
                        before_count,
                    ],
                    timeout=15_000,
                )
            except Exception:
                page.wait_for_timeout(2_000)

            click_count += 1

        except Exception as error:
            print(f"  Could not click Load More: {error}")
            break

    final_count = page.locator("a[href*='/s/article/']").count()

    print(
        f"  Load More expansion complete: "
        f"{final_count} article links visible"
    )


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
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2_000)

        load_all_topic_articles(page)

        links = page.locator("a[href]")
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
print("Saved to knowledge_base/output/article_urls.txt")