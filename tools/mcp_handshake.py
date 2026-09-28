#!/usr/bin/env python3
"""MCP 协议交接测试：以真子进程 + stdio 跑一次 initialize / tools/list / tools/call。

用法：python tools/mcp_handshake.py
不依赖 MCP 客户端 SDK，直接按 JSON-RPC over stdio 发消息，任何协议层污染都会失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")


def send(proc: subprocess.Popen, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()


def recv(proc: subprocess.Popen, want_id: int | None = None, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            # stdio 通道被日志污染 = 协议失败，必须显式暴露
            raise RuntimeError(f"stdout 不是合法 JSON-RPC：{text[:200]!r}")
        if want_id is None or msg.get("id") == want_id:
            return msg
    raise TimeoutError(f"等待 id={want_id} 超时")


def main() -> int:
    env = dict(os.environ)
    env.setdefault("COMPANY_DD_HOME", str(ROOT / "data"))
    proc = subprocess.Popen(
        [PY, "-m", "company_dd.server"],
        cwd=str(ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "handshake-test", "version": "0.0.1"},
            },
        })
        init = recv(proc, 1)
        info = init.get("result", {}).get("serverInfo", {})
        print(f"[1] initialize OK -> {info.get('name')} {info.get('version')} "
              f"proto={init.get('result', {}).get('protocolVersion')}")

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        time.sleep(0.3)

        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv(proc, 2)["result"]["tools"]
        print(f"[2] tools/list OK -> {len(tools)} 个工具")
        for t in tools:
            print(f"      - {t['name']}: {t.get('description', '')[:52]}")

        send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "resolve_company", "arguments": {"name": "宁德时代"}},
        })
        res = recv(proc, 3)
        payload = res["result"]["content"][0]["text"]
        data = json.loads(payload)
        names = [c.get("name") for c in data.get("candidates", [])][:3]
        print(f"[3] tools/call resolve_company OK -> 候选 {len(data.get('candidates', []))} 个：{names}")

        send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "manual_checklist", "arguments": {"name": "某某科技有限公司"}},
        })
        res4 = recv(proc, 4)
        steps = json.loads(res4["result"]["content"][0]["text"])["steps"]
        print(f"[4] tools/call manual_checklist OK -> {len(steps)} 条人工步骤")

        send(proc, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "build_report", "arguments": {
                "name": "康美药业", "announcement_pages": 2, "page_size": 30, "save_files": False}},
        })
        res5 = recv(proc, 5)
        rep = json.loads(res5["result"]["content"][0]["text"])["report"]
        print(f"[5] tools/call build_report OK -> verdict={rep['verdict']} 证据={rep['evidence_count']} "
              f"红线={len(rep['rules'])} 未验证={len(rep['gaps'])}")

        print("\nMCP 交接测试通过：initialize / tools/list / tools/call（含完整背调）全部正常。")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if err:
            print("\n[stderr]\n" + err[-1200:])


if __name__ == "__main__":
    sys.exit(main())
