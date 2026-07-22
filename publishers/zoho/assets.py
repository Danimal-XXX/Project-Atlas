"""Publish the validated Zoho bundle assets to a stable Cloudflare R2 prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from sources.confluence.utils import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUNDLE = PROJECT_ROOT / "output" / "zoho" / "confluence"
DEFAULT_STATE = PROJECT_ROOT / "inventory" / "zoho" / "confluence-r2-assets.json"
DEFAULT_PREFIX = "atlas/zoho/confluence/assets"


class ZohoAssetPublishError(RuntimeError):
    """Raised when the public asset repository cannot be verified."""


def publish_assets(
    *,
    bundle_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    prefix: str = DEFAULT_PREFIX,
) -> dict[str, Any]:
    load_dotenv()
    required = {
        "CLOUDFLARE_ACCOUNT_ID": os.getenv("CLOUDFLARE_ACCOUNT_ID"),
        "R2_ACCESS_KEY_ID": os.getenv("R2_ACCESS_KEY_ID"),
        "R2_SECRET_ACCESS_KEY": os.getenv("R2_SECRET_ACCESS_KEY"),
        "R2_BUCKET": os.getenv("R2_BUCKET"),
        "R2_PUBLIC_BASE_URL": os.getenv("R2_PUBLIC_BASE_URL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ZohoAssetPublishError("Missing R2 environment values: " + ", ".join(missing))
    prefix = prefix.strip("/")
    bundle = Path(bundle_dir or DEFAULT_BUNDLE)
    state_file = Path(state_path or DEFAULT_STATE)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    client = boto3.client(
        "s3",
        endpoint_url=(
            f"https://{required['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        ),
        aws_access_key_id=required["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=required["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )
    bucket = str(required["R2_BUCKET"])
    public_base = str(required["R2_PUBLIC_BASE_URL"]).rstrip("/")
    records = []
    uploaded = 0
    skipped = 0
    for asset in manifest.get("assets", []):
        source = bundle / asset["path"]
        _verify_file(source, asset["sha256"])
        key = f"{prefix}/{source.name}"
        if _remote_matches(client, bucket, key, asset["size_bytes"], asset["sha256"]):
            status = "skipped"
            skipped += 1
        else:
            extra: dict[str, Any] = {
                "Metadata": {"atlas-sha256": asset["sha256"]},
            }
            content_type, _ = mimetypes.guess_type(source.name)
            if content_type:
                extra["ContentType"] = content_type
            client.upload_file(str(source), bucket, key, ExtraArgs=extra)
            if not _remote_matches(
                client, bucket, key, asset["size_bytes"], asset["sha256"]
            ):
                raise ZohoAssetPublishError(f"R2 verification failed: {key}")
            status = "uploaded"
            uploaded += 1
        records.append(
            {
                "id": asset["id"],
                "key": key,
                "public_url": f"{public_base}/{quote(key, safe='/')}",
                "sha256": asset["sha256"],
                "size_bytes": asset["size_bytes"],
                "status": status,
            }
        )
    state = {
        "schema_version": "1.0",
        "publisher": "cloudflare-r2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bucket": bucket,
        "prefix": prefix,
        "public_base_url": f"{public_base}/{quote(prefix, safe='/')}",
        "asset_count": len(records),
        "uploaded": uploaded,
        "skipped": skipped,
        "assets": records,
    }
    atomic_write_json(state_file, state)
    return state


def _remote_matches(
    client: Any,
    bucket: str,
    key: str,
    size_bytes: int,
    sha256: str,
) -> bool:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    metadata = response.get("Metadata", {})
    return int(response.get("ContentLength", -1)) == int(size_bytes) and metadata.get(
        "atlas-sha256"
    ) == sha256


def _verify_file(path: Path, expected: str) -> None:
    if not path.is_file():
        raise ZohoAssetPublishError(f"Bundle asset not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ZohoAssetPublishError(f"Bundle asset checksum mismatch: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir")
    parser.add_argument("--state-path")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    arguments = parser.parse_args()
    report = publish_assets(
        bundle_dir=arguments.bundle_dir,
        state_path=arguments.state_path,
        prefix=arguments.prefix,
    )
    print(
        f"R2 assets verified: uploaded={report['uploaded']}, "
        f"skipped={report['skipped']}, total={report['asset_count']}"
    )
    print(f"Asset base URL: {report['public_base_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
