"""Filesystem helpers shared by the Confluence connector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_filename(value: str, *, fallback: str = "attachment") -> str:
    name = Path(value).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "-", name).strip(" .-")
    return name or fallback


def page_body(page: dict[str, Any], body_format: str = "storage") -> str:
    body = page.get("body", {}).get(body_format, {})
    return str(body.get("value", "")) if isinstance(body, dict) else ""
