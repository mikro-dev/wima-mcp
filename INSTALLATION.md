# Installing Wima MCP in Claude Desktop (Cowork)

Step-by-step guide to wire `wima-mcp` into Claude Desktop so **Cowork** can
triage the inbox, research regulations, draft artifacts, and deliver them —
all from inside a Claude conversation.

Target audience: Nabil + any future admin-role teammate.

---

## 0. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| macOS / Windows 10+ / Linux | — | Claude Desktop runs on all three; paths differ, noted inline |
| Claude Desktop | latest | <https://claude.ai/download> |
| Python | **3.11 or newer** | `python3 --version` must print ≥ 3.11 |
| git | 2.30+ | `git --version` |
| Supabase project access | — | You need the Postgres DSN with a role that can bypass RLS (session pooler postgres user, or service role) |
| Your `admin_worker.id` | uuid | Row in `admin_worker` with `status='active'`. If in doubt, see §5 below |

You do **not** need `gh`, `node`, Rust, or Docker for this server. It's a pure
Python stdio process.

---

## 1. Clone the repo

```bash
git clone https://github.com/mikro-dev/wima-mcp.git ~/wima-mcp
cd ~/wima-mcp
```

The path above (`~/wima-mcp`) is recommended because the Claude Desktop config
needs an **absolute path** and `~` isn't expanded there. Pick any directory,
but remember the exact location — you'll paste it into the config file below.

---

## 2. Create a virtualenv + install

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

On Windows the commands are:

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e .
```

**Verify the Python entry point exists:**

```bash
.venv/bin/python -c "from wima_mcp.server import mcp; print(len(mcp._tool_manager._tools), 'tools registered')"
# → 19 tools registered
```

---

## 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` (use `nano`, `vim`, or any GUI editor) and fill the two keys:

```dotenv
# 3a. Supabase session pooler — port 5432 (not 6543).
#     Get yours from Supabase Studio → Project Settings → Database → Connection string → "Session pooler".
WIMA_DB_DSN=postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-1-<REGION>.pooler.supabase.com:5432/postgres?sslmode=require

# 3b. Your admin_worker.id (uuid). See §5 if you don't know it.
WIMA_ADMIN_WORKER_ID=87e604e4-36d8-4f06-b86c-615d3cd1ade5

# 3c. Optional.
WIMA_LOG_LEVEL=INFO
```

> **Why the session pooler, not transaction pooler?** The server issues
> multi-statement transactions (`deliver_artifact`, `send_cta`), uses advisory
> locks for draft versioning, and LISTENs/NOTIFIEs. Port 6543 (pgBouncer in
> transaction mode) can't do any of that.

---

## 4. Smoke-test before touching Claude Desktop

Two quick scripts exercise the wire-up against your real DB:

```bash
.venv/bin/python scripts/smoke_test.py
#  → ✓ initialize
#  → ✓ tools/list  (19 tools)
#  → ✓ all 19 Blok 1 tools registered
#  → ✓ list_pending_tasks → N tasks
#  → ✓ get_client_profile bogus-id → ToolError surfaced
#  → ✓ search_regulations → N hits
#  → ✅ smoke test passed.

# Only run this once at install time — it writes a throwaway test row set:
.venv/bin/python scripts/test_deliver_transaction.py
#  → ✅ deliver_artifact transaction verified — 6/6 side-effects atomic.
```

If either step fails, **do not** proceed to Claude Desktop — the failures will
just surface there as opaque errors. Check §7 Troubleshooting first.

---

## 5. Find your `admin_worker.id`

The MCP server attributes every write to a specific admin. Two ways to find
your id:

### 5a. From Supabase Studio

SQL editor → run:

```sql
select id, email, role, status
  from admin_worker
 where email = 'you@example.com';
```

Copy the `id` value (uuid) into `WIMA_ADMIN_WORKER_ID`.

### 5b. If you don't have an `admin_worker` row yet

The `admin/` Tauri app auto-provisions one on first login (see `admin/README.md`).
Log in there once, then run the query in 5a.

If you're Nabil and the owner row is already seeded, your id is
`87e604e4-36d8-4f06-b86c-615d3cd1ade5` (the value already in `.env.example`).

---

## 6. Wire into Claude Desktop

### 6.1 Locate the config file

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

If the file doesn't exist, create it with `{}` as the starting content.

### 6.2 Add the `wima` server

