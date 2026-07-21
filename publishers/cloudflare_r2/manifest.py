"""
Generate manifest.json for the Cloudflare R2 bucket.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .checksum import file_size, sha256

ROOT = Path("staging/cfr2/stretchsense-downloads")

CATEGORIES = [
    "current",
    "previous",
    "beta",
]

IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    "manifest.json",
}


def build_manifest() -> dict:
    manifest = {
        "manifest_version": "1.0",
        "bucket": "stretchsense-downloads",
        "generated_at": datetime.now(UTC).isoformat(),
        "categories": {},
    }

    for category in CATEGORIES:
        folder = ROOT / category
        files = []

        if folder.exists():
            for file_path in sorted(folder.rglob("*")):

                if not file_path.is_file():
                    continue

                if file_path.name in IGNORED_FILES:
                    continue

                files.append(
                    {
                        "filename": file_path.name,
                        "path": file_path.relative_to(ROOT).as_posix(),
                        "size_bytes": file_size(file_path),
                        "sha256": sha256(file_path),
                    }
                )

        manifest["categories"][category] = files

    return manifest


def write_manifest() -> None:
    output = ROOT / "manifest.json"

    output.parent.mkdir(parents=True, exist_ok=True)

    output.write_text(
        json.dumps(build_manifest(), indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {output}")


if __name__ == "__main__":
    write_manifest()