"""Supabase Storage helpers — downloads + signed URL generation.

Auth: uses the service_role key from env. The MCP server bypasses RLS
anyway, so this is consistent. Never bundle the service_role key into a
client-shippable artifact.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Iterator

import httpx

from wima_mcp.config import CONFIG

log = logging.getLogger("wima_mcp.storage")


@dataclass(frozen=True)
class DownloadedFile:
    upload_id: str
    original_filename: str
    storage_path: str
    local_path: pathlib.Path
    size_bytes: int
    mime_type: str | None


def _storage_headers() -> dict[str, str]:
    CONFIG.require_storage_creds()
    return {
        "apikey": CONFIG.supabase_service_key,
        "Authorization": f"Bearer {CONFIG.supabase_service_key}",
    }


def _object_url(bucket: str, path: str) -> str:
    CONFIG.require_storage_creds()
    return f"{CONFIG.supabase_url}/storage/v1/object/{bucket}/{path}"


def download_object(
    bucket: str, path: str, dest: pathlib.Path, *, chunk: int = 1 << 15
) -> int:
    """Stream the object at `bucket/path` into `dest`. Returns bytes written."""
    CONFIG.require_storage_creds()
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = _object_url(bucket, path)
    written = 0
    with httpx.stream("GET", url, headers=_storage_headers(), timeout=120) as r:
        if r.status_code != 200:
            body = r.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(
                f"storage GET {path} returned {r.status_code}: {body}"
            )
        with dest.open("wb") as f:
            for block in r.iter_bytes(chunk):
                f.write(block)
                written += len(block)
    return written


def create_signed_url(
    bucket: str, path: str, expires_in: int = 3600
) -> str:
    """Ask Supabase Storage for a time-limited HTTPS URL."""
    CONFIG.require_storage_creds()
    r = httpx.post(
        f"{CONFIG.supabase_url}/storage/v1/object/sign/{bucket}/{path}",
        headers={**_storage_headers(), "Content-Type": "application/json"},
        json={"expiresIn": int(expires_in)},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"sign url {path}: {r.status_code} {r.text[:300]}")
    body = r.json()
    signed_path = body.get("signedURL") or body.get("signedUrl")
    if not signed_path:
        raise RuntimeError(f"sign url missing signedURL in response: {body}")
    if signed_path.startswith("http"):
        return signed_path
    # API returns "/object/sign/documents/<path>?token=..."
    return f"{CONFIG.supabase_url}/storage/v1{signed_path}"


def local_path_for(project_id: str, filename: str) -> pathlib.Path:
    """Where the downloader drops files — mirrors storage layout."""
    safe_project = str(project_id).replace("/", "_").replace("..", "_")
    safe_name = filename.replace("/", "_").replace("..", "_")
    return CONFIG.workspace_dir / safe_project / safe_name


def iter_workspace_files() -> Iterator[pathlib.Path]:
    """Walk the local workspace for the list_workspace_files tool."""
    if not CONFIG.workspace_dir.exists():
        return iter(())
    return (p for p in CONFIG.workspace_dir.rglob("*") if p.is_file())