Open the file and merge the `wima` block under `mcpServers`. If it's empty,
paste:

```json
{
  "mcpServers": {
    "wima": {
      "command": "/Users/YOU/wima-mcp/.venv/bin/python",
      "args": ["-m", "wima_mcp"],
      "env": {
        "WIMA_DB_DSN": "postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-1-<REGION>.pooler.supabase.com:5432/postgres?sslmode=require",
        "WIMA_ADMIN_WORKER_ID": "87e604e4-36d8-4f06-b86c-615d3cd1ade5",
        "WIMA_LOG_LEVEL": "INFO",

        "WIMA_SUPABASE_URL": "https://<PROJECT_REF>.supabase.co",
        "WIMA_SUPABASE_SERVICE_KEY": "<service_role key from Supabase Studio → Settings → API>",
        "WIMA_DOCUMENTS_BUCKET": "documents",
        "WIMA_WORKSPACE_DIR": "~/.wima-cowork/workspace"
      }
    }
  }
}
```

**Three edits you MUST make:**

1. Replace `/Users/YOU/wima-mcp/.venv/bin/python` with the **absolute** path
   to your venv's Python. On macOS: run `echo "$(pwd)/.venv/bin/python"` from
   inside the cloned repo and paste the output. On Windows use
   `echo %cd%\.venv\Scripts\python.exe` in cmd.
2. Fill the real DSN in `WIMA_DB_DSN` (same one that passed §4).
3. Set `WIMA_ADMIN_WORKER_ID` to your uuid from §5.

**One optional edit (enables file-download tools):**

4. Paste your Supabase **service_role** key into `WIMA_SUPABASE_SERVICE_KEY`. You can
   skip this — the 19 Blok 1 tools keep working — but the four storage tools
   (`download_upload`, `download_task_uploads`, `get_upload_signed_url`,
   `list_workspace_files`) need it to fetch object bytes from
   `storage/v1/object/documents/<project_id>/<filename>`.

> **Why repeat the env vars here instead of relying on `.env`?** Claude Desktop
> spawns the server in a completely clean environment — no shell, no login
> profile, no working-directory `.env` load. The server *does* try to read `.env`
> from the package root, but being explicit here makes troubleshooting easier
> and lets multiple Claude Desktop profiles point at different Wima projects.

### 6.3 Already have other MCP servers?

Merge the `wima` entry into the existing `mcpServers` object — don't replace
the whole file. Example with two servers:

```json
{
  "mcpServers": {
    "filesystem": { "command": "...", "args": ["..."] },
    "wima":       { "command": "...", "args": ["-m", "wima_mcp"], "env": { ... } }
  }
}
```

### 6.4 Restart Claude Desktop

