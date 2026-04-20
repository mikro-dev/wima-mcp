# Wima v2 — Current System Flow

**As of:** 20 April 2026 — post-SCHEMA_SUPABASE.md §4.3 admin RLS + MCP server wiring.

## 1. Component map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            SUPABASE (single project)                         │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Postgres 17  +  pgvector  +  pg_cron                                   │  │
│  │  • 19 Wima tables (SCHEMA_SUPABASE.md §2)                              │  │
│  │  • RLS policies: client-scoped (§4.1) + admin bypass (§4.3)            │  │
│  │  • SQL RPCs: post_client_chat_message, respond_to_cta,                 │  │
│  │              create_client_topup_invoice                               │  │
│  │  • Realtime publication on chat/cta/artifact/pipeline/task/credit      │  │
│  │  • Storage buckets: documents, artifacts, invoices                     │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
          ▲                          ▲                           ▲
          │ anon-key JWT             │ anon-key JWT              │ postgres DSN
          │ (user scoped)            │ (admin UID gated)         │ (superuser, no RLS)
          │                          │                           │
┌─────────┴──────────┐    ┌──────────┴─────────┐    ┌────────────┴────────────┐
│   client/          │    │   admin/           │    │   mcp/                  │
│   Tauri 2 desktop  │    │   Tauri 2 desktop  │    │   Python stdio MCP      │
│   Vue 3 OptionsAPI │    │   Vue 3 OptionsAPI │    │   server  (this dir)    │
│   Pinia            │    │   Pinia            │    │                         │
│                    │    │   UID-gated to     │    │   Runs on Nabil's       │
│   End-user SVP /   │    │   nabil@basyuni…   │    │   laptop, invoked by    │
│   legal staff      │    │                    │    │   Claude Desktop        │
└────────────────────┘    └────────────────────┘    └──────────┬──────────────┘
                                                                │ stdio (JSON-RPC)
                                                                ▼
                                                    ┌───────────────────────┐
                                                    │  Claude Desktop (Mac) │
                                                    │   Cowork = Opus 4.x   │
                                                    │   with MCP wired      │
                                                    └───────────────────────┘
```

Three paths into Postgres, each with a distinct **trust boundary**:

| Path            | Credential                        | RLS evaluation                    | Who uses it           |
|-----------------|-----------------------------------|-----------------------------------|-----------------------|
| PostgREST       | `anon` + user JWT                 | Full RLS — per-client scope       | `client/` app         |
| PostgREST       | `anon` + admin JWT                | RLS bypassed via `is_admin_user()` + admin policies | `admin/` app          |
| Direct DB       | `postgres` DSN (session pooler)   | Bypassed (superuser)              | `mcp/` server (this)  |

The MCP server intentionally skips RLS — it is the "service-layer" DECISIONS §H names
as the Cowork channel. Attribution falls back to `WIMA_ADMIN_WORKER_ID` for every
write (see §4 below).

## 2. Request shape — end-to-end examples

### 2.1 Client sends a chat message

```
Client Tauri ──POST /rest/v1/rpc/post_client_chat_message──► PostgREST
                        (JWT signed with anon key)                │
                                                                  ▼
                                                          Postgres RPC
                                                (SECURITY DEFINER, gates on
                                                 current_client_id())
                                                                  │
                                                                  ▼
                      INSERT chat_message  ◄───── UPDATE task.status (if awaiting_client)
                             │
                             ▼
                     Realtime NOTIFY  ──► any subscribed client (other tabs,
                                           admin app, …) sees it live.
```

### 2.2 Cowork drafts + delivers an artifact

```
1. Nabil in Claude Desktop:  "baca draft KSPP pasal 7"
      │
      ▼
   Claude Desktop invokes MCP tool:  get_task(task_id=…) via stdio JSON-RPC
      │
      ▼
   wima_mcp.server  ──► psycopg.connect(DSN)  ──► SELECT … FROM task … ;
                                                 SELECT … FROM draft …
      │
      ▼
   Tool returns {task, project, chat, drafts, artifacts, uploads, ctas}
      │
      ▼
   Claude Desktop reasons, composes a new draft body → calls save_draft(task_id, …)
      │                                                       │
      │                                                       ▼
      │                                          Insert draft (next version)
      │                                          Emit pipeline_event stage='draft'
      │                                          Insert audit_log
      │                                          Realtime NOTIFY
      ▼
2. Nabil in admin/ console sees the new draft via Realtime; clicks Deliver.
   (Or Claude Desktop can call deliver_artifact directly.)

3. deliver_artifact(draft_id, client_title, client_note, credit_deduct):
      one Postgres transaction does all of —
        ── SELECT client FOR UPDATE  (lock balance row)
        ── INSERT/UPDATE artifact  (delivered_at, client_title)
        ── INSERT credit_transaction (direction='debit', reason='delivery',
                                      amount = credit_deduct,
                                      balance_after = client.balance − credit_deduct)
             └─ trigger apply_credit_tx: UPDATE client SET balance -= amount
        ── INSERT chat_message (message_type='artifact_delivery', artifact_id)
        ── UPDATE task SET status='delivered'
        ── INSERT pipeline_event (stage='delivered')
        ── INSERT audit_log
