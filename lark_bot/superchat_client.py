"""
superchat_client.py
对 Gateway 的封装，使用与 cli 相同的 SSE 模式：
  1. POST /chat        投入队列，立即返回
  2. GET  /stream/...  SSE 订阅，实时获取进度和最终回复

相比 /chat/sync，这种方式不存在 HTTP 超时问题，
进度可以实时推给飞书卡片。
"""

import json
import logging
from typing import Callable

import requests

from config import settings

log = logging.getLogger(__name__)


class SuperChatClient:

    def __init__(self):
        self.base_url = settings.superchat_url.rstrip("/")
        self.timeout  = settings.request_timeout

    def _auth_headers(self) -> dict:
        token = (settings.superchat_access_token or "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def chat_stream(
        self,
        message:     str,
        sender_id:   str,
        session_id:  str = "main",
        on_progress: Callable[[str], None] | None = None,
        on_final:    Callable[[str], None] | None = None,
        on_error:    Callable[[str], None] | None = None,
    ) -> None:
        """
        流式调用（在当前线程阻塞直到收到 final 回复）。

        on_progress(text) : 收到进度通知时调用（工具调用过程）
        on_final(text)    : 收到最终回复时调用
        on_error(text)    : 发生错误时调用
        """
        effective_session = sender_id if sender_id else session_id

        # 1. 投递消息
        try:
            resp = requests.post(
                f"{self.base_url}/chat",
                json={"message": message, "session_id": effective_session},
                timeout=10,
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
        except Exception as e:
            if on_error:
                on_error(f"❌ 无法连接 Gateway：{e}")
            return

        # 2. SSE 订阅进度和最终回复
        try:
            with requests.get(
                f"{self.base_url}/stream/{effective_session}",
                stream=True,
                timeout=self.timeout,
                headers=self._auth_headers(),
            ) as stream:
                for line in stream.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue

                    text = payload.get("text", "")
                    if not text:
                        continue

                    if payload.get("progress"):
                        if on_progress:
                            on_progress(text)
                        continue

                    if payload.get("final"):
                        if on_final:
                            on_final(text)
                        return

        except requests.Timeout:
            if on_error:
                on_error("⏱ 响应超时，请稍后重试。")
        except Exception as e:
            if on_error:
                on_error(f"❌ 请求异常：{e}")


superchat = SuperChatClient()