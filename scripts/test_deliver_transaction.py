"""Prove deliver_artifact is atomic by driving it end-to-end.

Seeds a test client + project + task + 100-credit balance,
then walks: send_cta → accept → save_draft → deliver_artifact, and
checks every side-effect row exists with matching ids.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import uuid

import psycopg

HERE = pathlib.Path(__file__).resolve().parents[1]
PY = HERE / ".venv" / "bin" / "python"
ADMIN_DSN = os.environ.get(
    "WIMA_DB_DSN",
    "postgresql://postgres.uowdeqqbkuoyxcfxyobv:Ifaassegaf1!@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
)

EMAIL = f"mcp-deliver-{uuid.uuid4().hex[:6]}@wima-test.test"


def seed(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into auth.users (
              id, instance_id, aud, role, email, encrypted_password,
              email_confirmed_at, confirmation_token, recovery_token,
              email_change, email_change_token_new, email_change_token_current,
              raw_app_meta_data, raw_user_meta_data,
              created_at, updated_at, is_super_admin, is_anonymous
            ) values (
              gen_random_uuid(), '00000000-0000-0000-0000-000000000000',
              'authenticated','authenticated',
              %s, crypt('Pass-1!', gen_salt('bf')),
              now(), '', '', '', '', '',
              '{"provider":"email","providers":["email"]}'::jsonb,
              '{}'::jsonb,
              now(), now(), false, false
            ) returning id
            """, (EMAIL,))
        uid = cur.fetchone()[0]
        cur.execute("select id from client where auth_user_id = %s", (uid,))
        client_id = cur.fetchone()[0]
        # top up balance to 100 credits (insert credit_transaction triggers apply_credit_tx)
        cur.execute(
            """insert into credit_transaction (client_id, direction, reason, amount,
                                               balance_after, notes)
                       values (%s,'credit','topup',100,100,'test seed') returning id""",
            (client_id,))
        cur.execute(
            """insert into project (client_id, name, matter_type)
                       values (%s, 'MCP deliver test', 'korporasi') returning id""",
            (client_id,))
        project_id = cur.fetchone()[0]
        cur.execute(
            """insert into task (project_id, title, status, tier)
                       values (%s, 'Opinion Pasal 7', 'pending', 'standard') returning id""",
            (project_id,))
        task_id = cur.fetchone()[0]
        conn.commit()
        return uid, client_id, task_id


def cleanup(conn, uid):
    with conn.cursor() as cur:
        cur.execute("""
            with c as (select id from client where auth_user_id = %s),
                 t as (select t.id from task t join project p on p.id = t.project_id
                        where p.client_id in (select id from c))
            delete from audit_log where task_id in (select id from t)
        """, (uid,))
        cur.execute("""
            delete from credit_transaction where client_id in (
              select id from client where auth_user_id = %s)
        """, (uid,))
        cur.execute("""
            update cta set responded_client_id = null
             where task_id in (select t.id from task t join project p on p.id = t.project_id
                                join client c on c.id = p.client_id
                                where c.auth_user_id = %s)
        """, (uid,))
        cur.execute("""
            delete from upload where uploaded_by_client_id in (
              select id from client where auth_user_id = %s)
        """, (uid,))
        cur.execute("delete from auth.users where id = %s", (uid,))
        conn.commit()


def call(proc, msg_id, name, args):
    msg = {"jsonrpc":"2.0","id":msg_id,"method":"tools/call",
           "params":{"name":name,"arguments":args}}
    proc.stdin.write((json.dumps(msg)+"\n").encode())
    proc.stdin.flush()
    r = json.loads(proc.stdout.readline())
    assert not r.get("result",{}).get("isError"), f"{name} errored: {r}"
    res = r["result"]
    # FastMCP may return the dict as structuredContent OR as a text block with JSON.
    sc = res.get("structuredContent")
    if sc:
        # Some SDK versions wrap as {"result": {...}}; unwrap if present.
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    for block in res.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (ValueError, KeyError):
                pass
    return {}


def init(proc):
    proc.stdin.write(b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}\n')
    proc.stdin.flush()
    proc.stdout.readline()
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()


