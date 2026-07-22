"""Exchange a Zoho Self Client authorization code without printing secrets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ZohoOAuthSetupError(RuntimeError):
    """Raised when the one-time Self Client exchange fails."""


def exchange_authorization_code(
    *,
    env_path: Path,
    code_env: str = "ZOHO_REFRESH_TOKEN",
) -> str:
    """Exchange a local authorization code and store only the refresh token."""
    values = {**dotenv_values(env_path), **os.environ}
    required = {
        "ZOHO_CLIENT_ID": values.get("ZOHO_CLIENT_ID"),
        "ZOHO_CLIENT_SECRET": values.get("ZOHO_CLIENT_SECRET"),
        code_env: values.get(code_env),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ZohoOAuthSetupError("Missing environment values: " + ", ".join(missing))
    accounts_url = str(
        values.get("ZOHO_ACCOUNTS_URL") or "https://accounts.zoho.eu"
    ).rstrip("/")
    response = requests.post(
        f"{accounts_url}/oauth/v2/token",
        data={
            "client_id": required["ZOHO_CLIENT_ID"],
            "client_secret": required["ZOHO_CLIENT_SECRET"],
            "grant_type": "authorization_code",
            "code": required[code_env],
        },
        timeout=45,
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise ZohoOAuthSetupError("Zoho returned an unreadable OAuth response") from error
    refresh_token = payload.get("refresh_token")
    if response.status_code >= 400 or not refresh_token:
        detail = payload.get("error") or "refresh_token was not returned"
        raise ZohoOAuthSetupError(f"Authorization-code exchange failed: {detail}")
    set_key(str(env_path), "ZOHO_REFRESH_TOKEN", str(refresh_token), quote_mode="never")
    api_domain = _desk_domain(accounts_url)
    set_key(str(env_path), "ZOHO_DESK_API_DOMAIN", api_domain, quote_mode="never")
    return api_domain


def repair_desk_domain(*, env_path: Path) -> str:
    values = {**dotenv_values(env_path), **os.environ}
    accounts_url = str(
        values.get("ZOHO_ACCOUNTS_URL") or "https://accounts.zoho.eu"
    ).rstrip("/")
    api_domain = _desk_domain(accounts_url)
    set_key(str(env_path), "ZOHO_DESK_API_DOMAIN", api_domain, quote_mode="never")
    return api_domain


def _desk_domain(accounts_url: str) -> str:
    suffix = accounts_url.removeprefix("https://accounts.zoho")
    domains = {
        ".com": "https://desk.zoho.com",
        ".eu": "https://desk.zoho.eu",
        ".in": "https://desk.zoho.in",
        ".com.au": "https://desk.zoho.com.au",
        ".jp": "https://desk.zoho.jp",
        ".ca": "https://desk.zohocloud.ca",
        ".sa": "https://desk.zoho.sa",
        ".sg": "https://desk.zoho.sg",
        ".ae": "https://desk.zoho.ae",
        ".com.cn": "https://desk.zoho.com.cn",
    }
    try:
        return domains[suffix]
    except KeyError as error:
        raise ZohoOAuthSetupError(
            f"Unsupported Zoho Accounts region: {accounts_url}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--code-env", default="ZOHO_REFRESH_TOKEN")
    parser.add_argument("--repair-desk-domain", action="store_true")
    arguments = parser.parse_args()
    if arguments.repair_desk_domain:
        repair_desk_domain(env_path=Path(arguments.env_file))
        print("Zoho Desk regional API domain repaired in .env.")
        return 0
    exchange_authorization_code(
        env_path=Path(arguments.env_file),
        code_env=arguments.code_env,
    )
    print("Zoho authorization code exchanged; the refresh token is stored in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
