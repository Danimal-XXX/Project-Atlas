from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


DOWNLOAD_PAGE = (
    "https://dev.stretchsense.com/my-account/software-downloads/"
)

SESSION_DIR = Path(".atlas_browser_session")


def main() -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(SESSION_DIR),
            headless=False,
            viewport={"width": 1600, "height": 1100},
            accept_downloads=True,
        )

        page = context.pages[0] if context.pages else context.new_page()

        def log_request(request) -> None:
            if request.resource_type in {
                "document",
                "xhr",
                "fetch",
            }:
                print(
                    f"REQUEST  [{request.resource_type}] "
                    f"{request.method} {request.url}"
                )

        def log_response(response) -> None:
            content_type = response.headers.get("content-type", "")
            disposition = response.headers.get(
                "content-disposition",
                "",
            )

            if (
                response.request.resource_type
                in {"document", "xhr", "fetch"}
                or disposition
                or "application/" in content_type
            ):
                print(
                    f"RESPONSE [{response.status}] "
                    f"{response.url}"
                )
                print(f"         Content-Type: {content_type}")

                if disposition:
                    print(
                        "         Content-Disposition: "
                        f"{disposition}"
                    )

        page.on("request", log_request)
        page.on("response", log_response)

        page.goto(DOWNLOAD_PAGE, timeout=60_000)
        page.wait_for_load_state("domcontentloaded")

        print()
        print("=" * 64)
        print("Customer Download Trace")
        print("=" * 64)
        print()
        print("In Chrome:")
        print("1. Make sure the software download page is visible.")
        print("2. Click the Download control manually.")
        print("3. Wait several seconds.")
        print("4. Return to Terminal.")
        print()

        input("After clicking Download, press Enter here... ")

        print()
        print(f"Current browser URL: {page.url}")
        print("Trace complete.")

        context.close()


if __name__ == "__main__":
    main()