Fully quit (⌘Q / File → Exit — closing the window isn't enough) and relaunch.

---

## 7. Verify inside Claude Desktop

In a new conversation, click the 🔌 (tools) icon in the composer. You should
see a collapsible `wima` group listing all **19 tools**:

```
wima
  list_pending_tasks
  get_task
  get_client_profile
  read_upload
  list_artifacts
  get_artifact
  search_regulations
  get_regulation
  search_precedents
  get_client_knowledge
  send_cta
  post_chat_message
  save_draft
  update_draft
  deliver_artifact
  log_pipeline_event
  add_internal_note
  claim_task
  release_task
```

### 7.1 First end-to-end test

Ask Claude in plain Indonesian / English:

> "Pakai wima, tunjukkan semua task yang butuh atensi."
> *(Using wima, show me all tasks needing attention.)*

Claude should call `list_pending_tasks` and render the result. If there are
pending tasks in the DB, you'll see them. If the DB is empty, you'll see
`{items: [], count: 0}` — that's fine, it proves the wire-up works.

### 7.2 Cowork-style workflow

Try a realistic multi-tool session:

> "Ambil task KSPP terbaru, cari regulasi terkait Pasal 7, lalu draft legal
> opinion v1."

Under the hood Claude will:

1. `list_pending_tasks` → pick the KSPP one
2. `get_task(task_id=…)` → read chat + existing drafts
3. `search_regulations("PP 35/2021 pasal 40 pesangon")` → find references
4. `claim_task(task_id)` → open a cowork_session
5. `save_draft(task_id, artifact_type='opinion', …)` → v1 lands in DB

Every step writes an `audit_log` row, so Nabil can always inspect *what* Cowork
looked at and mutated.

---

## 8. Shipping an artifact to a client (Pay-on-Delivery flow)

After you've iterated on a draft and are ready to charge the client:

> "Kirim memo v3 KSPP ke Dimas, judul klien 'Memo Review KSPP v3', potong 24 credit."

Claude sequences:

1. `get_task(task_id)` to re-read the latest `draft_id` + confirmed CTA
2. `deliver_artifact(draft_id=…, client_title='Memo Review KSPP v3', credit_deduct=24, client_note=…)`

`deliver_artifact` runs **one atomic Postgres transaction** that:

- upserts the `artifact` row (`delivered_at=now()`, `review_status='delivered'`)
- inserts `credit_transaction(direction='debit', reason='delivery', amount=24)` — the `apply_credit_tx` trigger auto-subtracts from `client.balance`
- inserts `chat_message(message_type='artifact_delivery')` — this is what the client sees
- flips `task.status='delivered'`
- emits `pipeline_event(stage='delivered')`
- writes `audit_log`

If the client is short on balance (say balance=20, credit_deduct=24), the
transaction rolls back with `INSUFFICIENT_BALANCE` and **nothing** was
committed — no half-delivered state, no phantom chat messages.

---

## 9. Updating

```bash
cd ~/wima-mcp
git pull
.venv/bin/pip install -r requirements.txt  # in case deps bumped
```

No Claude Desktop restart needed unless `pyproject.toml` or new env vars were
introduced — check `CHANGELOG.md` (when it exists) or the commit messages.

---

## 10. Uninstall

```bash
rm -rf ~/wima-mcp
```

Then remove the `"wima": { … }` block from
`~/Library/Application Support/Claude/claude_desktop_config.json` and restart
Claude Desktop.

The DB is untouched — nothing on the server side was installed, the MCP
process just held a DB connection.

---

## 11. Troubleshooting

### "Wima server failed to start"

Claude Desktop silently swallows stderr from MCP servers on some platforms. Run
the server manually to see the real error:

```bash
cd ~/wima-mcp
.venv/bin/python -m wima_mcp
# stdin stays open, waiting for JSON-RPC. Type Ctrl-D to exit after confirming
# it prints the "wima-mcp starting" line without crashing.
```

### `could not determine data type of parameter $N`

This means a query was written without explicit `::type` casts and psycopg
can't infer it. Shouldn't happen in the shipped code — if it does, file an
issue with the full traceback.

### "No tools available" in Claude Desktop

The config JSON is malformed. Open it in an editor that validates JSON (VS
Code, JSONLint). Trailing commas and unbalanced braces are the usual cause.

### Tools show up but every call errors with `NOT_FOUND`

The DSN points at the wrong Supabase project (one without the Wima tables).
Double-check `SCHEMA_SUPABASE.md` + `scripts/admin_rls.sql` have been applied
to the project you're hitting.

### `PERMISSION DENIED for schema auth` or similar

You're connecting as the `anon` or `authenticated` role. Use the `postgres`
role via the session pooler, which is what the default Supabase connection
string gives you.

### Server logs

The server logs to stderr. Claude Desktop captures them at:

- macOS: `~/Library/Logs/Claude/mcp-server-wima.log`
- Windows: `%LOCALAPPDATA%\Claude\logs\mcp-server-wima.log`
- Linux: `~/.local/share/Claude/logs/mcp-server-wima.log`

Set `WIMA_LOG_LEVEL=DEBUG` in the config env block for verbose output.

---

## 12. Security posture — the short version

- **No service role key ships anywhere** except your local `.env`. The Tauri
  apps only carry the `anon` key.
- The MCP server holds a DB password directly — it only ever runs on your
  laptop via stdio. **Don't expose it as a network service** without adopting
  the SSE + MCP token scheme from `DECISIONS_v2.md §H.6` first.
- Every write is attributed to your `WIMA_ADMIN_WORKER_ID` via `audit_log`.
  Rotating that uuid means rotating your Claude Desktop config — no DB change
  needed.
- To fully revoke your MCP access at the DB level (e.g., laptop lost), set
  your admin_worker row to `status='inactive'`; `is_admin_user()` starts
  returning `false` immediately and the admin Tauri app locks you out. The
  MCP server itself continues to work because it bypasses RLS, so also
  rotate the Supabase DB password if threat model requires.
