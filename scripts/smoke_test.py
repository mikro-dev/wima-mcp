"""Smoke test the MCP server by speaking JSON-RPC to it over stdio.

Validates:
  - server boots via `python -m wima_mcp`
  - initialize handshake succeeds
  - tools/list returns >= 19 tools
  - tools/call list_pending_tasks works against the live DB
  - tools/call get_client_profile on a non-existent id returns a ToolError
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parents[1]
PY = HERE / ".venv" / "bin" / "python"


def rpc(proc, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout prematurely")
    return json.loads(line)


def notify(proc, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def main():
    env = os.environ.copy()
    proc = subprocess.Popen(
        [str(PY), "-m", "wima_mcp"],
        cwd=str(HERE),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        # 1) initialize
        init = rpc(proc, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "wima-smoke-test", "version": "0"},
        })
        assert init.get("result"), f"initialize failed: {init}"
        print("✓ initialize")
        notify(proc, "notifications/initialized")

        # 2) tools/list
        t = rpc(proc, "tools/list", {}, msg_id=2)
        tools = t["result"]["tools"]
        names = sorted(x["name"] for x in tools)
        print(f"✓ tools/list  ({len(names)} tools)")
        print("  " + ", ".join(names))
        expected = {
            "list_pending_tasks", "get_task", "get_client_profile", "read_upload",
            "list_artifacts", "get_artifact",
            "search_regulations", "get_regulation", "search_precedents", "get_client_knowledge",
            "send_cta", "post_chat_message",
            "save_draft", "update_draft", "deliver_artifact",
            "log_pipeline_event", "add_internal_note", "claim_task", "release_task",
        }
        missing = expected - set(names)
        assert not missing, f"missing tools: {missing}"
        print("  ✓ all 19 Blok 1 tools registered")

        # 3) tools/call — list_pending_tasks
        r = rpc(proc, "tools/call", {"name": "list_pending_tasks", "arguments": {"limit": 5}}, msg_id=3)
        result = r["result"]
        # FastMCP returns content blocks + structuredContent
        sc = result.get("structuredContent") or {}
        print(f"✓ list_pending_tasks → {sc.get('count', '?')} tasks")

        # 4) tools/call — get_client_profile with bogus id  (error path)
        r = rpc(proc, "tools/call",
                {"name": "get_client_profile", "arguments": {"client_id": "00000000-0000-0000-0000-000000000000"}},
                msg_id=4)
        err = r.get("result", {})
        # isError=True is how FastMCP surfaces tool-raised errors
        assert err.get("isError") is True, f"expected error, got: {err}"
        print("✓ get_client_profile bogus-id → ToolError surfaced")

        # 5) tools/call — search_regulations
        r = rpc(proc, "tools/call",
                {"name": "search_regulations", "arguments": {"query": "ketenagakerjaan", "limit": 3}},
                msg_id=5)
        sc = r["result"].get("structuredContent") or {}
        print(f"✓ search_regulations → {sc.get('count', '?')} hits")

        print("\n✅ smoke test passed.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", errors="replace")
        if err and "--" not in err.splitlines()[0]:
            print("\n--- server stderr ---")
            print(err[:2000])


if __name__ == "__main__":
    main()
