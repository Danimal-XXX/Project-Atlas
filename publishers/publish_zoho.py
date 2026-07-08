from pathlib import Path
from bs4 import BeautifulSoup
import base64
import json
import mimetypes
import re

PACKAGES_DIR = Path("knowledge_base/output/packages")
ZOHO_DIR = Path("knowledge_base/output/zoho_import")


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def image_to_base64(image_path: Path) -> str | None:
    if not image_path.exists():
        return None

    mime_type, _ = mimetypes.guess_type(image_path)

    if not mime_type:
        mime_type = "image/png"

    encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def create_zoho_html(package_dir: Path) -> None:
    html_file = package_dir / "article.html"
    metadata_file = package_dir / "metadata.json"

    if not html_file.exists():
        print(f"Skipping {package_dir.name}: no article.html")
        return

    metadata = {}
    if metadata_file.exists():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))

    title = metadata.get("title", package_dir.name)
    filename = f"{safe_filename(title)}.html"

    soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "html.parser")

    for img in soup.find_all("img"):
        src = img.get("src")

        if not src:
            continue

        if src.startswith("data:"):
            continue

        image_path = package_dir / src
        embedded = image_to_base64(image_path)

        if embedded:
            img["src"] = embedded
        else:
            print(f"Missing image: {image_path}")

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
</head>
<body>
<h1>{title}</h1>
{soup}
</body>
</html>
"""

    ZOHO_DIR.mkdir(parents=True, exist_ok=True)
    output_file = ZOHO_DIR / filename
    output_file.write_text(full_html, encoding="utf-8")

    print(f"Created: {output_file}")


def main():
    package_dirs = [
        path for path in sorted(PACKAGES_DIR.iterdir())
        if path.is_dir()
    ]

    print("====================================")
    print("Project Atlas Zoho HTML Publisher")
    print("====================================")
    print(f"Packages found: {len(package_dirs)}")

    for package_dir in package_dirs:
        create_zoho_html(package_dir)

    print("\nZoho HTML export complete.")
    print(f"Output folder: {ZOHO_DIR}")


if __name__ == "__main__":
    main()