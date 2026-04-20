"""Probe the four storage tools via stdio JSON-RPC.

Phase A — always runs, no service_role key required:
  - list_workspace_files → succeeds, shows empty workspace or whatever is cached
  - download_upload without WIMA_SUPABASE_SERVICE_KEY → clean UPSTREAM_ERROR

Phase B — runs only if WIMA_SUPABASE_SERVICE_KEY is set in .env:
  - seeds a one-shot test client + project + task + upload row + storage obj
  - calls download_upload → file lands under WIMA_WORKSPACE_DIR/<project_id>/
  - calls list_workspace_files filtered to that project → sees the file
  - calls get_upload_signed_url → HTTPS URL back, HEAD returns 200

Run:
    /Users/admin/Downloads/wima-v2-handover/mcp/.venv/bin/python \
      /Users/admin/Downloads/wima-v2-handover/mcp/scripts/test_download_tools.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import uuid

import httpx
import psycopg

HERE = pathlib.Path(__file__).resolve().parents[1]
PY = HERE / ".venv" / "bin" / "python"

from dotenv import load_dotenv
load_dotenv(HERE / ".env")

SUPABASE_URL = os.environ.get("WIMA_SUPABASE_URL", "").rstrip("/")
SERVICE_KEY  = os.environ.get("WIMA_SUPABASE_SERVICE_KEY", "")
DB_DSN       = os.environ.get("WIMA_DB_DSN", "")


def init(proc):
    proc.stdin.write(b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}\n')
    proc.stdin.flush()
    proc.stdout.readline()
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()


def call(proc, mid, name, args, *, expect_error=False):
    msg = {"jsonrpc":"2.0","id":mid,"method":"tools/call",
           "params":{"name":name,"arguments":args}}
    proc.stdin.write((json.dumps(msg)+"\n").encode())
    proc.stdin.flush()
    r = json.loads(proc.stdout.readline())
    is_err = r.get("result",{}).get("isError", False)
    if expect_error and not is_err:
        raise AssertionError(f"expected error on {name}, got: {r}")
    if not expect_error and is_err:
        raise AssertionError(f"unexpected error on {name}: {r}")
    if is_err:
        return {"_error": r["result"]["content"][0].get("text", "")}
    sc = r["result"].get("structuredContent")
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in r["result"].get("content", []):
        if block.get("type") == "text":
            try: return json.loads(block["text"])
            except ValueError: pass
    return {}


def phase_a():
    print("=== phase A · always-on ===\n")
    proc = subprocess.Popen([str(PY), "-m", "wima_mcp"], cwd=str(HERE),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        init(proc)
        print("  list_workspace_files (empty scope):")
        r = call(proc, 1, "list_workspace_files", {})
        print(f"    ✓ workspace={r['workspace_dir']}  count={r['count']}")

        # Without creds, download_upload of a *real* upload must fail cleanly
        # (NOT_FOUND upload_id would error earlier, before storage layer fires).
        if not SERVICE_KEY:
            real_upload_id = None
            with psycopg.connect(DB_DSN, connect_timeout=15) as c:
                with c.cursor() as cur:
                    cur.execute("select id from upload limit 1")
                    row = cur.fetchone()
                    if row:
                        real_upload_id = str(row[0])
            if real_upload_id:
                print("  download_upload without service key (real upload):")
                r = call(proc, 2, "download_upload",
                         {"upload_id": real_upload_id}, expect_error=True)
                msg = r["_error"]
                assert ("SERVICE_KEY" in msg or "WIMA_SUPABASE" in msg
                        or "UPSTREAM" in msg), msg
                print(f"    ✓ error surfaced: {msg[:100]}")
            else:
                print("  (no upload rows to probe — phase A skip)")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


def seed_fixture():
    """Create a tiny test client+project+task+upload with a real storage object."""
    email = f"dl-{uuid.uuid4().hex[:6]}@wima-test.test"
    with psycopg.connect(DB_DSN, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""insert into auth.users
              (id, instance_id, aud, role, email, encrypted_password, email_confirmed_at,
               confirmation_token, recovery_token, email_change,
               email_change_token_new, email_change_token_current,
               raw_app_meta_data, raw_user_meta_data,
               created_at, updated_at, is_super_admin, is_anonymous)
              values (gen_random_uuid(), '00000000-0000-0000-0000-000000000000',
                      'authenticated','authenticated', %s, crypt(%s, gen_salt('bf')),
                      now(), '', '', '', '', '',
                      '{"provider":"email","providers":["email"]}'::jsonb,
                      '{}'::jsonb,
                      now(), now(), false, false)
              returning id""", (email, "Pass-1!"))
            uid = str(cur.fetchone()[0])
            cur.execute("select id from client where auth_user_id = %s", (uid,))
            client_id = str(cur.fetchone()[0])
            cur.execute("insert into project (client_id, name, matter_type) values (%s, %s, 'korporasi') returning id",
                        (client_id, "download test"))
            project_id = str(cur.fetchone()[0])
            cur.execute("insert into task (project_id, title, status, tier) values (%s, %s, 'pending','standard') returning id",
                        (project_id, "download task"))
            task_id = str(cur.fetchone()[0])

    # Upload real bytes to the documents bucket via service_role
    payload = b"wima download tool smoke test\n" + uuid.uuid4().bytes
    filename = "smoke.txt"
    r = httpx.post(
        f"{SUPABASE_URL}/storage/v1/object/documents/{project_id}/{filename}",
        headers={
            "apikey": SERVICE_KEY,
            "Authorization": f"Bearer {SERVICE_KEY}",
            "Content-Type": "text/plain",
            "x-upsert": "true",
        },
        content=payload,
        timeout=60,
    )
    assert r.status_code in (200, 201), r.text

    with psycopg.connect(DB_DSN, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""insert into upload
              (project_id, task_id, uploader_type, uploaded_by_client_id,
               original_filename, mime_type, size_bytes, storage_bucket, storage_path)
              values (%s, %s, 'client', %s, %s, %s, %s, 'documents', %s) returning id""",
              (project_id, task_id, client_id, filename, "text/plain",
               len(payload), f"{project_id}/{filename}"))
            upload_id = str(cur.fetchone()[0])
    return {"uid": uid, "client_id": client_id, "project_id": project_id,
            "task_id": task_id, "upload_id": upload_id, "filename": filename,
            "payload_len": len(payload)}


def cleanup(uid):
    with psycopg.connect(DB_DSN, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("""delete from chat_message where task_id in (
                select t.id from task t join project p on p.id = t.project_id
                  join client c on c.id = p.client_id where c.auth_user_id = %s)""", (uid,))
            cur.execute("""delete from upload where uploaded_by_client_id in (
                select id from client where auth_user_id = %s)""", (uid,))
            cur.execute("delete from auth.users where id = %s", (uid,))


def phase_b():
    print("\n=== phase B · with service_role ===\n")
    fx = seed_fixture()
    try:
        proc = subprocess.Popen([str(PY), "-m", "wima_mcp"], cwd=str(HERE),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        try:
            init(proc)
            print("  download_upload:")
            r = call(proc, 10, "download_upload", {"upload_id": fx["upload_id"]})
            assert r["action"] in ("downloaded", "cached")
            local = pathlib.Path(r["local_path"])
            assert local.is_file(), f"missing local file {local}"
            data = local.read_bytes()
            assert len(data) == fx["payload_len"], f"size {len(data)} != {fx['payload_len']}"
            print(f"    ✓ {r['action']} → {r['local_path']} ({len(data)} bytes)")

            print("  list_workspace_files filtered:")
            r = call(proc, 11, "list_workspace_files", {"project_id": fx["project_id"]})
            assert r["count"] >= 1, r
            print(f"    ✓ {r['count']} file(s) in project folder")

            print("  get_upload_signed_url:")
            r = call(proc, 12, "get_upload_signed_url",
                     {"upload_id": fx["upload_id"], "expires_in_seconds": 120})
            url = r["signed_url"]
            assert url.startswith("https://")
            check = httpx.get(url, timeout=15)
            assert check.status_code == 200 and len(check.content) == fx["payload_len"]
            print(f"    ✓ url returns 200, correct length")

            print("  download_task_uploads:")
            r = call(proc, 13, "download_task_uploads",
                     {"task_id": fx["task_id"], "overwrite": True})
            assert r["error_count"] == 0 and len(r["files"]) == 1
            print(f"    ✓ fetched {len(r['files'])} file(s), 0 errors")

        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill()
    finally:
        cleanup(fx["uid"])
        # clean the local file too
        try:
            local = pathlib.Path(os.environ.get("WIMA_WORKSPACE_DIR", "~/.wima-cowork/workspace")).expanduser()
            for p in (local / fx["project_id"]).glob("*"):
                p.unlink()
            (local / fx["project_id"]).rmdir()
        except Exception:
            pass


def main():
    phase_a()
    if not SERVICE_KEY:
        print("\n(phase B skipped — set WIMA_SUPABASE_SERVICE_KEY in .env to run the full E2E)")
        return
    phase_b()
    print("\n✅ download tools verified.")


if __name__ == "__main__":
    main()
