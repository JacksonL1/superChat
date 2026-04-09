"""
cli.py
命令行入口。支持：
  superChat chat "消息内容"          ← 发消息并等待输出
  superChat sessions                 ← 列出所有 session
  superChat history [session_id]     ← 查看消息历史
  superChat reset [session_id]       ← 重置 session 历史
  superChat serve                    ← 启动 Gateway 服务
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import httpx
import argparse

from config import settings

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '')))

GATEWAY_URL = "http://localhost:8000"


def _auth_headers() -> dict:
    token = (settings.gateway_access_token or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


# ════════════════════════════════════════════════════════════════
# 子命令
# ════════════════════════════════════════════════════════════════

async def cmd_chat(message: str, session_id: str = "main") -> None:
    """发消息，通过 SSE 实时打印输出直到收到完整回复。"""
    async with httpx.AsyncClient(timeout=300) as client:
        # 先订阅 SSE，避免非常快的回复在订阅前丢失
        async with client.stream("GET", f"{GATEWAY_URL}/stream/{session_id}", headers=_auth_headers()) as stream:
            # 投递消息
            resp = await client.post(
                f"{GATEWAY_URL}/chat",
                json={"message": message, "session_id": session_id},
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            print(f"[queued → {session_id}]")

            async for line in stream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    # [DONE] 不是业务结束条件，继续等待 final=True
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue

                text = payload.get("text", "")
                progress = payload.get("progress", False)
                final = payload.get("final", False)

                if progress:
                    print(f"\033[90m{text}\033[0m", flush=True)  # 灰色显示进度
                else:
                    if text:
                        print(text, flush=True)

                # 仅 final=True 才代表完整回复结束，避免长回复被截断
                if final:
                    break


async def cmd_sessions() -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GATEWAY_URL}/sessions", headers=_auth_headers())
        resp.raise_for_status()
        sessions = resp.json()
        print(f"{'ID':<20} {'ROLE':<12} {'STATUS':<10} CREATED")
        print("-" * 60)
        for s in sessions:
            print(f"{s['id']:<20} {s['role']:<12} {s['status']:<10} {s.get('created_at','')}")


async def cmd_history(session_id: str = "main") -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GATEWAY_URL}/sessions/{session_id}/history", headers=_auth_headers())
        resp.raise_for_status()
        data = resp.json()
        msgs = data.get("messages", [])
        print(f"=== {session_id} history ({len(msgs)} messages) ===")
        for m in msgs:
            role    = m.get("role", "?")
            content = (m.get("content") or "")[:200]
            print(f"\n[{role.upper()}] {content}")


async def cmd_reset(session_id: str = "main") -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{GATEWAY_URL}/sessions/{session_id}/reset", headers=_auth_headers())
        resp.raise_for_status()
        print(f"✅ {session_id} history cleared")


def cmd_serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(
        "gateway.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


# ════════════════════════════════════════════════════════════════
# 入口
# ════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(prog="superChat", description="superChat-PY CLI")
    sub    = parser.add_subparsers(dest="cmd")

    # chat
    p_chat = sub.add_parser("chat", help="发送消息")
    p_chat.add_argument("message", help="消息内容")
    p_chat.add_argument("--session", default="main", help="目标 session（默认 main）")

    # sessions
    sub.add_parser("sessions", help="列出所有 session")

    # history
    p_hist = sub.add_parser("history", help="查看消息历史")
    p_hist.add_argument("session", nargs="?", default="main")

    # reset
    p_reset = sub.add_parser("reset", help="重置 session 历史")
    p_reset.add_argument("session", nargs="?", default="main")

    # serve
    p_serve = sub.add_parser("serve", help="启动 Gateway")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    if args.cmd == "chat":
        asyncio.run(cmd_chat(args.message, args.session))
    elif args.cmd == "sessions":
        asyncio.run(cmd_sessions())
    elif args.cmd == "history":
        asyncio.run(cmd_history(args.session))
    elif args.cmd == "reset":
        asyncio.run(cmd_reset(args.session))
    elif args.cmd == "serve":
        cmd_serve(args.host, args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
