"""Wima MCP server — stdio, 19 Blok 1 tools per DECISIONS §H.4.

Run as:
    python -m wima_mcp
or via the installed entry point:
    wima-mcp
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb
from pydantic import Field

from wima_mcp.config import CONFIG
from wima_mcp.db import ToolError, audit, conn, duration_ms_since

logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wima_mcp")

mcp = FastMCP(name="wima", instructions=(
    "Wima v2 Cowork tool layer. Use these tools to triage inbox tasks, research, "
    "draft artifacts, and deliver them to clients. Every write is audited. "
    "Credits are debited only on deliver_artifact (Pay-on-Delivery)."
))


# -----------------------------------------------------------------------------
# 1. Context / Read (6)
# -----------------------------------------------------------------------------

@mcp.tool()
def list_pending_tasks(
    status: Annotated[Optional[list[str]], Field(description="task_status filter; default ['pending','in_progress','awaiting_client']")] = None,
    tier: Annotated[Optional[list[str]], Field(description="task_tier filter, e.g. ['urgent','standard']")] = None,
    matter_type: Annotated[Optional[list[str]], Field(description="project_matter_type filter")] = None,
    has_pending_cta: Annotated[Optional[bool], Field(description="only tasks with a pending CTA")] = None,
    client_id: Annotated[Optional[str], Field(description="filter by client uuid")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """List tasks needing admin attention — DECISIONS §H.4 #1, L4.F1 shape lock."""
    t0 = time.perf_counter()
    statuses = status or ["pending", "in_progress", "awaiting_client"]
    sql = [
        "SELECT t.id, t.title, t.status, t.tier, t.priority, t.pinned_pipeline_stage,",
        "       t.created_at, t.updated_at,",
        "       p.id AS project_id, p.name AS project_name, p.matter_type,",
        "       c.id AS client_id, c.name AS client_name, c.balance,",
        "       (SELECT count(*) FROM cta WHERE task_id = t.id AND status = 'pending') AS pending_cta_count",
        "  FROM task t JOIN project p ON p.id = t.project_id",
        "  JOIN client c ON c.id = p.client_id",
        " WHERE t.status = ANY(%s)",
    ]
    params: list[Any] = [statuses]
    if tier:
        sql.append(" AND t.tier = ANY(%s)"); params.append(tier)
    if matter_type:
        sql.append(" AND p.matter_type = ANY(%s)"); params.append(matter_type)
    if client_id:
        sql.append(" AND c.id = %s"); params.append(client_id)
    if has_pending_cta is not None:
        cmp = ">" if has_pending_cta else "="
        sql.append(f" AND (SELECT count(*) FROM cta WHERE task_id = t.id AND status = 'pending') {cmp} 0")
    sql.append(" ORDER BY t.created_at DESC LIMIT %s")
    params.append(limit)

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("".join(sql), params)
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="list_pending_tasks", tool_category="read",
                  task_id=None, args={"statuses": statuses, "tier": tier, "limit": limit},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Full task tree — DECISIONS §K.3.4 shape."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT t.*, row_to_json(p) AS project, row_to_json(c) AS client
                  FROM task t
                  JOIN project p ON p.id = t.project_id
                  JOIN client c ON c.id = p.client_id
                 WHERE t.id = %s
            """, (task_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "get_task", "read", task_id, {"task_id": task_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "task not found")

            cur.execute("SELECT * FROM chat_message WHERE task_id = %s ORDER BY created_at ASC LIMIT 100", (task_id,))
            chat = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM cta WHERE task_id = %s ORDER BY created_at DESC", (task_id,))
            ctas = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM draft WHERE task_id = %s ORDER BY artifact_type, version DESC", (task_id,))
            drafts = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM artifact WHERE task_id = %s ORDER BY delivered_at DESC NULLS LAST", (task_id,))
            artifacts = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM upload WHERE task_id = %s ORDER BY created_at DESC", (task_id,))
            uploads = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM pipeline_event WHERE task_id = %s ORDER BY created_at DESC LIMIT 20", (task_id,))
            pipeline = [_jsonable(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM internal_note WHERE task_id = %s ORDER BY created_at DESC", (task_id,))
            notes = [_jsonable(r) for r in cur.fetchall()]

            audit(cur, tool_name="get_task", tool_category="read",
                  task_id=task_id, args={"task_id": task_id},
                  result={"has_data": True}, duration_ms=duration_ms_since(t0))

    task = _jsonable(row)
    return {
        "task": task,
        "chat_messages": chat,
        "ctas": ctas,
        "drafts": drafts,
        "artifacts": artifacts,
        "uploads": uploads,
        "pipeline_events": pipeline,
        "internal_notes": notes,
    }


@mcp.tool()
def get_client_profile(client_id: str, limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
    """Client summary + past_projects — DECISIONS §R3.L3 shape."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, email, sector, balance, total_credits_purchased,
                       preferences_json, created_at
                  FROM client WHERE id = %s AND deleted_at IS NULL
            """, (client_id,))
            client = cur.fetchone()
            if not client:
                _audit_error(cur, "get_client_profile", "read", None, {"client_id": client_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "client not found")
            cur.execute("""
                SELECT p.id AS project_id, p.name, p.matter_type, p.tags,
                       CASE WHEN p.archived_at IS NULL THEN 'active' ELSE 'archived' END AS status,
                       (SELECT count(*) FROM task t WHERE t.project_id = p.id) AS task_count,
                       (SELECT count(*) FROM task t WHERE t.project_id = p.id AND t.status = 'delivered') AS delivered_task_count,
                       (SELECT max(t.updated_at) FROM task t WHERE t.project_id = p.id) AS last_activity_at
                  FROM project p
                 WHERE p.client_id = %s
                 ORDER BY last_activity_at DESC NULLS LAST
                 LIMIT %s
            """, (client_id, limit))
            past = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="get_client_profile", tool_category="read",
                  task_id=None, args={"client_id": client_id},
                  result={"past_project_count": len(past)}, duration_ms=duration_ms_since(t0))
    return {"client": _jsonable(client), "past_projects": past}


@mcp.tool()
def read_upload(upload_id: str) -> dict:
    """Upload metadata + OCR text — DECISIONS §K.3.5.

    Returns VALIDATION_ERROR with details.reason='ocr_not_ready' when OCR hasn't run yet.
    """
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM upload WHERE id = %s", (upload_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "read_upload", "read", None, {"upload_id": upload_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "upload not found")
            if row["ocr_processed_at"] is None:
                _audit_error(cur, "read_upload", "read", row["task_id"],
                             {"upload_id": upload_id}, "VALIDATION_ERROR", t0)
                raise ToolError("VALIDATION_ERROR", "OCR not ready", {"reason": "ocr_not_ready"})
            audit(cur, tool_name="read_upload", tool_category="read",
                  task_id=row["task_id"], args={"upload_id": upload_id},
                  result={"size": row["size_bytes"]}, duration_ms=duration_ms_since(t0))
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]) if row["task_id"] else None,
        "project_id": str(row["project_id"]),
        "original_filename": row["original_filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "ocr_content": row["ocr_content"],
        "ocr_processed_at": row["ocr_processed_at"].isoformat() if row["ocr_processed_at"] else None,
        "storage_path": row["storage_path"],
    }


@mcp.tool()
def list_artifacts(task_id: str) -> dict:
    """Metadata-only artifact refs — body fetched via get_artifact."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, task_id, artifact_type, client_title, delivered_at,
                       review_status, length(coalesce(client_note,'')) AS client_note_size
                  FROM artifact WHERE task_id = %s
                  ORDER BY delivered_at DESC NULLS LAST
            """, (task_id,))
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="list_artifacts", tool_category="read",
                  task_id=task_id, args={"task_id": task_id},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


@mcp.tool()
def get_artifact(artifact_id: str) -> dict:
    """Artifact incl. source_draft body_markdown — DECISIONS §L4.F2."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT a.*, d.body_markdown AS source_draft_body_markdown,
                       d.version AS source_draft_version,
                       d.title AS source_draft_title
                  FROM artifact a JOIN draft d ON d.id = a.source_draft_id
                 WHERE a.id = %s
            """, (artifact_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "get_artifact", "read", None, {"artifact_id": artifact_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND")
            audit(cur, tool_name="get_artifact", tool_category="read",
                  task_id=row["task_id"], args={"artifact_id": artifact_id},
                  result={"artifact_type": row["artifact_type"]},
                  duration_ms=duration_ms_since(t0))
    return _jsonable(row)


# -----------------------------------------------------------------------------
# 2. Knowledge (4)
# -----------------------------------------------------------------------------

@mcp.tool()
def search_regulations(
    query: str,
    status: Annotated[list[str], Field(description="default ['active']")] = None,
    jenis: Annotated[Optional[list[str]], Field(description="e.g. ['UU','PP']")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """FTS-only search over regulation (pgvector hybrid arrives when embeddings are seeded)."""
    t0 = time.perf_counter()
    statuses = status or ["active"]
    sql = [
        "SELECT r.id, r.jenis, r.nomor, r.tahun, r.judul, r.tags, r.status, r.source_url,",
        "       ts_rank_cd(to_tsvector('simple', r.judul), plainto_tsquery('simple', %s)) AS rank",
        "  FROM regulation r",
        " WHERE r.status = ANY(%s)",
        "   AND (to_tsvector('simple', r.judul || ' ' || coalesce(array_to_string(r.tags,' '), ''))",
        "        @@ plainto_tsquery('simple', %s))",
    ]
    params: list[Any] = [query, statuses, query]
    if jenis:
        sql.append(" AND r.jenis::text = ANY(%s)"); params.append(jenis)
    sql.append(" ORDER BY rank DESC LIMIT %s"); params.append(limit)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("".join(sql), params)
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="search_regulations", tool_category="read",
                  task_id=None, args={"query": query, "limit": limit},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


@mcp.tool()
def get_regulation(
    regulation_id: str,
    include_full_text: bool = False,
    top_n_chunks: Annotated[int, Field(ge=1, le=50)] = 5,
) -> dict:
    """Regulation detail — capped at 200 chunks (DECISIONS §R3.L4)."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM regulation WHERE id = %s", (regulation_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "get_regulation", "read", None, {"regulation_id": regulation_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND")

            if include_full_text:
                cur.execute("SELECT count(*) AS n FROM regulation_chunk WHERE regulation_id = %s", (regulation_id,))
                total = cur.fetchone()["n"]
                if total > 200:
                    _audit_error(cur, "get_regulation", "read", None,
                                 {"regulation_id": regulation_id, "chunk_count": total},
                                 "VALIDATION_ERROR", t0)
                    raise ToolError("VALIDATION_ERROR",
                                    f"regulation has {total} chunks (cap 200); paginate via search_regulations",
                                    {"chunk_count": total})
                cur.execute("""
                  SELECT id, chunk_type, bab, pasal, ayat, content, position, char_count
                    FROM regulation_chunk WHERE regulation_id = %s ORDER BY position
                """, (regulation_id,))
            else:
                cur.execute("""
                  SELECT id, chunk_type, bab, pasal, ayat, content, position, char_count
                    FROM regulation_chunk WHERE regulation_id = %s ORDER BY position LIMIT %s
                """, (regulation_id, top_n_chunks))
            chunks = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="get_regulation", tool_category="read",
                  task_id=None, args={"regulation_id": regulation_id, "full": include_full_text},
                  result={"chunk_count": len(chunks)}, duration_ms=duration_ms_since(t0))
    return {"regulation": _jsonable(row), "chunks": chunks}


@mcp.tool()
def search_precedents(
    query: str,
    matter_type: Annotated[Optional[list[str]], Field(description="filter by matter_type enum")] = None,
    limit: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict:
    """FTS over curated precedent KB. Response includes `source_artifact_id` for hopping."""
    t0 = time.perf_counter()
    sql = [
        "SELECT id, source_artifact_id, title, summary, matter_type, tags, created_at,",
        "       ts_rank_cd(to_tsvector('simple', title || ' ' || summary), plainto_tsquery('simple', %s)) AS rank",
        "  FROM precedent WHERE (to_tsvector('simple', title || ' ' || summary || ' ' || coalesce(body,''))",
        "        @@ plainto_tsquery('simple', %s))",
    ]
    params: list[Any] = [query, query]
    if matter_type:
        sql.append(" AND matter_type::text = ANY(%s)"); params.append(matter_type)
    sql.append(" ORDER BY rank DESC LIMIT %s"); params.append(limit)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("".join(sql), params)
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="search_precedents", tool_category="read",
                  task_id=None, args={"query": query, "limit": limit},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


@mcp.tool()
def get_client_knowledge(
    client_id: str,
    query: Annotated[Optional[str], Field(description="optional text match; otherwise returns latest facts")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict:
    """Client memory facts (preference / pattern / precedent_ref / context)."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            if query:
                cur.execute("""
                  SELECT *, ts_rank_cd(to_tsvector('simple', title || ' ' || content),
                                       plainto_tsquery('simple', %s)) AS rank
                    FROM client_knowledge
                   WHERE client_id = %s
                     AND (to_tsvector('simple', title || ' ' || content) @@ plainto_tsquery('simple', %s))
                   ORDER BY rank DESC LIMIT %s
                """, (query, client_id, query, limit))
            else:
                cur.execute("SELECT * FROM client_knowledge WHERE client_id = %s ORDER BY created_at DESC LIMIT %s",
                            (client_id, limit))
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="get_client_knowledge", tool_category="read",
                  task_id=None, args={"client_id": client_id, "query": query},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


# -----------------------------------------------------------------------------
# 3. Intake / Classification (2)
# -----------------------------------------------------------------------------

@mcp.tool()
def send_cta(
    task_id: str,
    tier: Annotated[str, Field(pattern="^(standard|urgent|bulk)$")],
    credit: Annotated[int, Field(ge=0)],
    eta_text: Annotated[Optional[str], Field(description="free-form ETA string shown to client")] = None,
    reasoning_note: Annotated[Optional[str], Field(description="why this tier+credit")] = None,
) -> dict:
    """Propose a CTA, schedule 15-min auto-confirm job. DECISIONS §K.2.1 transaction."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            # Guard: no active pending CTA (H4 / DECISIONS §K.2.4)
            cur.execute("SELECT count(*) AS n FROM cta WHERE task_id = %s::uuid AND status = 'pending'::cta_status", (task_id,))
            if cur.fetchone()["n"] > 0:
                _audit_error(cur, "send_cta", "write", task_id, {"task_id": task_id}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR", "Task already has a pending CTA")

            cur.execute("""
              INSERT INTO scheduled_job (job_type, payload_json, run_at)
                   VALUES ('auto_confirm_cta'::scheduled_job_type, %s::jsonb, now() + interval '15 minutes')
                RETURNING id
            """, (Jsonb({"credit_snapshot": credit, "tier": tier}),))
            job_id = cur.fetchone()["id"]

            cur.execute("""
              INSERT INTO cta (task_id, tier, credit, eta_text, reasoning_note,
                               status, created_by_admin_id, auto_confirm_job_id)
                   VALUES (%s::uuid, %s::task_tier, %s::int, %s::text, %s::text,
                           'pending'::cta_status, %s::uuid, %s::uuid)
                RETURNING id, created_at
            """, (task_id, tier, credit, eta_text, reasoning_note, CONFIG.admin_worker_id, job_id))
            cta = cur.fetchone()

            cur.execute("""
              UPDATE scheduled_job
                 SET payload_json = payload_json || jsonb_build_object('cta_id', %s::text)
               WHERE id = %s::uuid
            """, (str(cta["id"]), str(job_id)))

            cta_body = f"CTA {tier.upper()} · {credit} credit"
            if eta_text:
                cta_body += f" · {eta_text}"
            cur.execute("""
              INSERT INTO chat_message (task_id, sender_type, sender_admin_id,
                                        message_type, body, cta_id)
                   VALUES (%s::uuid, 'admin'::sender_type, %s::uuid,
                           'cta_prompt'::chat_message_type, %s::text, %s::uuid)
            """, (task_id, CONFIG.admin_worker_id, cta_body, cta["id"]))

            pipeline_summary = f"CTA {tier} · {credit} cr proposed"
            cur.execute("""
              INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                   VALUES (%s::uuid, %s::uuid, 'classify'::pipeline_stage,
                           'send_cta'::text, %s::text)
            """, (task_id, CONFIG.admin_worker_id, pipeline_summary))

            cur.execute("UPDATE task SET status = 'awaiting_client'::task_status, updated_at = now() WHERE id = %s::uuid",
                        (task_id,))

            audit(cur, tool_name="send_cta", tool_category="write",
                  task_id=task_id, args={"tier": tier, "credit": credit},
                  result={"cta_id": str(cta["id"])}, duration_ms=duration_ms_since(t0))

    return {"cta_id": str(cta["id"]), "auto_confirm_job_id": str(job_id), "status": "pending"}


@mcp.tool()
def post_chat_message(
    task_id: str,
    body: str,
    message_type: Annotated[str, Field(pattern="^(standard|pipeline_update|system)$")] = "standard",
) -> dict:
    """Admin chat message. Non-CTA, non-artifact. No pipeline_event side-effect."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
              INSERT INTO chat_message (task_id, sender_type, sender_admin_id, message_type, body)
                   VALUES (%s, 'admin', %s, %s::chat_message_type, %s)
                RETURNING id, created_at
            """, (task_id, CONFIG.admin_worker_id, message_type, body))
            row = cur.fetchone()
            audit(cur, tool_name="post_chat_message", tool_category="write",
                  task_id=task_id, args={"type": message_type, "body_len": len(body)},
                  result={"message_id": str(row["id"])}, duration_ms=duration_ms_since(t0))
    return {"message_id": str(row["id"]), "created_at": row["created_at"].isoformat()}


# -----------------------------------------------------------------------------
# 4. Draft & Delivery (3)
# -----------------------------------------------------------------------------

@mcp.tool()
def save_draft(
    task_id: str,
    artifact_type: Annotated[str, Field(pattern="^(opinion|memo|draft_document|action_list|triage_summary)$")],
    title: str,
    body_markdown: str,
    source_refs: Annotated[Optional[list[dict]], Field(description="[{type:'regulation', regulation_id, article}, ...]")] = None,
    notes: Optional[str] = None,
) -> dict:
    """Create a new draft version (append-only). Uses advisory lock per DECISIONS §L3.F3."""
    return _insert_draft(task_id, artifact_type, title, body_markdown, source_refs, notes,
                         parent_draft_id=None, tool_name="save_draft")


@mcp.tool()
def update_draft(
    draft_id: str,
    body_markdown: Optional[str] = None,
    title: Optional[str] = None,
    source_refs: Optional[list[dict]] = None,
    notes: Optional[str] = None,
) -> dict:
    """Fork a new draft version from an existing one (append-only, version+1)."""
    if not any([body_markdown, title, source_refs, notes]):
        raise ToolError("VALIDATION_ERROR", "at least one mutable field is required")
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM draft WHERE id = %s", (draft_id,))
            parent = cur.fetchone()
            if not parent:
                _audit_error(cur, "update_draft", "write", None, {"draft_id": draft_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND")
    return _insert_draft(
        task_id=str(parent["task_id"]),
        artifact_type=parent["artifact_type"],
        title=title if title is not None else parent["title"],
        body_markdown=body_markdown if body_markdown is not None else parent["body_markdown"],
        source_refs=source_refs if source_refs is not None else parent["source_refs_json"],
        notes=notes if notes is not None else parent["notes"],
        parent_draft_id=str(parent["id"]),
        tool_name="update_draft",
    )


def _insert_draft(task_id, artifact_type, title, body_markdown, source_refs, notes,
                  parent_draft_id, tool_name):
    import hashlib
    t0 = time.perf_counter()
    lock_key = int(hashlib.md5(f"draft:{task_id}:{artifact_type}".encode()).hexdigest()[:15], 16)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute("""
              SELECT coalesce(max(version), 0) + 1 AS v
                FROM draft WHERE task_id = %s AND artifact_type = %s::artifact_type
            """, (task_id, artifact_type))
            version = cur.fetchone()["v"]
            cur.execute("""
              INSERT INTO draft (task_id, artifact_type, version, title, body_markdown,
                                 source_refs_json, created_by_admin_id, parent_draft_id, notes)
                   VALUES (%s, %s::artifact_type, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, version, created_at
            """, (task_id, artifact_type, version, title, body_markdown,
                  Jsonb(source_refs or []), CONFIG.admin_worker_id, parent_draft_id, notes))
            row = cur.fetchone()
            stage = "review" if tool_name == "update_draft" else "draft"
            cur.execute("""
              INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                   VALUES (%s, %s, %s::pipeline_stage, %s, %s)
            """, (task_id, CONFIG.admin_worker_id, stage, tool_name,
                  f"{artifact_type} v{version} {'revised' if parent_draft_id else 'drafted'}"))
            audit(cur, tool_name=tool_name, tool_category="write",
                  task_id=task_id, args={"artifact_type": artifact_type, "version": version},
                  result={"draft_id": str(row["id"])}, duration_ms=duration_ms_since(t0))
    return {"draft_id": str(row["id"]), "version": version, "artifact_type": artifact_type}


@mcp.tool()
def deliver_artifact(
    draft_id: str,
    client_title: str,
    credit_deduct: Annotated[int, Field(ge=0)],
    client_note: Optional[str] = None,
) -> dict:
    """Commit a draft as a delivered artifact. One atomic transaction — DECISIONS §K.2.2.

    Side-effects:
      1. UPDATE/INSERT artifact row (delivered_at, review_status='delivered')
      2. INSERT credit_transaction (direction='debit', reason='delivery') — trigger updates client.balance
      3. INSERT chat_message (message_type='artifact_delivery')
      4. UPDATE task.status='delivered'
      5. emit pipeline_event stage='delivered'
      6. INSERT audit_log
    """
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            # Load + lock client + validate state
            cur.execute("""
                SELECT d.id AS draft_id, d.task_id, d.artifact_type::text AS artifact_type,
                       t.status AS task_status, t.project_id,
                       p.client_id, c.balance, c.deleted_at, c.name AS client_name
                  FROM draft d
                  JOIN task t ON t.id = d.task_id
                  JOIN project p ON p.id = t.project_id
                  JOIN client c ON c.id = p.client_id
                 WHERE d.id = %s
                 FOR UPDATE OF c
            """, (draft_id,))
            base = cur.fetchone()
            if not base:
                _audit_error(cur, "deliver_artifact", "write", None, {"draft_id": draft_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "draft not found")
            if base["deleted_at"] is not None:
                _audit_error(cur, "deliver_artifact", "write", base["task_id"],
                             {"draft_id": draft_id}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR", "client soft-deleted")
            if base["task_status"] == "delivered":
                _audit_error(cur, "deliver_artifact", "write", base["task_id"],
                             {"draft_id": draft_id}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR", "task already delivered")
            if base["balance"] < credit_deduct:
                _audit_error(cur, "deliver_artifact", "write", base["task_id"],
                             {"draft_id": draft_id, "balance": base["balance"]},
                             "INSUFFICIENT_BALANCE", t0)
                raise ToolError("INSUFFICIENT_BALANCE",
                                f"balance {base['balance']} < required {credit_deduct}")

            # Most-recent accepted / auto-confirmed CTA
            cur.execute("""
                SELECT id, credit FROM cta
                 WHERE task_id = %s AND status IN ('accepted','auto_confirmed')
                 ORDER BY responded_at DESC NULLS LAST, created_at DESC LIMIT 1
            """, (base["task_id"],))
            cta = cur.fetchone()
            if not cta:
                _audit_error(cur, "deliver_artifact", "write", base["task_id"],
                             {"draft_id": draft_id}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR", "no accepted CTA for task", {"reason": "no_cta"})
            if cta["credit"] != credit_deduct:
                _audit_error(cur, "deliver_artifact", "write", base["task_id"],
                             {"cta_credit": cta["credit"], "requested": credit_deduct},
                             "VALIDATION_ERROR", t0)
                raise ToolError("VALIDATION_ERROR",
                                f"credit_deduct {credit_deduct} != CTA agreed {cta['credit']}")

            new_balance = base["balance"] - credit_deduct

            # Upsert artifact row
            cur.execute("SELECT id FROM artifact WHERE source_draft_id = %s", (draft_id,))
            existing = cur.fetchone()
            if existing:
                artifact_id = existing["id"]
                cur.execute("""
                    UPDATE artifact
                       SET client_title = %s, client_note = %s, review_status = 'delivered',
                           delivered_at = now(), delivered_by_admin_id = %s
                     WHERE id = %s
                """, (client_title, client_note, CONFIG.admin_worker_id, artifact_id))
            else:
                cur.execute("""
                    INSERT INTO artifact (task_id, source_draft_id, artifact_type,
                                          delivered_by_admin_id, client_title, client_note,
                                          delivered_at, review_status, created_by_admin_id)
                         VALUES (%s, %s, %s::artifact_type, %s, %s, %s, now(),
                                 'delivered', %s)
                      RETURNING id
                """, (base["task_id"], draft_id, base["artifact_type"],
                      CONFIG.admin_worker_id, client_title, client_note, CONFIG.admin_worker_id))
                artifact_id = cur.fetchone()["id"]

            # Credit debit — trigger apply_credit_tx syncs client.balance
            cur.execute("""
                INSERT INTO credit_transaction (client_id, task_id, cta_id,
                                                direction, reason, amount,
                                                balance_after, performed_by_admin_id, notes)
                     VALUES (%s, %s, %s, 'debit', 'delivery', %s, %s, %s,
                             'deliver_artifact')
                  RETURNING id
            """, (base["client_id"], base["task_id"], cta["id"], credit_deduct,
                  new_balance, CONFIG.admin_worker_id))
            ct_id = cur.fetchone()["id"]

            cur.execute("""
                INSERT INTO chat_message (task_id, sender_type, sender_admin_id,
                                          message_type, body, artifact_id)
                     VALUES (%s, 'admin', %s, 'artifact_delivery', %s, %s)
            """, (base["task_id"], CONFIG.admin_worker_id,
                  client_note or client_title, artifact_id))

            cur.execute("UPDATE task SET status = 'delivered', updated_at = now() WHERE id = %s",
                        (base["task_id"],))

            pipeline_summary = f'Artifact "{client_title}" delivered to {base["client_name"]}'
            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage,
                                            source_tool, summary)
                     VALUES (%s, %s, 'delivered', 'deliver_artifact', %s)
            """, (base["task_id"], CONFIG.admin_worker_id, pipeline_summary))

            audit(cur, tool_name="deliver_artifact", tool_category="write",
                  task_id=base["task_id"],
                  args={"draft_id": draft_id, "credit_deduct": credit_deduct},
                  result={"artifact_id": str(artifact_id), "new_balance": new_balance,
                          "credit_transaction_id": str(ct_id)},
                  duration_ms=duration_ms_since(t0))

    return {
        "artifact_id": str(artifact_id),
        "task_id": str(base["task_id"]),
        "credit_transaction_id": str(ct_id),
        "new_balance": new_balance,
    }


# -----------------------------------------------------------------------------
# 5. State / Pipeline (4)
# -----------------------------------------------------------------------------

@mcp.tool()
def log_pipeline_event(
    task_id: str,
    summary: str,
    stage_label: Annotated[str, Field(description="Required since only stage='custom' is allowed")],
    payload_json: Optional[dict] = None,
) -> dict:
    """Emit a custom pipeline stage — DECISIONS §H.4, only stage='custom' allowed via this tool."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage, stage_label,
                                            source_tool, summary, payload_json)
                     VALUES (%s, %s, 'custom', %s, 'log_pipeline_event', %s, %s)
                  RETURNING id, created_at
            """, (task_id, CONFIG.admin_worker_id, stage_label, summary,
                  Jsonb(payload_json or {})))
            row = cur.fetchone()
            audit(cur, tool_name="log_pipeline_event", tool_category="write",
                  task_id=task_id, args={"stage_label": stage_label},
                  result={"event_id": str(row["id"])},
                  duration_ms=duration_ms_since(t0))
    return {"event_id": str(row["id"]), "created_at": row["created_at"].isoformat()}


@mcp.tool()
def add_internal_note(task_id: str, body: str) -> dict:
    """Task-scoped internal note, invisible to client."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO internal_note (task_id, admin_worker_id, body)
                     VALUES (%s, %s, %s) RETURNING id, created_at
            """, (task_id, CONFIG.admin_worker_id, body))
            row = cur.fetchone()
            audit(cur, tool_name="add_internal_note", tool_category="write",
                  task_id=task_id, args={"body_len": len(body)},
                  result={"note_id": str(row["id"])},
                  duration_ms=duration_ms_since(t0))
    return {"note_id": str(row["id"]), "created_at": row["created_at"].isoformat()}


@mcp.tool()
def claim_task(task_id: str) -> dict:
    """Open a cowork_session for task. Idempotent for same admin (re-returns active session)."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, admin_worker_id FROM cowork_session
                 WHERE task_id = %s AND ended_at IS NULL
            """, (task_id,))
            existing = cur.fetchone()
            if existing:
                if str(existing["admin_worker_id"]) == CONFIG.admin_worker_id:
                    audit(cur, tool_name="claim_task", tool_category="write",
                          task_id=task_id, args={"task_id": task_id},
                          result={"session_id": str(existing["id"]), "reused": True},
                          duration_ms=duration_ms_since(t0))
                    return {"session_id": str(existing["id"]), "reused": True}
                _audit_error(cur, "claim_task", "write", task_id,
                             {"task_id": task_id}, "CONFLICT", t0)
                raise ToolError("CONFLICT",
                                "task already claimed by a different admin",
                                {"held_by_admin_id": str(existing["admin_worker_id"])})

            cur.execute("""
                INSERT INTO cowork_session (task_id, admin_worker_id, session_type)
                     VALUES (%s, %s, 'normal') RETURNING id, started_at
            """, (task_id, CONFIG.admin_worker_id))
            row = cur.fetchone()
            cur.execute("""
                UPDATE task SET current_session_id = %s,
                                status = CASE WHEN status = 'pending' THEN 'in_progress'::task_status
                                              WHEN status = 'awaiting_client' THEN 'in_progress'::task_status
                                              ELSE status END,
                                updated_at = now()
                 WHERE id = %s
            """, (row["id"], task_id))
            audit(cur, tool_name="claim_task", tool_category="write",
                  task_id=task_id, args={"task_id": task_id},
                  result={"session_id": str(row["id"])},
                  duration_ms=duration_ms_since(t0))
    return {"session_id": str(row["id"]), "started_at": row["started_at"].isoformat(), "reused": False}


@mcp.tool()
def release_task(task_id: str) -> dict:
    """Close the active cowork_session. Idempotent."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE cowork_session SET ended_at = now()
                 WHERE task_id = %s AND admin_worker_id = %s AND ended_at IS NULL
             RETURNING id
            """, (task_id, CONFIG.admin_worker_id))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE task SET current_session_id = NULL, updated_at = now() WHERE id = %s",
                            (task_id,))
            audit(cur, tool_name="release_task", tool_category="write",
                  task_id=task_id, args={"task_id": task_id},
                  result={"released": bool(row)},
                  duration_ms=duration_ms_since(t0))
    return {"released": bool(row)}


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------

def _jsonable(row: dict) -> dict:
    """Convert uuid / datetime / Decimal to JSON-safe scalars."""
    out: dict = {}
    for k, v in row.items():
        if v is None:
            out[k] = None
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (bytes, bytearray)):
            out[k] = v.decode("utf-8", errors="replace")
        elif hasattr(v, "hex"):  # UUID
            try:
                out[k] = str(v)
            except Exception:
                out[k] = repr(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [
                str(x) if hasattr(x, "hex") and not isinstance(x, (bytes, bytearray)) else x
                for x in v
            ]
        else:
            out[k] = v
    return out


def _audit_error(cur, tool_name, category, task_id, args, code, t0):
    audit(cur, tool_name=tool_name, tool_category=category, task_id=task_id,
          args=args, result={"error": code}, duration_ms=duration_ms_since(t0),
          error=code, error_code=code)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    log.info("wima-mcp starting — admin_worker_id=%s", CONFIG.admin_worker_id)
    mcp.run()


if __name__ == "__main__":
    main()
