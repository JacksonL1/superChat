"""
store/db.py
SQLite 连接池 + 建表。使用 aiosqlite 保持全程 async。
"""

import aiosqlite
import asyncio
from pathlib import Path
from typing import AsyncIterator
from contextlib import asynccontextmanager
from config import settings

DB_PATH = Path(settings.db_path)

_CREATE_TABLES = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Session 元数据
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL DEFAULT 'main',   -- main | planner | knowledge | executor
    status      TEXT NOT NULL DEFAULT 'idle',   -- idle | running | stopped
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    meta        TEXT NOT NULL DEFAULT '{}'       -- JSON 备用字段
);

-- 每条消息 / tool_call / tool_result
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,   -- system|user|assistant|tool
    content     TEXT,
    tool_calls  TEXT,            -- JSON
    tool_call_id TEXT,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    flags       INTEGER NOT NULL DEFAULT 0  -- REPLY_SKIP / ANNOUNCE_SKIP
);

-- 工作区文件 (TODO / NOTES / SUMMARY / ERRORS)
CREATE TABLE IF NOT EXISTS workspace (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    filename    TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, filename)
);

-- Agent 间消息路由记录（用于调试 / 审计）
CREATE TABLE IF NOT EXISTS agent_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_session TEXT NOT NULL,
    to_session   TEXT NOT NULL,
    content      TEXT NOT NULL,
    reply_to     TEXT,
    flags        INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    replied_at   TEXT
);


-- 审计日志：Agent 决策与执行路径
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_session_time ON audit_logs(session_id, created_at);

-- 向量记忆（简化版：SQLite + JSON embedding）
CREATE TABLE IF NOT EXISTS vector_memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vm_session_time ON vector_memories(session_id, created_at);

-- Skill 历史成功命令（skills.memory 模块使用）
-- 与 skills/memory.py 的 schema 完全一致
CREATE TABLE IF NOT EXISTS skill_memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name    TEXT NOT NULL,
    command       TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 1,
    last_used_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(skill_name, command)
);
CREATE INDEX IF NOT EXISTS idx_sm_skill ON skill_memory(skill_name);
"""


async def init_db() -> None:
    """建库建表 + migration，启动时调用一次。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_CREATE_TABLES)
        await db.commit()
        # migration：检查 skill_memory 是否有 success_count 列，没有则重建
        await _migrate_skill_memory(db)


async def _migrate_skill_memory(db: aiosqlite.Connection) -> None:
    """如果 skill_memory 表 schema 不对，直接 DROP 重建。"""
    async with db.execute("PRAGMA table_info(skill_memory)") as cur:
        cols = {row[1] async for row in cur}  # row[1] = column name
    if "success_count" not in cols or "last_used_at" not in cols:
        # 旧表 schema 不对，重建
        await db.executescript("""
            DROP TABLE IF EXISTS skill_memory;
            CREATE TABLE skill_memory (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name    TEXT NOT NULL,
                command       TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 1,
                last_used_at  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(skill_name, command)
            );
            CREATE INDEX IF NOT EXISTS idx_sm_skill ON skill_memory(skill_name);
        """)
        await db.commit()


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """获取数据库连接的 async context manager。"""
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")  # 等锁最多10秒
        yield db