```

The four mandatory side-effects called out in DECISIONS §K.2.2 ("`deliver_artifact`
atomicity") happen inside **one** BEGIN…COMMIT — no partial states on crash.

### 2.3 CTA confirm loop (15-minute auto-confirm)

```
Cowork sends a CTA:
    Cowork.send_cta(task_id, tier='urgent', credit=24, eta_text='~1 jam')
        │
        ▼
    INSERT cta (status='pending')
    INSERT scheduled_job (job_type='auto_confirm_cta', run_at=now()+15 min,
                          payload_json={cta_id,...})
    UPDATE cta SET auto_confirm_job_id = <job.id>
    INSERT chat_message (message_type='cta_prompt', cta_id)
    INSERT pipeline_event (stage='classify')

Client sees the CTA via Realtime, has 15 minutes to accept / decline:
    — accept/decline → rpc('respond_to_cta') → cta.status updated, job cancelled
    — timeout        → pg_cron (every minute) flips pending CTAs past run_at
                        to status='auto_confirmed', no credit debit yet.

Credit is NOT debited here (Pay-on-Delivery, DECISIONS §K.1.1).
Debit happens only inside deliver_artifact().
```

## 3. Why the MCP server bypasses RLS

Three reasons, locked in DECISIONS:

1. **§H.4 — Cowork tools must see every client.** Tools like `list_pending_tasks`,
   `search_precedents`, `get_client_knowledge` span clients by design. RLS would
   require Cowork to carry multiple identities.
2. **§I.4 — Cross-cutting invariants.** `task.pinned_pipeline_stage` denorm is
   maintained by a trigger that can only fire if the write reaches the table.
   Bypassing RLS keeps the single-writer invariant simple.
3. **§N-§P — Orchestrator-grade transactions.** `deliver_artifact`, `handover_task`,
   `open_bulk_session` are multi-table atomic writes. Having the MCP server hold
   a single superuser transaction is the cleanest place to express them.

The trade-off — full trust in the MCP server — is offset by:

- The MCP server runs **on Nabil's laptop**, never exposed to the network. stdio
  transport, no listener port. (If you move to a hosted MCP later, switch to SSE
  + MCP bearer tokens per §H.6.)
- Every tool call writes a row to `audit_log` with `admin_worker_id` = env
  `WIMA_ADMIN_WORKER_ID`, so the trail is preserved even if the caller identity
  is not a per-request JWT.
- Rate-limiting + allowlisting lives at the `mcp/` code layer (see
  `src/wima_mcp/server.py`).

## 4. Audit log — single source of truth for Cowork actions

Every write tool (`send_cta`, `save_draft`, `deliver_artifact`, `claim_task`, …)
inserts into `audit_log` as part of its transaction:

```sql
INSERT INTO audit_log (
  admin_worker_id,        -- env WIMA_ADMIN_WORKER_ID
  source,                 -- 'mcp'
  tool_name,              -- e.g. 'deliver_artifact'
  tool_category,          -- 'read' | 'write'
  task_id,                -- scoped where applicable
  args_summary_json,      -- truncated 2KB
  result_summary_json,    -- truncated 2KB
  duration_ms,
  error, error_code,
  request_id
) VALUES (…);
```

Read tools log too (category `read`) so we can diff "what did Cowork look at"
vs "what did Cowork change". This is the only trace we have now that the MCP
server skips RLS.

## 5. Tool catalog (Blok 1 — 19 tools, §H.4 locked)

| # | Tool                | Category | Summary                                                   |
|---|---------------------|----------|-----------------------------------------------------------|
| 1 | `list_pending_tasks`| read     | Inbox triage — filter by status/tier/matter_type/client   |
| 2 | `get_task`          | read     | Full task tree (chat, drafts, artifacts, ctas, pipeline)  |
| 3 | `get_client_profile`| read     | Client summary + past_projects                            |
| 4 | `read_upload`       | read     | Upload metadata + OCR text                                |
| 5 | `list_artifacts`    | read     | Metadata-only refs                                         |
| 6 | `get_artifact`      | read     | Full artifact incl. source_draft body_markdown            |
| 7 | `search_regulations`| read     | Hybrid FTS + pgvector semantic                             |
| 8 | `get_regulation`    | read     | Regulation by id, optional chunk expansion                 |
| 9 | `search_precedents` | read     | Curated precedent KB (vector)                              |
|10 | `get_client_knowledge`| read   | Per-client memory facts                                    |
|11 | `send_cta`          | write    | Propose tier + credit, schedules 15-min auto-confirm job   |
|12 | `post_chat_message` | write    | Admin-side chat message (no auto-pipeline)                 |
|13 | `save_draft`        | write    | Append-only new draft version                               |
|14 | `update_draft`      | write    | Same, with `parent_draft_id` lineage                        |
|15 | `deliver_artifact`  | write    | **Transactional**. Pay-on-Delivery credit debit             |
|16 | `log_pipeline_event`| write    | Custom-stage escape hatch (stage_label required)            |
|17 | `add_internal_note` | write    | Task-scoped internal note (invisible to client)             |
|18 | `claim_task`        | write    | Start cowork_session, increments shift_log (soft-wired)     |
|19 | `release_task`      | write    | Close cowork_session (idempotent)                           |

Blok 4–7 tools (handover, quality review, bulk, client_knowledge writes) are
marked `TODO` in `src/wima_mcp/tools/` — add as those phases open.

## 6. Deployment

Local-only, runs from Claude Desktop. See `README.md` for the Claude Desktop
config snippet. No open port, no TLS cert to manage.
