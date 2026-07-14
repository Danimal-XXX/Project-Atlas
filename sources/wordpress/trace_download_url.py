from pathlib import Path
from playwright.sync_api import sync_playwright

SESSION_DIR = Path(".atlas_browser_session")

START_URL = "https://dev.stretchsense.com/wp-admin/edit.php?post_type=mocap-download-files"


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=".atlas_browser_session",
            headless=False,
            viewport={"width": 1600, "height": 1100},
        )

        page = context.pages[0] if context.pages else context.new_page()

        def log_request(request) -> None:
            url = request.url.lower()

            if any(
                extension in url
                for extension in [
                    ".zip",
                    ".exe",
                    ".fbx",
                    ".pdf",
                    ".unitypackage",
                    "download",
                ]
            ):
                print(f"REQUEST: {request.url}")

        def log_response(response) -> None:
            url = response.url.lower()
            content_type = response.headers.get("content-type", "")

            if (
                any(
                    extension in url
                    for extension in [
                        ".zip",
                        ".exe",
                        ".fbx",
                        ".pdf",
                        ".unitypackage",
                    ]
                )
                or "application/octet-stream" in content_type
                or "application/zip" in content_type
            ):
                print(f"RESPONSE: {response.status} {response.url}")
                print(f"CONTENT-TYPE: {content_type}")

        page.on("request", log_request)
        page.on("response", log_response)

        page.goto(START_URL, timeout=60_000)

        print()
        print("In the browser:")
        print("1. Open one download entry.")
        print("2. Open its public-facing page if available.")
        print("3. Click the actual download link.")
        print()
        input("After clicking the download, press Enter here... ")

        context.close()


if __name__ == "__main__":
    main()