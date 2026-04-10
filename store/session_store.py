"""
store/session_store.py
Session 元数据 + 消息历史的持久化读写。

写入优化：
  每个 session 有一个专属的 MessageWriter，内部维护单一持久连接 + asyncio.Queue。
  所有写操作排队串行执行，彻底消除多连接并发时的 SQLite 文件锁竞争。
  读操作仍用独立连接（读不争写锁，开销可接受）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiosqlite

from store.db import DB_PATH, get_db

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# MessageWriter：per-session 单连接写队列
# ════════════════════════════════════════════════════════════════

class MessageWriter:
    """
    每个 session 一个实例。
    内部持有一个持久 aiosqlite 连接，所有写操作通过 asyncio.Queue 串行化。
    不再每次写入都开关连接，彻底消除 SQLite 锁竞争。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task:  asyncio.Task | None = None
        self._conn:  aiosqlite.Connection | None = None

    async def start(self) -> None:
        self._conn = await aiosqlite.connect(DB_PATH, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        self._task = asyncio.create_task(
            self._worker(), name=f"writer-{self.session_id}"
        )

    async def stop(self) -> None:
        if self._task:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def write(self, coro_factory) -> None:
        fut = asyncio.get_event_loop().create_future()
        await self._queue.put((coro_factory, fut))
        await fut

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                break
            coro_factory, fut = item
            try:
                await coro_factory(self._conn)
                await self._conn.commit()
                fut.set_result(None)
            except Exception as e:
                log.error(f"[{self.session_id}] write error: {e}")
                if not fut.done():
                    fut.set_exception(e)


_writers: dict[str, MessageWriter] = {}


async def get_writer(session_id: str) -> MessageWriter:
    if session_id not in _writers:
        w = MessageWriter(session_id)
        await w.start()
        _writers[session_id] = w
    return _writers[session_id]


async def close_writer(session_id: str) -> None:
    w = _writers.pop(session_id, None)
    if w:
        await w.stop()


# ════════════════════════════════════════════════════════════════
# Session CRUD
# ════════════════════════════════════════════════════════════════

async def create_session(session_id: str, role: str = "main") -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO sessions (id, role) VALUES (?, ?)",
            (session_id, role),
        )
        await db.commit()


async def get_session(session_id: str) -> dict | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_sessions() -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def set_session_status(session_id: str, status: str) -> None:
    w = await get_writer(session_id)
    async def _do(conn):
        await conn.execute(
            "UPDATE sessions SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, session_id),
        )
    await w.write(_do)


# ════════════════════════════════════════════════════════════════
# Message 历史
# ════════════════════════════════════════════════════════════════

async def append_message(session_id: str, msg: dict, flags: int = 0) -> None:
    tool_calls_json = (
        json.dumps(msg.get("tool_calls"), ensure_ascii=False)
        if msg.get("tool_calls") else None
    )
    values = (
        session_id,
        msg.get("role", ""),
        msg.get("content"),
        tool_calls_json,
        msg.get("tool_call_id"),
        msg.get("name"),
        flags,
    )
    w = await get_writer(session_id)
    async def _do(conn):
        await conn.execute(
            """INSERT INTO messages
               (session_id, role, content, tool_calls, tool_call_id, name, flags)
               VALUES (?,?,?,?,?,?,?)""",
            values,
        )
    await w.write(_do)


async def load_history(session_id: str) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            """SELECT role, content, tool_calls, tool_call_id, name
               FROM messages WHERE session_id=? ORDER BY id ASC""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()

    history: list[dict] = []
    for row in rows:
        role = row["role"]
        m: dict[str, Any] = {"role": role}

        # content: assistant 纯工具调用时为 None，其他角色必须有内容
        if row["content"] is not None:
            m["content"] = row["content"]
        elif role == "assistant" and row["tool_calls"]:
            m["content"] = None  # OpenAI 协议允许 assistant content=null 时有 tool_calls
        elif role != "assistant":
            continue  # user/tool 消息没有 content 是损坏数据，跳过

        if row["tool_calls"]:
            try:
                m["tool_calls"] = json.loads(row["tool_calls"])
            except Exception:
                continue  # 损坏的 tool_calls JSON，跳过整条消息
        if row["tool_call_id"]:
            m["tool_call_id"] = row["tool_call_id"]
        if row["name"]:
            m["name"] = row["name"]
        history.append(m)
    return history


async def clear_history(session_id: str) -> None:
    w = await get_writer(session_id)
    async def _do(conn):
        await conn.execute(
            "DELETE FROM messages WHERE session_id=?", (session_id,)
        )
    await w.write(_do)