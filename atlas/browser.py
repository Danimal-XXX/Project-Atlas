from pathlib import Path
from playwright.sync_api import sync_playwright

CHROME_PROFILE = (
    "/Users/danwalker/Library/Application Support/"
    "Google/Chrome/Atlas"
)


def launch_browser(playwright):
    return playwright.chromium.launch_persistent_context(
        channel="chrome",
        user_data_dir=CHROME_PROFILE,
        headless=False,
        viewport={"width": 1600, "height": 1100},
    )