"""HTTP client + fetch-with-cache helper for the observed esimdb.com JSON API."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from . import config, snapshots


class FetchError(RuntimeError):
    pass


def build_client(timeout: float | None = None) -> httpx.Client:
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return httpx.Client(
        headers=headers,
        timeout=timeout or config.TIMEOUT_SECONDS,
        follow_redirects=True,
        http2=False,
    )


def http_get(client: httpx.Client, url: str) -> bytes:
    """GET with bounded retries/backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(config.RETRIES):
        try:
            resp = client.get(url)
            if resp.status_code in config.RETRY_STATUS_CODES:
                last_exc = FetchError(f"{resp.status_code} from {url}")
                time.sleep(config.RETRY_BACKOFF_FACTOR ** attempt)
                continue
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:  # network / status error
            last_exc = exc
            time.sleep(config.RETRY_BACKOFF_FACTOR ** attempt)
    raise FetchError(f"Failed to fetch {url} after {config.RETRIES} attempts: {last_exc}")


def fetch_json(
    client: httpx.Client,
    url: str,
    data_dir: Path,
    use_cache: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """Return (parsed_json, snapshot_manifest). Uses the cache when enabled.

    `snapshot_manifest` always carries the URL, a path, fetch time, and SHA-256
    so a report can cite exactly what it used.
    """
    if use_cache:
        cached = snapshots.read_cache(data_dir, url)
        if cached is not None:
            data, meta = snapshots.load_json(data_dir, url)
            meta.setdefault("source", "cache")
            return data, meta

    body = http_get(client, url)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FetchError(f"Response from {url} was not valid JSON: {exc}") from exc
    manifest = snapshots.write_cache(data_dir, url, body)
    manifest["source"] = "fetched"
    return data, manifest
