from pathlib import Path
from bs4 import BeautifulSoup
from markdownify import markdownify as md


PACKAGES_DIR = Path("knowledge_base/output/packages")


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Remove HelpScout/Salesforce styling noise but keep the actual content.
    for tag in soup.find_all(True):
        tag.attrs = {
            key: value
            for key, value in tag.attrs.items()
            if key in ["href", "src", "alt", "title"]
        }

    # Remove empty spans.
    for span in soup.find_all("span"):
        span.unwrap()

    return str(soup)


def convert_package_to_markdown(package_dir: Path) -> None:
    html_file = package_dir / "article.html"
    markdown_file = package_dir / "article.md"

    if not html_file.exists():
        print(f"Skipping {package_dir.name}: no article.html")
        return

    html = html_file.read_text(encoding="utf-8")
    clean = clean_html(html)

    markdown = md(
        clean,
        heading_style="ATX",
        bullets="-",
    )

    markdown = markdown.strip() + "\n"

    markdown_file.write_text(markdown, encoding="utf-8")

    print(f"Created Markdown: {markdown_file}")


def main():
    package_dirs = [
        path
        for path in sorted(PACKAGES_DIR.iterdir())
        if path.is_dir()
    ]

    print("====================================")
    print("Project Atlas Markdown Publisher")
    print("====================================")
    print(f"Packages found: {len(package_dirs)}")

    for package_dir in package_dirs:
        convert_package_to_markdown(package_dir)

    print("\nMarkdown publishing complete.")


if __name__ == "__main__":
    main()