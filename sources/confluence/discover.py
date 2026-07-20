from dotenv import load_dotenv
import os
import requests

load_dotenv()

BASE_URL = os.getenv("ATLASSIAN_BASE_URL")
EMAIL = os.getenv("ATLASSIAN_EMAIL")
TOKEN = os.getenv("ATLASSIAN_API_TOKEN")
HOMEPAGE_ID = os.getenv("CONFLUENCE_HOMEPAGE_ID")

required_values = {
    "ATLASSIAN_BASE_URL": BASE_URL,
    "ATLASSIAN_EMAIL": EMAIL,
    "ATLASSIAN_API_TOKEN": TOKEN,
    "CONFLUENCE_HOMEPAGE_ID": HOMEPAGE_ID,
}

missing = [
    name
    for name, value in required_values.items()
    if not value
]

if missing:
    raise RuntimeError(
        "Missing environment values: "
        + ", ".join(missing)
    )

session = requests.Session()
session.auth = (EMAIL, TOKEN)
session.headers.update({
    "Accept": "application/json"
})

page_url = (
    f"{BASE_URL}/wiki/api/v2/pages/"
    f"{HOMEPAGE_ID}"
)

response = session.get(
    page_url,
    timeout=30,
)

print(f"HTTP Status: {response.status_code}")

if not response.ok:
    print(response.text)
    response.raise_for_status()

page = response.json()

print("\nHomepage details:")
print(f"Page ID:   {page.get('id')}")
print(f"Title:     {page.get('title')}")
print(f"Space ID:  {page.get('spaceId')}")
print(f"Parent ID: {page.get('parentId')}")
print(f"Status:    {page.get('status')}")

space_id = page.get("spaceId")

if space_id:
    space_url = (
        f"{BASE_URL}/wiki/api/v2/spaces/"
        f"{space_id}"
    )

    space_response = session.get(
        space_url,
        timeout=30,
    )

    print(
        f"\nSpace lookup status: "
        f"{space_response.status_code}"
    )

    if space_response.ok:
        space = space_response.json()

        print("\nConfirmed space:")
        print(f"Space ID: {space.get('id')}")
        print(f"Key:      {space.get('key')}")
        print(f"Name:     {space.get('name')}")
    else:
        print(space_response.text)