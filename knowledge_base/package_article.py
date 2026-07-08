from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import json
import re

BASE_URL = "https://stretchsense.my.site.com"
OUTPUT_DIR = Path("knowledge_base/output/packages")


def slugify(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text)
    return text.strip("-")


def package_article(page, url: str) -> dict:
    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)

    title = page.locator("h2.article-head").first.inner_text()
    summary = page.locator(".article-summary").first.inner_text()
    body = page.locator(".slds-rich-text-editor__output").first

    body_html = body.inner_html()
    body_text = body.inner_text()

    slug = slugify(title)
    article_dir = OUTPUT_DIR / slug
    images_dir = article_dir / "images"

    article_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    soup = BeautifulSoup(body_html, "html.parser")

    image_count = 0
    failed_images = []

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        image_count += 1
        image_url = urljoin(BASE_URL, src)
        image_name = f"image_{image_count:03}.png"
        image_path = images_dir / image_name

        response = page.request.get(image_url)

        if response.ok:
            image_path.write_bytes(response.body())
            img["src"] = f"images/{image_name}"
        else:
            failed_images.append(image_url)

    metadata = {
    "project": "Project Atlas",
    "source_system": "Salesforce Knowledge",
    "title": title,
    "summary": summary,
    "source_url": url,
    "slug": slug,
    "package_folder": str(article_dir),
    "image_count": image_count,
    "failed_images": failed_images,
    "status": "packaged",
    }

    (article_dir / "article.html").write_text(str(soup), encoding="utf-8")
    (article_dir / "article.txt").write_text(body_text, encoding="utf-8")
    (article_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (article_dir / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    page.screenshot(path=article_dir / "screenshot.png", full_page=True)


    return metadata