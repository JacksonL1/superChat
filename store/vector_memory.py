from __future__ import annotations

import json
import logging
import math
from typing import Iterable

from openai import AsyncOpenAI, BadRequestError

from config import settings
from store.db import get_db

logger = logging.getLogger(__name__)
_embedding_unavailable_reason: str | None = None


def _cosine_similarity(v1: Iterable[float], v2: Iterable[float]) -> float:
    a = list(v1)
    b = list(v2)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    n1 = math.sqrt(sum(x * x for x in a))
    n2 = math.sqrt(sum(y * y for y in b))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


async def build_embedding(client: AsyncOpenAI, text: str) -> list[float] | None:
    global _embedding_unavailable_reason

    if not settings.embedding_enabled or _embedding_unavailable_reason:
        return None

    payload = (text or "").strip()
    if not payload:
        return None

    payload = payload[: settings.embedding_max_chars]
    try:
        resp = await client.embeddings.create(
            model=settings.embedding_model,
            input=payload,
        )
        return list(resp.data[0].embedding)
    except BadRequestError as exc:
        _embedding_unavailable_reason = "bad_request"
        logger.warning(
            "Disable vector memory for this process because embedding request was rejected "
            "(model=%s). Set EMBEDDING_ENABLED=false or use an embedding-capable model/gateway. "
            "Detail: %s",
            settings.embedding_model,
            exc,
        )
        return None
    except Exception:
        return None


async def record_memory(
    session_id: str,
    role: str,
    content: str,
    embedding: list[float] | None,
) -> None:
    if not embedding:
        return

    async with get_db() as db:
        await db.execute(
            """INSERT INTO vector_memories (session_id, role, content, embedding)
               VALUES (?, ?, ?, ?)""",
            (session_id, role, content[:4000], json.dumps(embedding)),
        )
        await db.commit()


async def recall_memories(
    session_id: str,
    query_embedding: list[float] | None,
    limit: int | None = None,
) -> list[str]:
    if not query_embedding:
        return []

    top_k = limit or settings.embedding_max_memories

    async with get_db() as db:
        async with db.execute(
            """SELECT role, content, embedding
               FROM vector_memories
               WHERE session_id = ?
               ORDER BY id DESC
               LIMIT 300""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()

    scored: list[tuple[float, str]] = []
    for row in rows:
        try:
            emb = json.loads(row["embedding"])
        except Exception:
            continue
        score = _cosine_similarity(query_embedding, emb)
        if score >= settings.embedding_similarity_threshold:
            scored.append((score, f"[{row['role']}] {row['content']}"))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_k]]
