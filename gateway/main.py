"""
gateway/main.py
FastAPI 入口：HTTP API + SSE 流式输出。

接口：
  POST /chat                    ← 发消息给 main session
  GET  /stream/{session_id}     ← SSE 订阅 session 输出
  GET  /sessions                ← 查看所有 session
  POST /sessions/{id}/reset     ← 重置 session 历史
  GET  /sessions/{id}/history   ← 查看 session 消息历史
  GET  /health                  ← 健康检查
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel
from config import settings
from gateway.auth import require_auth
from security.input_filter import inspect_external_input
from gateway.session_manager import SessionManager
from store.session_store import load_history, clear_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── 全局 SessionManager（lifespan 里初始化）──────────────────────
_session_manager: SessionManager | None = None


def get_manager() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError("SessionManager not initialized")
    return _session_manager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _session_manager

    headers = settings.sglang_headers_dict.copy()
    api_key = settings.effective_api_key
    # 兼容需要 Bearer Token 的网关（例如 ModelScope OpenAI 兼容接口）
    if api_key != "EMPTY" and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=settings.sglang_base_url,
        )
    if api_key == "EMPTY":
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=settings.sglang_base_url,
            default_headers=headers,
        )
        log.warning("SGLANG_API_KEY / MODELSCOPE_API_TOKEN 未设置，将以无鉴权模式启动。")
    log.info(f"headers: {headers}, url: {settings.sglang_base_url}, model: {settings.sglang_model}")

    # 模型名：优先用环境变量，否则从 SGLang /v1/models 获取
    # 用 httpx 直接请求避免 OpenAI SDK 的 Method Not Allowed 问题
    model = settings.sglang_model

    _session_manager = SessionManager(client=client, model=model)
    await _session_manager.startup()
    log.info("Gateway started")

    yield

    await _session_manager.shutdown()
    log.info("Gateway shutdown")


app = FastAPI(title="OpenClaw-PY Gateway", lifespan=lifespan)


# ════════════════════════════════════════════════════════════════
# 请求 / 响应模型
# ════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str
    session_id: str = "main"  # 默认发给 main session


class ChatResponse(BaseModel):
    session_id: str
    status: str = "queued"


# ════════════════════════════════════════════════════════════════
# 路由
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health(_claims: dict = Depends(require_auth)):
    mgr = get_manager()
    return {
        "status": "ok",
        "sessions": mgr.get_running_sessions(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _claims: dict = Depends(require_auth)):
    """异步接口：投入队列立即返回，结果通过 SSE 推送。"""
    scan = inspect_external_input(req.message)
    if not scan.allowed:
        raise HTTPException(status_code=400, detail=f"输入风控拦截（score={scan.risk_score}）：{scan.reason}")

    mgr = get_manager()
    await mgr.send_to_session(req.session_id, req.message)
    return ChatResponse(session_id=req.session_id)


class SyncChatRequest(BaseModel):
    message: str
    session_id: str = "main"
    sender_id: str = ""
    timeout: float = 300.0


class SyncChatResponse(BaseModel):
    session_id: str
    reply: str
    progress: list[str] = []  # 中间步骤日志，供前端展示


@app.post("/chat/sync", response_model=SyncChatResponse)
async def chat_sync(req: SyncChatRequest, _claims: dict = Depends(require_auth)):
    """
    同步接口：发消息后阻塞等待 AgentLoop 完成，直接返回回复文字。
    飞书 bot 等需要同步回复的调用方使用此接口。
    sender_id 不为空时用 sender_id 作为 session_id，实现多用户隔离。
    """
    import time
    scan = inspect_external_input(req.message)
    if not scan.allowed:
        raise HTTPException(status_code=400, detail=f"输入风控拦截（score={scan.risk_score}）：{scan.reason}")

    mgr = get_manager()
    effective_session = req.sender_id if req.sender_id else req.session_id
    await mgr.ensure_session(effective_session, role="main")

    # 先订阅再发消息，避免漏掉回复
    q = mgr.subscribe_sse(effective_session)
    try:
        await mgr.send_to_session(effective_session, req.message)
        log.info(f"[chat/sync] message sent to {effective_session}, waiting for reply (timeout={req.timeout}s)")

        progress_logs: list[str] = []
        deadline = time.monotonic() + req.timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                payload = await asyncio.wait_for(q.get(), timeout=min(remaining, 30.0))
            except asyncio.TimeoutError:
                continue

            log.info(
                f"[chat/sync] got payload: progress={payload.get('progress')} text={str(payload.get('text', ''))[:60]}")

            if payload.get("progress"):
                # 中间步骤，收集后继续等
                step = payload.get("text", "")
                if step:
                    progress_logs.append(step)
                continue

            if not payload.get("final"):
                # 非 final 的文字（如 main 的中间思考）也收集为 progress
                step = payload.get("text", "")
                if step:
                    progress_logs.append(step)
                continue

            # final=True 才是真正的最终回复
            reply = payload.get("text", "")
            if reply:
                log.info(f"[chat/sync] returning final reply: {reply[:80]}")
                return SyncChatResponse(
                    session_id=effective_session,
                    reply=reply,
                    progress=progress_logs,
                )

        log.warning(f"[chat/sync] timeout for session {effective_session}")
        return SyncChatResponse(
            session_id=effective_session,
            reply="⏱ 请求超时，Agent 可能正在执行耗时任务，请稍后重试。",
            progress=progress_logs,
        )
    finally:
        mgr.unsubscribe_sse(effective_session, q)


@app.get("/stream/{session_id}")
async def stream(session_id: str, request: Request, _claims: dict = Depends(require_auth)):
    """
    SSE 订阅：实时接收 session 的输出。
    每条 SSE 事件格式：data: {"session_id": "...", "text": "...", "progress": bool}
    连接断开时自动取消订阅。
    """
    mgr = get_manager()
    q = mgr.subscribe_sse(session_id)

    async def event_generator() -> AsyncIterator[str]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳，保持连接
                    yield ": ping\n\n"
        finally:
            mgr.unsubscribe_sse(session_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/sessions")
async def get_sessions(_claims: dict = Depends(require_auth)):
    mgr = get_manager()
    return await mgr.get_all_sessions()


@app.get("/sessions/{session_id}/history")
async def get_history(session_id: str, _claims: dict = Depends(require_auth)):
    history = await load_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.post("/sessions/{session_id}/reset")
async def reset_session(session_id: str, _claims: dict = Depends(require_auth)):
    await clear_history(session_id)
    return {"session_id": session_id, "status": "reset"}
