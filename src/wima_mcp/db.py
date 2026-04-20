"""Thin psycopg wrapper — gives tools a single `with conn()` + audit helper."""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from wima_mcp.config import CONFIG

log = logging.getLogger("wima_mcp.db")


def _connect() -> psycopg.Connection:
    """Open a fresh, autocommit-off connection. Callers manage transactions."""
    return psycopg.connect(
        CONFIG.db_dsn,
        autocommit=False,
        row_factory=dict_row,
        connect_timeout=10,
    )


@contextlib.contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Yield a connection, commit on clean exit, rollback on exception."""
    c = _connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _truncate_json(payload: Any, limit_bytes: int = 2048) -> dict:
    """Truncate a JSON-serialisable value to roughly `limit_bytes` chars."""
    try:
        s = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        s = str(payload)
    if len(s) > limit_bytes:
        return {"_truncated": True, "preview": s[:limit_bytes]}
    try:
        return json.loads(s)
    except ValueError:
        return {"_raw": s[:limit_bytes]}


def audit(
    cur: psycopg.Cursor,
    *,
    tool_name: str,
    tool_category: str,
    task_id: str | None,
    args: dict | None,
    result: dict | None,
    duration_ms: int,
    error: str | None = None,
    error_code: str | None = None,
    request_id: str | None = None,
) -> None:
    """Insert one audit_log row. Swallows failure so audit never masks business errors."""
    try:
        cur.execute(
            """
            INSERT INTO audit_log (
              admin_worker_id, source, tool_name, tool_category,
              task_id, args_summary_json, result_summary_json,
              duration_ms, error, error_code, request_id
            )
            VALUES (%s, 'mcp', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                CONFIG.admin_worker_id,
                tool_name,
                tool_category,
                task_id,
                Jsonb(_truncate_json(args or {})),
                Jsonb(_truncate_json(result or {})),
                duration_ms,
                error,
                error_code,
                request_id or str(uuid.uuid4()),
            ),
        )
    except psycopg.Error as e:  # pragma: no cover
        log.warning("audit_log insert failed for %s: %s", tool_name, e)


class ToolError(Exception):
    """Raised from tool implementations with a canonical error code (DECISIONS §Q.6)."""

    def __init__(self, code: str, message: str = "", details: dict | None = None):
        self.code = code
        self.message = message or code
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")


# ---- Instrumentation helper -------------------------------------------------

@contextlib.contextmanager
def instrument(tool_name: str, category: str):
    """Timer + uniform error path for tools. Use inside every tool body."""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.debug("%s done in %dms", tool_name, elapsed_ms)


def duration_ms_since(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
