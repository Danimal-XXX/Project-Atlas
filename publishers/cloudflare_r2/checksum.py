"""
checksum.py

Utility functions for Cloudflare R2 publishing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    """
    Calculate the SHA-256 checksum of a file.
    """

    h = hashlib.sha256()

    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)

    return h.hexdigest()


def file_size(path: Path) -> int:
    """
    Return file size in bytes.
    """

    return path.stat().st_size