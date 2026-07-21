"""
Upload staged files and manifest.json to Cloudflare R2.
"""

from __future__ import annotations

import mimetypes
import os
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

from .manifest import ROOT, write_manifest

load_dotenv()

BUCKET_NAME = os.getenv("R2_BUCKET")

IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
}


def get_r2_client():
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    access_key_id = os.getenv("R2_ACCESS_KEY_ID")
    secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key_id, secret_access_key, BUCKET_NAME]):
        raise RuntimeError("Missing Cloudflare R2 configuration in .env")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def object_key(local_path: Path) -> str:
    return local_path.relative_to(ROOT).as_posix()


def remote_size(client, key: str):

    try:
        response = client.head_object(
            Bucket=BUCKET_NAME,
            Key=key,
        )

        return response["ContentLength"]

    except ClientError as e:

        code = e.response["Error"]["Code"]

        if code in ("404", "NoSuchKey", "NotFound"):
            return None

        raise


def upload_file(client, local_path: Path):

    key = object_key(local_path)

    local_size = local_path.stat().st_size
    existing_size = remote_size(client, key)

    if existing_size == local_size:
        return "skipped"

    content_type, _ = mimetypes.guess_type(local_path.name)

    extra = {}

    if content_type:
        extra["ContentType"] = content_type

    for attempt in range(3):

        try:

            client.upload_file(
                str(local_path),
                BUCKET_NAME,
                key,
                ExtraArgs=extra,
            )

            return "uploaded"

        except Exception:

            if attempt == 2:
                raise

            time.sleep(2 ** attempt)


def publish():

    start = time.time()

    write_manifest()

    client = get_r2_client()

    files = [
        p
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
        and p.name not in IGNORED_FILES
    ]

    uploaded = 0
    skipped = 0

    total = len(files)

    for i, local_path in enumerate(files, start=1):

        print(f"[{i}/{total}] {object_key(local_path)}")

        result = upload_file(client, local_path)

        if result == "uploaded":
            uploaded += 1
            print("    ↑ uploaded")

        else:
            skipped += 1
            print("    ✓ unchanged")

    elapsed = round(time.time() - start, 2)

    print()
    print("=" * 60)
    print("Cloudflare R2 Publish Summary")
    print("=" * 60)
    print(f"Bucket        : {BUCKET_NAME}")
    print(f"Uploaded      : {uploaded}")
    print(f"Skipped       : {skipped}")
    print(f"Elapsed       : {elapsed}s")
    print("=" * 60)


if __name__ == "__main__":
    publish()