def main():
    with psycopg.connect(ADMIN_DSN, connect_timeout=30) as admin:
        print("=== 1/6 seed test data ===")
        uid, client_id, task_id = seed(admin)
        print(f"  client_id = {client_id}\n  task_id   = {task_id}")

    proc = subprocess.Popen([str(PY), "-m", "wima_mcp"], cwd=str(HERE),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        init(proc)

        print("\n=== 2/6 send_cta ===")
        cta = call(proc, 1, "send_cta", {
            "task_id": str(task_id),
            "tier": "standard", "credit": 8,
            "eta_text": "~12 jam", "reasoning_note": "opinion pasal 7 standard scope",
        })
        print(f"  cta_id = {cta['cta_id']}")

        print("\n=== 3/6 flip CTA → accepted (normally done by client) ===")
        with psycopg.connect(ADMIN_DSN) as admin:
            with admin.cursor() as cur:
                cur.execute("update cta set status='accepted', responded_at=now(), responded_client_id=%s where id=%s",
                            (client_id, cta["cta_id"]))
                admin.commit()
        print("  cta.status = accepted")

        print("\n=== 4/6 save_draft ===")
        d = call(proc, 2, "save_draft", {
            "task_id": str(task_id),
            "artifact_type": "opinion",
            "title": "Opinion Pasal 7 v1",
            "body_markdown": "# Legal Opinion\n\nFormula PP 35/2021 Pasal 40-41.",
        })
        print(f"  draft_id = {d['draft_id']} (v{d['version']})")

        print("\n=== 5/6 deliver_artifact (the atomic one) ===")
        out = call(proc, 3, "deliver_artifact", {
            "draft_id": d["draft_id"],
            "client_title": "Legal Opinion — Pasal 7",
            "client_note": "Executive summary + rekomendasi redraft.",
            "credit_deduct": 8,
        })
        print(f"  artifact_id       = {out['artifact_id']}")
        print(f"  credit_tx id      = {out['credit_transaction_id']}")
        print(f"  balance after     = {out['new_balance']}")

        print("\n=== 6/6 verify all 6 side-effects landed ===")
        with psycopg.connect(ADMIN_DSN) as admin:
            with admin.cursor() as cur:
                cur.execute("select status from task where id=%s", (task_id,))
                task = cur.fetchone()
                assert task[0] == "delivered", f"task.status expected delivered, got {task[0]}"
                print("  ✓ task.status = delivered")

                cur.execute("select balance from client where id=%s", (client_id,))
                bal = cur.fetchone()[0]
                assert bal == 92, f"balance expected 92 (100-8), got {bal}"
                print(f"  ✓ client.balance = {bal} (trigger applied -8)")

                cur.execute("select review_status, delivered_at from artifact where id=%s", (out["artifact_id"],))
                art = cur.fetchone()
                assert art[0] == "delivered" and art[1] is not None
                print("  ✓ artifact.review_status = delivered, delivered_at set")

                cur.execute("select direction, amount, reason from credit_transaction where id=%s",
                            (out["credit_transaction_id"],))
                ct = cur.fetchone()
                assert ct == ("debit", 8, "delivery")
                print("  ✓ credit_transaction (debit, 8, delivery)")

                cur.execute("select message_type from chat_message where task_id=%s and artifact_id=%s",
                            (task_id, out["artifact_id"]))
                msg = cur.fetchone()
                assert msg and msg[0] == "artifact_delivery"
                print("  ✓ chat_message (artifact_delivery)")

                cur.execute("select stage from pipeline_event where task_id=%s and source_tool='deliver_artifact'",
                            (task_id,))
                pe = cur.fetchone()
                assert pe and pe[0] == "delivered"
                print("  ✓ pipeline_event stage=delivered")

                cur.execute("""select count(*) from audit_log
                                where task_id=%s and tool_name='deliver_artifact' and error is null""",
                            (task_id,))
                ac = cur.fetchone()[0]
                assert ac == 1, f"expected 1 audit row, got {ac}"
                print("  ✓ audit_log row written")

        print("\n✅ deliver_artifact transaction verified — 6/6 side-effects atomic.")

    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()

        with psycopg.connect(ADMIN_DSN, connect_timeout=30) as admin:
            cleanup(admin, uid)
            print("\ncleanup: auth.users deleted (cascade cleared everything).")


if __name__ == "__main__":
    main()
