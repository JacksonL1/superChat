from __future__ import annotations

import json
from typing import Any

from store.db import get_db


async def log_audit_event(
    session_id: str,
    event_type: str,
    detail: str,
    status: str = "ok",
    meta: dict[str, Any] | None = None,
) -> None:
    """记录 Agent 决策与执行路径，供审计追溯。"""
    async with get_db() as db:
        await db.execute(
            """INSERT INTO audit_logs (session_id, event_type, detail, status, meta)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session_id,
                event_type,
                detail[:4000],
                status,
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        await db.commit()
