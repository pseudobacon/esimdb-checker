"""Raw-response snapshot cache for reproducibility.

Each fetch is stored as the exact response bytes plus a small manifest (URL,
fetch time, SHA-256, size). Reports reference the snapshots they used, so a
recommendation can be reproduced offline from the captured data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def snapshot_paths(data_dir: Path, url: str) -> tuple[Path, Path]:
    key = url_key(url)
    return data_dir / f"{key}.json", data_dir / f"{key}.meta.json"


def read_cache(data_dir: Path, url: str) -> bytes | None:
    body_path, _ = snapshot_paths(data_dir, url)
    if body_path.exists():
        return body_path.read_bytes()
    return None


def write_cache(data_dir: Path, url: str, body: bytes) -> dict[str, Any]:
    body_path, meta_path = snapshot_paths(data_dir, url)
    data_dir.mkdir(parents=True, exist_ok=True)
    body_path.write_bytes(body)
    meta = {
        "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"url": url, "body": str(body_path), "meta": str(meta_path), **meta}


def load_json(data_dir: Path, url: str) -> tuple[Any, dict[str, Any]]:
    """Load a cached snapshot's parsed JSON + its manifest."""
    body_path, meta_path = snapshot_paths(data_dir, url)
    data = json.loads(body_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return data, meta
