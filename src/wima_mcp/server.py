"""Wima MCP server — stdio. Implements the clean-slate QA-gate flow per
`update.md`:

    pending → cowork_working → pending_admin_review →
             [approve_and_deliver → delivered]
             [reject_with_revision → revision_needed → cowork_working]

Admin tools (4): approve_and_deliver, reject_with_revision, get_task,
list_pending_tasks.
Cowork tools (6): claim_task, save_draft, update_draft, submit_for_review,
add_internal_note, release_task.
Plus read/storage helpers kept from the prior surface (uploads, regulations,
precedents, client profile/knowledge).

All writes are audited via audit_log.

Run as: `python -m wima_mcp` or `wima-mcp` entry point.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb
from pydantic import Field

from wima_mcp.config import CONFIG
from wima_mcp.db import ToolError, audit, conn, duration_ms_since
from wima_mcp import storage as wima_storage

logging.basicConfig(
    level=CONFIG.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wima_mcp")

mcp = FastMCP(name="wima", instructions=(
    "Wima review-flow tool layer. Cowork agents claim tasks, draft artifacts, "
    "and submit for admin review. Admins approve-and-deliver in one action or "
    "reject with structured revision notes. No billing, no CTAs."
))


# =============================================================================
# Read tools
# =============================================================================

@mcp.tool()
def list_pending_tasks(
    status: Annotated[
        Optional[list[str]],
        Field(description="task.status filter; default ['pending_admin_review']")
    ] = None,
    client_id: Annotated[Optional[str], Field(description="filter by client uuid")] = None,
    matter_type: Annotated[Optional[list[str]], Field(description="project.matter_type filter")] = None,
    claimed_by: Annotated[Optional[str], Field(description="filter by agent uuid that claimed the task")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict:
    """Admin queue. Default sort: oldest submission first (FIFO), so the item
    waiting longest rises to the top."""
    t0 = time.perf_counter()
    statuses = status or ["pending_admin_review"]
    sql = [
        "SELECT t.id, t.title, t.status, t.priority, t.revision_count,",
        "       t.claimed_by_admin_id, t.claimed_at, t.delivered_at,",
        "       t.created_at, t.updated_at,",
        "       rs.id AS review_submission_id, rs.submitted_at, rs.summary_for_admin,",
        "       p.id AS project_id, p.name AS project_name, p.matter_type,",
        "       c.id AS client_id, c.name AS client_name",
        "  FROM task t",
        "  JOIN project p ON p.id = t.project_id",
        "  JOIN client c ON c.id = p.client_id",
        "  LEFT JOIN review_submission rs",
        "       ON rs.id = t.review_submission_id AND rs.resolved_at IS NULL",
        " WHERE t.status = ANY(%s)",
    ]
    params: list[Any] = [statuses]
    if matter_type:
        sql.append(" AND p.matter_type = ANY(%s)"); params.append(matter_type)
    if client_id:
        sql.append(" AND c.id = %s"); params.append(client_id)
    if claimed_by:
        sql.append(" AND t.claimed_by_admin_id = %s"); params.append(claimed_by)
    # Oldest-submission-first when filtering the admin queue; otherwise newest created.
    if statuses == ["pending_admin_review"]:
        sql.append(" ORDER BY rs.submitted_at ASC NULLS LAST, t.created_at ASC")
    else:
        sql.append(" ORDER BY t.created_at DESC")
    sql.append(" LIMIT %s"); params.append(limit)

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("".join(sql), params)
            items = [_jsonable(r) for r in cur.fetchall()]
            audit(cur, tool_name="list_pending_tasks", tool_category="read",
                  task_id=None, args={"statuses": statuses, "limit": limit},
                  result={"count": len(items)}, duration_ms=duration_ms_since(t0))
    return {"items": items, "count": len(items)}


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Full task tree: task + project + client + drafts + artifacts + chat +
    review_submission (active) + latest revision_note + internal notes +
    pipeline events + uploads."""
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

            cur.execute("SELECT * FROM chat_message WHERE task_id = %s ORDER BY created_at ASC LIMIT 200", (task_id,))
            chat = [_jsonable(r) for r in cur.fetchall()]
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

            # Active submission (unresolved) + latest revision note
            cur.execute("""
                SELECT * FROM review_submission
                 WHERE task_id = %s AND resolved_at IS NULL
                 ORDER BY submitted_at DESC LIMIT 1
            """, (task_id,))
            active_submission = cur.fetchone()
            cur.execute("""
                SELECT * FROM revision_note WHERE task_id = %s
                 ORDER BY created_at DESC LIMIT 1
            """, (task_id,))
            latest_revision = cur.fetchone()

            audit(cur, tool_name="get_task", tool_category="read",
                  task_id=task_id, args={"task_id": task_id},
                  result={"drafts": len(drafts), "artifacts": len(artifacts)},
                  duration_ms=duration_ms_since(t0))

    return {
        "task": _jsonable(row),
        "chat_messages": chat,
        "drafts": drafts,
        "artifacts": artifacts,
        "uploads": uploads,
        "pipeline_events": pipeline,
        "internal_notes": notes,
        "review_submission": _jsonable(active_submission) if active_submission else None,
        "latest_revision_note": _jsonable(latest_revision) if latest_revision else None,
    }


