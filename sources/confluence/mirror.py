from dotenv import load_dotenv
import csv
import os
import requests

load_dotenv()

BASE_URL = os.getenv("ATLASSIAN_BASE_URL")
EMAIL = os.getenv("ATLASSIAN_EMAIL")
TOKEN = os.getenv("ATLASSIAN_API_TOKEN")
SPACE_ID = os.getenv("CONFLUENCE_SPACE_ID")

session = requests.Session()
session.auth = (EMAIL, TOKEN)
session.headers.update({
    "Accept": "application/json"
})

os.makedirs("inventory/confluence", exist_ok=True)

csv_file = "inventory/confluence/pages.csv"

with open(csv_file, "w", newline="", encoding="utf-8") as f:

    writer = csv.writer(f)

    writer.writerow([
        "Page ID",
        "Title",
        "Parent ID",
        "Status",
        "Created",
        "Version"
    ])

    url = (
        f"{BASE_URL}/wiki/api/v2/spaces/"
        f"{SPACE_ID}/pages"
        "?depth=all&limit=250"
    )

    total = 0

    while url:

        print(f"Reading {url}")

        response = session.get(url)

        response.raise_for_status()

        data = response.json()

        for page in data["results"]:

            writer.writerow([
                page.get("id"),
                page.get("title"),
                page.get("parentId"),
                page.get("status"),
                page.get("createdAt"),
                page.get("version", {}).get("number"),
            ])

            total += 1

        print(f"Collected {total} pages")

        next_url = data.get("_links", {}).get("next")

        if next_url:

            if next_url.startswith("/"):

                url = BASE_URL + next_url

            else:

                url = next_url

        else:

            url = None

print(f"\nFinished. {total} pages written to {csv_file}")