"""Runtime configuration — loaded once from environment at import time."""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from dotenv import load_dotenv

# Resolve .env relative to the package root so `python -m wima_mcp` works from
# any working directory (including Claude Desktop's spawn cwd).
_ROOT = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


@dataclass(frozen=True)
class Config:
    db_dsn: str
    admin_worker_id: str
    log_level: str
    supabase_url: str | None
    supabase_service_key: str | None
    documents_bucket: str
    workspace_dir: pathlib.Path

    @classmethod
    def from_env(cls) -> "Config":
        dsn = os.environ.get("WIMA_DB_DSN")
        admin = os.environ.get("WIMA_ADMIN_WORKER_ID")
        if not dsn:
            raise SystemExit("WIMA_DB_DSN is required (see .env.example)")
        if not admin:
            raise SystemExit("WIMA_ADMIN_WORKER_ID is required (see .env.example)")

        workspace = pathlib.Path(
            os.environ.get("WIMA_WORKSPACE_DIR", "~/.wima-cowork/workspace")
        ).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        return cls(
            db_dsn=dsn,
            admin_worker_id=admin,
            log_level=os.environ.get("WIMA_LOG_LEVEL", "INFO").upper(),
            supabase_url=(os.environ.get("WIMA_SUPABASE_URL") or "").rstrip("/") or None,
            supabase_service_key=os.environ.get("WIMA_SUPABASE_SERVICE_KEY") or None,
            documents_bucket=os.environ.get("WIMA_DOCUMENTS_BUCKET", "documents"),
            workspace_dir=workspace,
        )

    def require_storage_creds(self) -> None:
        if not self.supabase_url:
            raise RuntimeError(
                "WIMA_SUPABASE_URL not set — download tools need it "
                "(e.g. https://PROJECT.supabase.co)"
            )
        if not self.supabase_service_key:
            raise RuntimeError(
                "WIMA_SUPABASE_SERVICE_KEY not set — download tools need the "
                "service_role key. Get it from Supabase Studio → Settings → API."
            )


CONFIG = Config.from_env()