@mcp.tool()
def get_client_profile(client_id: str, limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
    """Client summary + recent projects. No credit/balance (billing removed)."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, email, sector, preferences_json, created_at
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
    """Upload metadata + OCR text. VALIDATION_ERROR details.reason='ocr_not_ready'
    when OCR hasn't run yet."""
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
    """Metadata-only artifact refs for a task."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, task_id, draft_id, client_title, delivered_at, delivered_by_admin_id,
                       length(coalesce(client_note,'')) AS client_note_size
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
    """Artifact with its snapshot body_markdown."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM artifact WHERE id = %s", (artifact_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "get_artifact", "read", None, {"artifact_id": artifact_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND")
            audit(cur, tool_name="get_artifact", tool_category="read",
                  task_id=row["task_id"], args={"artifact_id": artifact_id},
                  result={"title": row["client_title"]},
                  duration_ms=duration_ms_since(t0))
    return _jsonable(row)


# =============================================================================
# Knowledge (regulations / precedents / client_knowledge)
# =============================================================================

@mcp.tool()
def search_regulations(
    query: str,
    status: Annotated[list[str], Field(description="default ['active']")] = None,
    jenis: Annotated[Optional[list[str]], Field(description="e.g. ['UU','PP']")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """FTS over regulation (pgvector hybrid arrives when embeddings are seeded)."""
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
    """Regulation detail. Capped at 200 chunks when include_full_text."""
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
    """FTS over curated precedent KB."""
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


# =============================================================================
# Cowork write tools
# =============================================================================

@mcp.tool()
def claim_task(task_id: str) -> dict:
    """Cowork claims a task. Idempotent for same caller. Transitions status
    pending|revision_needed → cowork_working. Conflict if another agent holds it."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, status, claimed_by_admin_id FROM task WHERE id = %s FOR UPDATE
            """, (task_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "claim_task", "write", None, {"task_id": task_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "task not found")

            if row["claimed_by_admin_id"] and str(row["claimed_by_admin_id"]) != CONFIG.admin_worker_id:
                _audit_error(cur, "claim_task", "write", task_id,
                             {"task_id": task_id}, "CONFLICT", t0)
                raise ToolError("CONFLICT",
                                "task already claimed by a different agent",
                                {"held_by": str(row["claimed_by_admin_id"])})

            if row["status"] not in ("pending", "revision_needed", "cowork_working"):
                _audit_error(cur, "claim_task", "write", task_id,
                             {"task_id": task_id, "status": row["status"]}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR",
                                f"cannot claim task in status '{row['status']}'")

            reused = (
                row["claimed_by_admin_id"]
                and str(row["claimed_by_admin_id"]) == CONFIG.admin_worker_id
                and row["status"] == "cowork_working"
            )

            cur.execute("""
                UPDATE task
                   SET claimed_by_admin_id = %s,
                       claimed_at = coalesce(claimed_at, now()),
                       status = 'cowork_working'::task_status,
                       updated_at = now()
                 WHERE id = %s
             RETURNING claimed_at
            """, (CONFIG.admin_worker_id, task_id))
            claimed_at = cur.fetchone()["claimed_at"]

            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                     VALUES (%s, %s, 'custom'::pipeline_stage, 'claim_task',
                             %s)
            """, (task_id, CONFIG.admin_worker_id,
                  "task re-claimed after revision" if reused else "cowork claimed task"))

            audit(cur, tool_name="claim_task", tool_category="write",
                  task_id=task_id, args={"task_id": task_id},
                  result={"reused": reused}, duration_ms=duration_ms_since(t0))

    return {
        "task_id": task_id,
        "claimed_at": claimed_at.isoformat() if claimed_at else None,
        "reused": reused,
    }


@mcp.tool()
def release_task(task_id: str) -> dict:
    """Release claim (emergency escape). Does NOT change task.status — caller
    decides whether to transition (e.g. back to `pending`)."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                UPDATE task
                   SET claimed_by_admin_id = NULL, claimed_at = NULL, updated_at = now()
                 WHERE id = %s AND claimed_by_admin_id = %s
             RETURNING id
            """, (task_id, CONFIG.admin_worker_id))
            row = cur.fetchone()
            audit(cur, tool_name="release_task", tool_category="write",
                  task_id=task_id, args={"task_id": task_id},
                  result={"released": bool(row)},
                  duration_ms=duration_ms_since(t0))
    return {"released": bool(row)}


@mcp.tool()
def save_draft(
    task_id: str,
    artifact_type: Annotated[str, Field(pattern="^(opinion|memo|draft_document|action_list|triage_summary)$")],
    title: str,
    body_markdown: str,
    source_refs: Annotated[Optional[list[dict]], Field(description="[{type:'regulation', regulation_id, article}, ...]")] = None,
    notes: Optional[str] = None,
) -> dict:
    """Append a draft. Version auto-increments per (task_id, artifact_type)."""
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
    """Fork a new version from an existing draft (append-only, version+1).
    Use this after `reject_with_revision` to iterate."""
    if not any([body_markdown, title, source_refs, notes]):
        raise ToolError("VALIDATION_ERROR", "at least one mutable field is required")
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM draft WHERE id = %s", (draft_id,))
            parent = cur.fetchone()
            if not parent:
                raise ToolError("NOT_FOUND", "parent draft not found")
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
                                 source_refs_json, created_by_admin_id, parent_draft_id, notes,
                                 review_status)
                   VALUES (%s, %s::artifact_type, %s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING id, version, created_at
            """, (task_id, artifact_type, version, title, body_markdown,
                  Jsonb(source_refs or []), CONFIG.admin_worker_id, parent_draft_id, notes))
            row = cur.fetchone()
            cur.execute("""
              INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                   VALUES (%s, %s, 'draft'::pipeline_stage, %s, %s)
            """, (task_id, CONFIG.admin_worker_id, tool_name,
                  f"{artifact_type} v{version} {'revised' if parent_draft_id else 'drafted'}"))
            audit(cur, tool_name=tool_name, tool_category="write",
                  task_id=task_id, args={"artifact_type": artifact_type, "version": version},
                  result={"draft_id": str(row["id"])}, duration_ms=duration_ms_since(t0))
    return {"draft_id": str(row["id"]), "version": version, "artifact_type": artifact_type}


@mcp.tool()
def submit_for_review(
    task_id: str,
    draft_ids: Annotated[list[str], Field(description="drafts (by id) to include in this submission")],
    summary_for_admin: Annotated[str, Field(description="what admin should know before reviewing (≤500 words)")],
) -> dict:
    """Cowork: bundle drafts and hand off to admin for QA.

    Preconditions:
      - task.status == 'cowork_working' and claimed_by caller
      - every draft_id belongs to this task
      - summary_for_admin non-empty

    Effects (one transaction):
      1. INSERT review_submission
      2. UPDATE task.status = 'pending_admin_review', task.review_submission_id
      3. INSERT pipeline_event
    """
    if not draft_ids:
        raise ToolError("VALIDATION_ERROR", "draft_ids must be non-empty")
    if not summary_for_admin or not summary_for_admin.strip():
        raise ToolError("VALIDATION_ERROR", "summary_for_admin must be non-empty")

    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, status, claimed_by_admin_id FROM task WHERE id = %s FOR UPDATE
            """, (task_id,))
            task = cur.fetchone()
            if not task:
                raise ToolError("NOT_FOUND", "task not found")
            if task["status"] != "cowork_working":
                _audit_error(cur, "submit_for_review", "write", task_id,
                             {"status": task["status"]}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR",
                                f"task status must be cowork_working (got {task['status']})")
            if str(task["claimed_by_admin_id"] or "") != CONFIG.admin_worker_id:
                _audit_error(cur, "submit_for_review", "write", task_id,
                             {"held_by": str(task["claimed_by_admin_id"])}, "CONFLICT", t0)
                raise ToolError("CONFLICT", "task not claimed by caller")

            cur.execute("""
                SELECT id FROM draft WHERE task_id = %s AND id = ANY(%s)
            """, (task_id, draft_ids))
            found = {str(r["id"]) for r in cur.fetchall()}
            missing = [d for d in draft_ids if d not in found]
            if missing:
                _audit_error(cur, "submit_for_review", "write", task_id,
                             {"missing": missing}, "VALIDATION_ERROR", t0)
                raise ToolError("VALIDATION_ERROR", "some drafts do not belong to this task",
                                {"missing": missing})

            cur.execute("""
                INSERT INTO review_submission
                       (task_id, draft_ids, summary_for_admin, submitted_by_admin_id)
                VALUES (%s, %s::uuid[], %s, %s)
             RETURNING id, submitted_at
            """, (task_id, draft_ids, summary_for_admin.strip(), CONFIG.admin_worker_id))
            sub = cur.fetchone()

            cur.execute("""
                UPDATE task
                   SET status = 'pending_admin_review'::task_status,
                       review_submission_id = %s,
                       updated_at = now()
                 WHERE id = %s
            """, (sub["id"], task_id))

            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                     VALUES (%s, %s, 'review'::pipeline_stage, 'submit_for_review',
                             %s)
            """, (task_id, CONFIG.admin_worker_id,
                  f"{len(draft_ids)} draft(s) submitted for admin review"))

            audit(cur, tool_name="submit_for_review", tool_category="write",
                  task_id=task_id,
                  args={"draft_count": len(draft_ids)},
                  result={"submission_id": str(sub["id"])},
                  duration_ms=duration_ms_since(t0))

    return {
        "submission_id": str(sub["id"]),
        "task_id": task_id,
        "submitted_at": sub["submitted_at"].isoformat(),
        "draft_count": len(draft_ids),
    }


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
def log_pipeline_event(
    task_id: str,
    summary: str,
    stage_label: Annotated[str, Field(description="required — only stage='custom' is allowed via this tool")],
    payload_json: Optional[dict] = None,
) -> dict:
    """Emit a custom-labelled pipeline event. Use for ad-hoc milestones."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage, stage_label,
                                            source_tool, summary, payload_json)
                     VALUES (%s, %s, 'custom'::pipeline_stage, %s, 'log_pipeline_event', %s, %s)
                  RETURNING id, created_at
            """, (task_id, CONFIG.admin_worker_id, stage_label, summary,
                  Jsonb(payload_json or {})))
            row = cur.fetchone()
            audit(cur, tool_name="log_pipeline_event", tool_category="write",
                  task_id=task_id, args={"stage_label": stage_label},
                  result={"event_id": str(row["id"])},
                  duration_ms=duration_ms_since(t0))
    return {"event_id": str(row["id"]), "created_at": row["created_at"].isoformat()}


# =============================================================================
# Admin write tools (QA gate)
# =============================================================================

@mcp.tool()
def approve_and_deliver(
    task_id: str,
    client_title_overrides: Annotated[
        Optional[dict[str, str]],
        Field(description="{draft_id: new_title} — rename per-draft on delivery")
    ] = None,
    client_note: Annotated[Optional[str], Field(description="message shown to client in the chat bubble")] = None,
    internal_note: Annotated[Optional[str], Field(description="admin-only note appended to task")] = None,
) -> dict:
    """Admin QA: approve the active review_submission and deliver every draft
    as a snapshot artifact. One atomic transaction.

    Effects:
      1. UPDATE draft.review_status='approved' for each draft in submission
      2. INSERT artifact (body_markdown snapshot) for each draft
      3. UPDATE task.status='delivered', delivered_at=now()
      4. UPDATE review_submission (resolved_at, resolution='approved')
      5. INSERT chat_message (message_type='artifact_delivery') — single
         aggregate bubble with artifact_refs array
      6. Optional internal_note INSERT
      7. Emit pipeline_event stage='delivered'
      8. audit_log
    """
    t0 = time.perf_counter()
    titles = client_title_overrides or {}
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.status, t.review_submission_id,
                       p.client_id, c.name AS client_name
                  FROM task t
                  JOIN project p ON p.id = t.project_id
                  JOIN client c ON c.id = p.client_id
                 WHERE t.id = %s
                 FOR UPDATE OF t
            """, (task_id,))
            base = cur.fetchone()
            if not base:
                _audit_error(cur, "approve_and_deliver", "write", None, {"task_id": task_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "task not found")
            if base["status"] != "pending_admin_review":
                _audit_error(cur, "approve_and_deliver", "write", task_id,
                             {"status": base["status"]}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR",
                                f"expected pending_admin_review, got {base['status']}")
            if not base["review_submission_id"]:
                raise ToolError("STATE_ERROR", "task has no active review_submission")

            cur.execute("""
                SELECT * FROM review_submission WHERE id = %s FOR UPDATE
            """, (base["review_submission_id"],))
            submission = cur.fetchone()
            if not submission or submission["resolved_at"] is not None:
                raise ToolError("STATE_ERROR", "review_submission not active")

            draft_ids = [str(d) for d in submission["draft_ids"]]
            cur.execute("""
                SELECT id, task_id, artifact_type, title, body_markdown
                  FROM draft WHERE id = ANY(%s::uuid[])
            """, (draft_ids,))
            drafts = cur.fetchall()
            if len(drafts) != len(draft_ids):
                raise ToolError("STATE_ERROR", "submission references missing drafts")

            artifact_ids: list[str] = []
            client_titles: list[str] = []
            for d in drafts:
                final_title = titles.get(str(d["id"])) or d["title"]
                cur.execute("""
                    INSERT INTO artifact
                           (task_id, draft_id, artifact_type, client_title,
                            body_markdown, client_note,
                            delivered_by_admin_id, delivered_at,
                            review_status, created_by_admin_id)
                    VALUES (%s, %s, %s::artifact_type, %s, %s, %s, %s, now(),
                            'delivered', %s)
                 RETURNING id
                """, (task_id, d["id"], d["artifact_type"], final_title,
                      d["body_markdown"], client_note,
                      CONFIG.admin_worker_id, CONFIG.admin_worker_id))
                artifact_ids.append(str(cur.fetchone()["id"]))
                client_titles.append(final_title)

            cur.execute("""
                UPDATE draft SET review_status = 'approved'
                 WHERE id = ANY(%s::uuid[])
            """, (draft_ids,))

            cur.execute("""
                UPDATE review_submission
                   SET resolved_at = now(), resolution = 'approved'
                 WHERE id = %s
            """, (submission["id"],))

            cur.execute("""
                UPDATE task
                   SET status = 'delivered'::task_status,
                       delivered_at = now(),
                       updated_at = now()
                 WHERE id = %s
            """, (task_id,))

            if len(client_titles) == 1:
                body = client_note or client_titles[0]
            else:
                listing = "\n".join(f"• {t}" for t in client_titles)
                body = (client_note + "\n\n" + listing) if client_note else listing
            cur.execute("""
                INSERT INTO chat_message
                       (task_id, sender_type, sender_admin_id, message_type,
                        body, artifact_refs)
                VALUES (%s, 'admin', %s, 'artifact_delivery', %s, %s::uuid[])
            """, (task_id, CONFIG.admin_worker_id, body, artifact_ids))

            if internal_note:
                cur.execute("""
                    INSERT INTO internal_note (task_id, admin_worker_id, body)
                         VALUES (%s, %s, %s)
                """, (task_id, CONFIG.admin_worker_id, internal_note))

            cur.execute("""
                INSERT INTO pipeline_event (task_id, admin_worker_id, stage, source_tool, summary)
                     VALUES (%s, %s, 'delivered'::pipeline_stage, 'approve_and_deliver', %s)
            """, (task_id, CONFIG.admin_worker_id,
                  f"{len(artifact_ids)} artifact(s) delivered to {base['client_name']}"))

            audit(cur, tool_name="approve_and_deliver", tool_category="write",
                  task_id=task_id,
                  args={"draft_count": len(draft_ids)},
                  result={"artifact_ids": artifact_ids},
                  duration_ms=duration_ms_since(t0))

    return {
        "task_id": task_id,
        "artifact_ids": artifact_ids,
        "delivered_count": len(artifact_ids),
    }


@mcp.tool()
def reject_with_revision(
    task_id: str,
    overall_feedback: Annotated[str, Field(description="summary feedback — non-empty")],
    priority: Annotated[str, Field(pattern="^(revise_all|revise_specific)$")] = "revise_all",
    per_draft_feedback: Annotated[
        Optional[dict[str, str]],
        Field(description="{draft_id: feedback_text} — required when priority=revise_specific")
    ] = None,
    must_fix_items: Annotated[
        Optional[list[str]],
        Field(description="short bullets the cowork must address")
    ] = None,
) -> dict:
    """Admin QA: send the submission back with structured notes. Cowork can
    iterate via update_draft + submit_for_review again.

    Effects:
      1. INSERT revision_note
      2. UPDATE draft.review_status='rejected' for drafts in submission
      3. UPDATE review_submission (resolved_at, resolution='rejected')
      4. UPDATE task.status='revision_needed', revision_count++,
         latest_revision_note_id, clear claimed_by/claimed_at so cowork must
         re-claim
      5. Emit pipeline_event stage='custom' label='revision_requested'
      6. audit_log
    """
    if not overall_feedback or not overall_feedback.strip():
        raise ToolError("VALIDATION_ERROR", "overall_feedback must be non-empty")
    per_draft = per_draft_feedback or {}
    if priority == "revise_specific" and not per_draft:
        raise ToolError("VALIDATION_ERROR",
                        "per_draft_feedback required when priority=revise_specific")

    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, status, review_submission_id, revision_count
                  FROM task WHERE id = %s FOR UPDATE
            """, (task_id,))
            task = cur.fetchone()
            if not task:
                raise ToolError("NOT_FOUND", "task not found")
            if task["status"] != "pending_admin_review":
                _audit_error(cur, "reject_with_revision", "write", task_id,
                             {"status": task["status"]}, "STATE_ERROR", t0)
                raise ToolError("STATE_ERROR",
                                f"expected pending_admin_review, got {task['status']}")
            if not task["review_submission_id"]:
                raise ToolError("STATE_ERROR", "task has no active review_submission")

            cur.execute("""
                SELECT draft_ids FROM review_submission WHERE id = %s FOR UPDATE
            """, (task["review_submission_id"],))
            submission = cur.fetchone()
            valid_draft_ids = {str(d) for d in submission["draft_ids"]}
            stray = [k for k in per_draft.keys() if k not in valid_draft_ids]
            if stray:
                raise ToolError("VALIDATION_ERROR",
                                "per_draft_feedback references drafts not in submission",
                                {"stray_draft_ids": stray})

            cur.execute("""
                INSERT INTO revision_note
                       (task_id, review_submission_id, overall_feedback,
                        per_draft_feedback, priority, must_fix_items,
                        created_by_admin_id)
                VALUES (%s, %s, %s, %s, %s, %s::text[], %s)
             RETURNING id, created_at
            """, (task_id, task["review_submission_id"],
                  overall_feedback.strip(),
                  Jsonb(per_draft),
                  priority,
                  must_fix_items or [],
                  CONFIG.admin_worker_id))
            note = cur.fetchone()

            cur.execute("""
                UPDATE draft SET review_status = 'rejected'
                 WHERE id = ANY(%s::uuid[])
            """, (list(valid_draft_ids),))

            cur.execute("""
                UPDATE review_submission
                   SET resolved_at = now(), resolution = 'rejected'
                 WHERE id = %s
            """, (task["review_submission_id"],))

            cur.execute("""
                UPDATE task
                   SET status = 'revision_needed'::task_status,
                       latest_revision_note_id = %s,
                       review_submission_id = NULL,
                       revision_count = revision_count + 1,
                       claimed_by_admin_id = NULL,
                       claimed_at = NULL,
                       updated_at = now()
                 WHERE id = %s
            """, (note["id"], task_id))

            cur.execute("""
                INSERT INTO pipeline_event
                       (task_id, admin_worker_id, stage, stage_label,
                        source_tool, summary, payload_json)
                VALUES (%s, %s, 'custom'::pipeline_stage, 'revision_requested',
                        'reject_with_revision', %s, %s)
            """, (task_id, CONFIG.admin_worker_id,
                  f"admin requested revision ({priority})",
                  Jsonb({"must_fix_count": len(must_fix_items or [])})))

            audit(cur, tool_name="reject_with_revision", tool_category="write",
                  task_id=task_id,
                  args={"priority": priority, "must_fix_count": len(must_fix_items or [])},
                  result={"revision_note_id": str(note["id"]),
                          "revision_count": task["revision_count"] + 1},
                  duration_ms=duration_ms_since(t0))

    return {
        "task_id": task_id,
        "revision_note_id": str(note["id"]),
        "revision_count": task["revision_count"] + 1,
    }


# =============================================================================
# Storage / workspace helpers (unchanged — orthogonal to the review flow)
# =============================================================================

@mcp.tool()
def download_upload(
    upload_id: str,
    overwrite: Annotated[bool, Field(description="re-download even if a local copy exists")] = False,
) -> dict:
    """Download one upload from Supabase Storage into the local workspace."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, project_id, task_id, storage_bucket, storage_path,
                       original_filename, mime_type, size_bytes
                  FROM upload WHERE id = %s::uuid
            """, (upload_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "download_upload", "read", None,
                             {"upload_id": upload_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND", "upload not found")

            dest = wima_storage.local_path_for(row["project_id"], row["original_filename"])
            already = dest.exists()
            bytes_written = None
            if already and not overwrite:
                action = "cached"
            else:
                try:
                    bytes_written = wima_storage.download_object(
                        row["storage_bucket"], row["storage_path"], dest)
                    action = "downloaded"
                except Exception as e:
                    _audit_error(cur, "download_upload", "read", row["task_id"],
                                 {"upload_id": upload_id}, "UPSTREAM_ERROR", t0)
                    raise ToolError("UPSTREAM_ERROR", str(e))

            audit(cur, tool_name="download_upload", tool_category="read",
                  task_id=row["task_id"],
                  args={"upload_id": upload_id, "overwrite": overwrite},
                  result={"local_path": str(dest), "action": action,
                          "bytes": bytes_written or row["size_bytes"]},
                  duration_ms=duration_ms_since(t0))

    return {
        "upload_id": str(row["id"]),
        "original_filename": row["original_filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "local_path": str(dest),
        "action": action,
    }


@mcp.tool()
def download_task_uploads(
    task_id: str,
    overwrite: Annotated[bool, Field(description="re-download even if a local copy exists")] = False,
) -> dict:
    """Download every upload attached to a task."""
    t0 = time.perf_counter()
    results: list[dict] = []
    errors: list[dict] = []
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, project_id, task_id, storage_bucket, storage_path,
                       original_filename, mime_type, size_bytes
                  FROM upload WHERE task_id = %s::uuid
                  ORDER BY created_at ASC
            """, (task_id,))
            rows = cur.fetchall()
            for row in rows:
                dest = wima_storage.local_path_for(row["project_id"], row["original_filename"])
                try:
                    if dest.exists() and not overwrite:
                        action = "cached"
                    else:
                        wima_storage.download_object(
                            row["storage_bucket"], row["storage_path"], dest)
                        action = "downloaded"
                    results.append({
                        "upload_id": str(row["id"]),
                        "original_filename": row["original_filename"],
                        "mime_type": row["mime_type"],
                        "size_bytes": row["size_bytes"],
                        "local_path": str(dest),
                        "action": action,
                    })
                except Exception as e:
                    errors.append({
                        "upload_id": str(row["id"]),
                        "original_filename": row["original_filename"],
                        "error": str(e),
                    })

            audit(cur, tool_name="download_task_uploads", tool_category="read",
                  task_id=task_id,
                  args={"task_id": task_id, "overwrite": overwrite},
                  result={"count": len(results), "errors": len(errors)},
                  duration_ms=duration_ms_since(t0))
    return {
        "task_id": task_id,
        "files": results,
        "error_count": len(errors),
        "errors": errors,
        "workspace_dir": str(CONFIG.workspace_dir),
    }


@mcp.tool()
def get_upload_signed_url(
    upload_id: str,
    expires_in_seconds: Annotated[int, Field(ge=30, le=7 * 24 * 3600)] = 3600,
) -> dict:
    """Generate a time-limited HTTPS URL for an upload."""
    t0 = time.perf_counter()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, task_id, project_id, storage_bucket, storage_path,
                       original_filename, mime_type, size_bytes
                  FROM upload WHERE id = %s::uuid
            """, (upload_id,))
            row = cur.fetchone()
            if not row:
                _audit_error(cur, "get_upload_signed_url", "read", None,
                             {"upload_id": upload_id}, "NOT_FOUND", t0)
                raise ToolError("NOT_FOUND")
            try:
                url = wima_storage.create_signed_url(
                    row["storage_bucket"], row["storage_path"], expires_in_seconds)
            except Exception as e:
                _audit_error(cur, "get_upload_signed_url", "read", row["task_id"],
                             {"upload_id": upload_id}, "UPSTREAM_ERROR", t0)
                raise ToolError("UPSTREAM_ERROR", str(e))
            audit(cur, tool_name="get_upload_signed_url", tool_category="read",
                  task_id=row["task_id"],
                  args={"upload_id": upload_id, "expires_in_seconds": expires_in_seconds},
                  result={"ok": True}, duration_ms=duration_ms_since(t0))
    return {
        "upload_id": str(row["id"]),
        "original_filename": row["original_filename"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "signed_url": url,
        "expires_in_seconds": expires_in_seconds,
    }


@mcp.tool()
def list_workspace_files(
    project_id: Annotated[Optional[str], Field(description="filter to one project_id folder")] = None,
) -> dict:
    """List files currently cached in the local workspace."""
    base = CONFIG.workspace_dir
    items: list[dict] = []
    if project_id:
        root = base / str(project_id).replace("/", "_")
    else:
        root = base
    if root.exists():
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = p
            stat = p.stat()
            items.append({
                "local_path": str(p),
                "relative_path": str(rel),
                "size_bytes": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
    return {
        "workspace_dir": str(base),
        "scope": str(root),
        "count": len(items),
        "files": items,
    }


# =============================================================================
# Internal helpers
# =============================================================================

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


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    log.info("wima-mcp starting — admin_worker_id=%s", CONFIG.admin_worker_id)
    mcp.run()


if __name__ == "__main__":
    main()
