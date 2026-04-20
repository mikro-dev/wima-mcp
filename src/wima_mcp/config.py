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

    @classmethod
    def from_env(cls) -> "Config":
        dsn = os.environ.get("WIMA_DB_DSN")
        admin = os.environ.get("WIMA_ADMIN_WORKER_ID")
        if not dsn:
            raise SystemExit("WIMA_DB_DSN is required (see .env.example)")
        if not admin:
            raise SystemExit("WIMA_ADMIN_WORKER_ID is required (see .env.example)")
        return cls(
            db_dsn=dsn,
            admin_worker_id=admin,
            log_level=os.environ.get("WIMA_LOG_LEVEL", "INFO").upper(),
        )


CONFIG = Config.from_env()
