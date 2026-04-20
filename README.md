# Wima MCP Server

Python MCP server that exposes the Wima v2 Supabase backend to **Claude Desktop** (the "Cowork" channel per `DECISIONS_v2.md §H`). Implements the locked 19-tool catalog from §H.4 as a stdio MCP server.

See **[SYSTEM_FLOW.md](./SYSTEM_FLOW.md)** for the architecture diagram and request flow
across `client/`, `admin/`, and this package.

## Quick start

```bash
cd mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .          # makes `wima-mcp` entry point available
cp .env.example .env                # fill in DSN + admin_worker_id

# verify
.venv/bin/python scripts/smoke_test.py
.venv/bin/python scripts/test_deliver_transaction.py
```

## Wire to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "wima": {
      "command": "/absolute/path/to/wima-v2-handover/mcp/.venv/bin/python",
      "args": ["-m", "wima_mcp"],
      "env": {
        "WIMA_DB_DSN": "postgresql://postgres.uowdeqqbkuoyxcfxyobv:PASSWORD@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
        "WIMA_ADMIN_WORKER_ID": "87e604e4-36d8-4f06-b86c-615d3cd1ade5"
      }
    }
  }
}
```

Restart Claude Desktop. You'll see a 🔌 icon in the composer; click it to confirm `wima` is
connected and all 19 tools are listed.

## Tool catalog — 19 Blok 1 tools + 4 storage tools

| Category | Tool | Side-effects beyond the obvious |
|---|---|---|
| Read | `list_pending_tasks` | — |
| Read | `get_task` | — |
| Read | `get_client_profile` | — |
| Read | `read_upload` | `VALIDATION_ERROR` if OCR not ready |
| Read | `list_artifacts` | metadata only |
| Read | `get_artifact` | includes source-draft body |
| Read | `search_regulations` | FTS (pgvector layer arrives w/ embeddings) |
| Read | `get_regulation` | caps full-text at 200 chunks |
| Read | `search_precedents` | returns `source_artifact_id` for hops |
| Read | `get_client_knowledge` | — |
| Write | `send_cta` | inserts `cta` + `scheduled_job` (15-min) + `chat_message` + emits `pipeline_event` stage=classify + flips `task.status='awaiting_client'` |
| Write | `post_chat_message` | — |
| Write | `save_draft` | advisory-locked version bump + `pipeline_event` stage=draft |
| Write | `update_draft` | fork version via `parent_draft_id` + `pipeline_event` stage=review |
| Write | `deliver_artifact` | **atomic** — update artifact + insert credit_transaction (Pay-on-Delivery) + insert chat_message + flip task.status=delivered + emit pipeline_event stage=delivered |
| Write | `log_pipeline_event` | custom-stage escape (stage_label required) |
| Write | `add_internal_note` | — |
| Write | `claim_task` | opens `cowork_session` (unique per active task); reuses if you already hold it |
| Write | `release_task` | closes session, idempotent |
| Read  | `download_upload` | fetch a single upload's bytes into `WIMA_WORKSPACE_DIR/<project_id>/<filename>`. Returns the local path — Cowork opens that file directly. Uses service_role. |
| Read  | `download_task_uploads` | bulk variant — grabs every upload tied to a task, mirrors layout on disk. Best call to make right after `get_task`. |
| Read  | `get_upload_signed_url` | HTTPS URL valid 30s – 7d. Use when another tool / API needs direct access without going through this MCP. |
| Read  | `list_workspace_files` | purely local — enumerates what's already cached under `WIMA_WORKSPACE_DIR`. No DB, no network. |

Every tool inserts an `audit_log` row with `source='mcp'`, `admin_worker_id` from the env,
`tool_category` (read/write), truncated args + result, duration_ms, and error_code when it
fails. See `SYSTEM_FLOW.md §4` for the rationale.

## Working on client files locally

Cowork workflow now looks like:

```
get_task(task_id)                   ← metadata, chat, drafts, upload refs
      │
      ▼
download_task_uploads(task_id)      ← pulls every file into the workspace
      │
      ▼
… read via the standard Read / filesystem-MCP tools …
… run OCR, diff, extract clauses …
      │
      ▼
save_draft(task_id, ...)            ← commit Cowork's output back to Wima
```

Paths:

| Env var | Default |
|---|---|
| `WIMA_WORKSPACE_DIR` | `~/.wima-cowork/workspace` |
| Per project folder | `$WIMA_WORKSPACE_DIR/<project_id>/` |
| File name | original filename, preserved |

Caching: `download_upload` / `download_task_uploads` skip the network if the
local file already exists. Pass `overwrite=true` to force a re-fetch (e.g. after
the admin re-uploaded a corrected copy).

The storage tools **do not** delete anything remote — cleanup is owner-only
from Supabase Studio. Deleting the local copy is just `rm`.

### Enabling the download tools

One env var beyond the base config:

```dotenv
WIMA_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
WIMA_SUPABASE_SERVICE_KEY=<paste from Supabase Studio → Settings → API → service_role>
```

Without `WIMA_SUPABASE_SERVICE_KEY`, the four storage tools return a clean
`UPSTREAM_ERROR: WIMA_SUPABASE_SERVICE_KEY not set …` instead of crashing.
Everything else (all 19 Blok 1 tools) keeps working.

## Tools deferred — Blok 4–7

Stubs to add when their phase opens:

- Blok 4 (handover): `handover_task`, `accept_handover`, `decline_handover`
- Blok 5 (QA gate): `submit_for_review`, `review_artifact`, `escalate_review`
- Blok 6 (bulk): `open_bulk_session`, `ingest_bulk_files`, `finalize_bulk_clusters`, `cancel_bulk_session`
- Blok 7: `save_client_knowledge`, `cancel_cta`, `override_delivery_credit`

## How is this different from the old FastAPI+MCP plan?

DECISIONS §H.2 originally locked "Opsi C (Embedded)" — MCP as a Python module inside a
FastAPI process. We pivoted to Supabase + no-backend-server, so the MCP server is now a
**standalone Python process on Nabil's laptop**. Tradeoffs:

- ✅ Zero extra infra to run ($0 recurring incremental cost — DECISIONS §H.8 preserved)
- ✅ Transactional `deliver_artifact` still lives in one place (here)
- ⚠️ The MCP process holds DB credentials directly — must never ship to a Tauri bundle
- ⚠️ No Realtime subscription from MCP side; Claude Desktop pulls on demand

If you later scale to multi-admin, swap stdio → SSE (per DECISIONS §H.3/§H.6) and introduce
per-admin MCP tokens so the `admin_worker_id` is derived from auth, not an env var.

## Files

```
mcp/
├── README.md                   # you are here
├── SYSTEM_FLOW.md              # architecture + flow
├── pyproject.toml              # + `wima-mcp` entry point
├── requirements.txt
├── .env.example
├── src/wima_mcp/
│   ├── __init__.py
│   ├── __main__.py             # `python -m wima_mcp`
│   ├── config.py               # dotenv + Config dataclass
│   ├── db.py                   # psycopg conn() + audit() + ToolError
│   └── server.py               # FastMCP + 19 tools
└── scripts/
    ├── smoke_test.py           # proves server boots + tools/list + 3 read calls
    └── test_deliver_transaction.py  # end-to-end send_cta → save_draft → deliver_artifact
```
