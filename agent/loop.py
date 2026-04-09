"""
agent/loop.py
单个 Agent 的 async 主循环。

每个 AgentLoop 实例对应一个 session，作为独立的 asyncio.Task 运行。
通过 MessageBus 的 inbox queue 接收消息，处理完后通过 bus.deliver_reply() 回复。

核心流程：
  1. 从 inbox 取出 AgentMessage
  2. 把消息内容追加到本 session 的 messages 历史
  3. 调用 LLM，处理工具调用循环
  4. sessions_send 工具调用 → 通过 MessageBus 发消息给其他 Agent，等回复
  5. 生成最终回复后，通过 bus.deliver_reply() 返回给调用方
  6. 如果 should_announce()，把结果推给用户 SSE
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from agent.executor import execute_tool
from agent.prompts import ROLE_PROMPT_BUILDERS
from agent.tools import TOOLS
from messaging.bus import MessageBus
from messaging.protocol import AgentMessage, Flags, MessageType
from store.session_store import (
    append_message,
    load_history,
    set_session_status,
)
from store.workspace import write_workspace_file
from store.session_store import list_sessions
from store.audit import log_audit_event
from store.vector_memory import build_embedding, recall_memories, record_memory

log = logging.getLogger(__name__)

# 工具白名单：各角色可以调用的工具
# main 只能协调，物理上无法调用执行类工具，强制走子 Agent
_ROLE_TOOLS: dict[str, list[str]] = {
    "main":      ["sessions_send", "sessions_list", "update_todo", "append_note", "read_workspace"],
    "planner":   [],
    "knowledge": ["load_skill", "list_skill_files", "read_file", "read_workspace"],
    "executor":  ["bash", "update_todo", "append_note", "read_file", "load_skill", "list_skill_files"],
}

_MAX_TOOL_ROUNDS = 20
_CHARS_PER_TOKEN = 2
_MAX_CTX_TOKENS  = 6000


class AgentLoop:
    """
    单个 Agent session 的执行引擎。
    由 SessionManager 创建，以 asyncio.Task 方式运行。
    """

    def __init__(
        self,
        session_id: str,
        role: str,
        bus: MessageBus,
        client: AsyncOpenAI,
        model: str,
        announce_callback=None,
        ensure_session_callback=None,
    ):
        self.session_id        = session_id
        self.role              = role
        self.bus               = bus
        self.client            = client
        self.model             = model
        self.announce_callback = announce_callback
        self.ensure_session_callback = ensure_session_callback
        self._inbox            = bus.register(session_id)
        self._stopped          = False
        self.ready             = asyncio.Event()  # task 进入主循环后 set

        # 过滤本角色可用的工具
        allowed = _ROLE_TOOLS.get(role, [])
        self._tools = [t for t in TOOLS if t["function"]["name"] in allowed] if allowed else []

    # ── 主循环：持续监听 inbox ────────────────────────────────────

    async def run(self) -> None:
        await asyncio.sleep(0)  # yield，让其他 task 先调度
        log.info(f"[{self.session_id}] loop started (role={self.role})")

        try:
            # ready.set() 必须在任何 DB 操作之前，避免 DB 锁导致 ready timeout
            self.ready.set()  # 通知 ensure_session：inbox 已就绪，可以接收消息

            try:
                await set_session_status(self.session_id, "running")
            except Exception as e:
                log.warning(f"[{self.session_id}] set_session_status failed: {e}")

            while not self._stopped:
                try:
                    msg: AgentMessage = await asyncio.wait_for(
                        self._inbox.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if msg.type == MessageType.SYSTEM and msg.content == "STOP":
                    break

                await self._handle_message(msg)

        finally:
            try:
                await set_session_status(self.session_id, "idle")
            except Exception:
                pass
            # 释放专属写连接
            try:
                from store.session_store import close_writer
                await close_writer(self.session_id)
            except Exception:
                pass
            log.info(f"[{self.session_id}] loop stopped")

    async def stop(self) -> None:
        self._stopped = True

    # ── 消息处理 ──────────────────────────────────────────────────

    async def _handle_message(self, incoming: AgentMessage) -> None:
        """处理一条入站消息，执行 LLM 循环，生成回复。"""
        log.info(f"[{self.session_id}({self.role})] ← [{incoming.from_session}] | {incoming.content}")

        await log_audit_event(self.session_id, "message_received", incoming.content, meta={"from": incoming.from_session, "role": self.role})

        # 子 Agent 收到空任务直接快速失败，不进入 LLM 循环
        if self.role != "main" and not incoming.content.strip():
            log.warning(f"[{self.session_id}] 收到空任务，跳过")
            if incoming.should_reply():
                reply = incoming.make_reply("FAILED: 收到空任务，请提供具体指令。")
                await self.bus.deliver_reply(reply)
                await log_audit_event(self.session_id, "empty_task", incoming.content, status="rejected")
            return

        system_content = await self._build_system_prompt(incoming.content)
        system_msg     = {"role": "system", "content": system_content}

        if self.role == "main":
            # main 保持完整对话历史，记住上下文
            # 先从 DB 加载历史，再在内存追加当前消息，不依赖 DB 读回写入结果
            user_msg = {"role": "user", "content": incoming.content}
            history  = await load_history(self.session_id)
            # 过滤掉历史里已有的相同内容（防止重复）
            messages = [system_msg] + history + [user_msg]
            # 异步写入 DB（不阻塞）
            asyncio.create_task(append_message(self.session_id, user_msg))
            log.info(f"[{self.session_id}] history loaded: {len(history)} messages, calling LLM...")
        else:
            # 子 Agent 每次无状态执行：只有 system + 当前任务，不带历史
            # 同时清空 ERRORS.md，避免上次失败记录拦截本次新任务的命令
            messages = [
                system_msg,
                {"role": "user", "content": incoming.content},
            ]
            try:
                await write_workspace_file(self.session_id, "ERRORS.md", "# Errors\n")
            except Exception:
                pass
            log.info(f"[{self.session_id}] stateless execution, calling LLM...")

        # 3. LLM 工具调用循环
        final_reply = await self._llm_loop(messages, incoming)
        log.info(f"[{self.session_id}] LLM done | reply={final_reply}")

        # 4. 持久化最终回复（fire-and-forget，不阻塞回复推送）
        reply_msg = {"role": "assistant", "content": final_reply}
        asyncio.create_task(append_message(self.session_id, reply_msg))
        await log_audit_event(self.session_id, "final_reply", final_reply, meta={"role": self.role})

        # 向量记忆：记录 user / assistant 内容
        user_emb = await build_embedding(self.client, incoming.content)
        await record_memory(self.session_id, "user", incoming.content, user_emb)
        ans_emb = await build_embedding(self.client, final_reply)
        await record_memory(self.session_id, "assistant", final_reply, ans_emb)

        # 5. 回复给发送方（如果需要）
        if incoming.should_reply():
            reply = incoming.make_reply(final_reply)
            await self.bus.deliver_reply(reply)
            log.info(f"[{self.session_id}] deliver_reply done")

        # 6. 推给用户 SSE（最终回复，带 is_final=True 标记）
        log.info(f"[{self.session_id}] announce final reply")
        if self.announce_callback:
            await self.announce_callback(
                self.session_id, final_reply,
                is_progress=False, is_final=True,
            )
            log.info(f"[{self.session_id}] announce done")

    # ── LLM 工具调用循环 ──────────────────────────────────────────

    async def _llm_loop(self, messages: list[dict], incoming: AgentMessage) -> str:
        """
        标准的 LLM → tool_call → 执行 → 追加结果 → 继续 循环。
        遇到 sessions_send 工具调用时，通过 MessageBus 真正发消息给其他 Agent。
        """
        for round_num in range(_MAX_TOOL_ROUNDS):

            kwargs = dict(
                model=self.model,
                messages=messages,
                max_tokens=4096,
                temperature=0.1,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            if self._tools:
                kwargs["tools"]       = self._tools
                kwargs["tool_choice"] = "auto"

            try:
                response = await self.client.chat.completions.create(**kwargs)
            except Exception as e:
                log.error(f"[{self.session_id}] LLM error: {e}")
                return f"LLM 请求失败: {e}"
            choices = getattr(response, "choices", None)
            if not choices:
                # 某些网关在鉴权失败/限流时会返回非标准结构，避免直接下标崩溃
                raw = ""
                try:
                    raw = response.model_dump_json(exclude_none=True)
                except Exception:
                    raw = str(response)
                log.error(f"[{self.session_id}] LLM empty choices, raw={raw}")
                return "LLM 返回空响应（choices 为空），请检查网关鉴权、模型名和配额。"

            msg = getattr(choices[0], "message", None)
            if msg is None:
                raw = ""
                try:
                    raw = response.model_dump_json(exclude_none=True)
                except Exception:
                    raw = str(response)
                log.error(f"[{self.session_id}] LLM missing message in first choice, raw={raw}")
                return "LLM 返回异常响应（message 为空），请检查网关返回格式。"

            tool_calls = self._parse_tool_calls(msg)

            log.info(f"[{self.session_id}] round={round_num} | text={repr((msg.content or ''))} | tools={[tc.function.name for tc in tool_calls]}")

            # 无工具调用 → 返回最终文字
            if not tool_calls:
                return self._clean(msg.content or "")

            # 追加 assistant 消息
            assistant_msg = {
                "role":    "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id, "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)
            asyncio.create_task(append_message(self.session_id, assistant_msg))

            # 执行每个工具
            for tc in tool_calls:
                try:
                    fn_args = json.loads(tc.function.arguments)
                except Exception:
                    fn_args = {}

                fn_name = tc.function.name
                log.info(f"[{self.session_id}] CALL {fn_name} | args={tc.function.arguments}")
                await log_audit_event(self.session_id, "tool_call", tc.function.arguments, meta={"tool": fn_name})

                # 推给前端：告知正在调用哪个工具
                if self.announce_callback:
                    preview = json.dumps(fn_args, ensure_ascii=False)
                    await self.announce_callback(
                        self.session_id,
                        f"🔧 {fn_name}({preview})",
                        is_progress=True, is_final=False,
                    )

                # ── sessions_send：真正的 Agent 间通信 ──────────
                if fn_name == "sessions_send":
                    result = await self._handle_sessions_send(fn_args, incoming)

                # ── sessions_list ────────────────────────────────
                elif fn_name == "sessions_list":
                    sessions = await list_sessions()
                    result   = json.dumps(sessions, ensure_ascii=False, indent=2)

                # ── 普通工具 ─────────────────────────────────────
                else:
                    result = await execute_tool(fn_name, fn_args, self.session_id)

                # 推给前端：工具执行结果摘要
                if self.announce_callback:
                    result_preview = (result or "").replace("\n", " ")
                    await self.announce_callback(
                        self.session_id,
                        f"  ↳ {result_preview}",
                        is_progress=True, is_final=False,
                    )

                log.info(f"[{self.session_id}] RESULT {fn_name} | {repr(result)}")
                await log_audit_event(self.session_id, "tool_result", str(result), meta={"tool": fn_name})
                tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result}
                messages.append(tool_msg)
                asyncio.create_task(append_message(self.session_id, tool_msg))

        return "已达到最大工具调用轮次，任务未完成。"

    # ── sessions_send 实现 ────────────────────────────────────────

    async def _handle_sessions_send(self, args: dict, incoming: AgentMessage) -> str:
        """
        真正的 Agent 间通信：
        1. 构造 AgentMessage 发给目标 session
        2. 等待回复（ping-pong）
        3. 返回回复内容给 LLM
        """
        to_session = args.get("to_session", "")
        message    = args.get("message", "")
        announce   = args.get("announce", False)

        if not to_session or not message:
            return "ERROR: to_session 和 message 不能为空"

        target_session, target_role = self._resolve_target_session(to_session)

        if self.ensure_session_callback and target_role:
            try:
                await self.ensure_session_callback(target_session, target_role)
            except Exception as e:
                return f"ERROR: 无法创建子 Agent 会话 {target_session}: {e}"

        log.info(f"[{self.session_id}] -> [{target_session}] task={repr(message)}")
        await log_audit_event(self.session_id, "delegate_task", message, meta={"to": target_session})

        flags = Flags.NONE
        if not announce:
            flags |= Flags.ANNOUNCE_SKIP

        outgoing = AgentMessage(
            from_session = self.session_id,
            to_session   = target_session,
            content      = message,
            type         = MessageType.TASK,
            flags        = flags,
            reply_to     = self.session_id,
        )

        log.info(f"[{self.session_id}] → [{target_session}]: {message}")

        # 推给用户：告知正在调用子 Agent
        if self.announce_callback:
            await self.announce_callback(
                self.session_id,
                f"[调用 {target_session}] {message}",
                is_progress=True,
            )

        # 发消息并等待回复
        reply = await self.bus.send(outgoing, wait_reply=True, reply_timeout=120.0)

        if reply is None:
            return f"ERROR: [{target_session}] 未在超时时间内回复"

        log.info(f"[{self.session_id}] ← [{target_session}]: {reply.content}")
        await log_audit_event(self.session_id, "delegate_result", reply.content, meta={"from": target_session})
        return reply.content

    def _resolve_target_session(self, requested: str) -> tuple[str, str | None]:
        """
        把逻辑角色名（planner/knowledge/executor）映射为当前会话命名空间内的子 session。
        例如：userA 主会话调用 planner -> userA::planner。
        """
        role = requested.strip()
        role_set = {"planner", "knowledge", "executor"}
        if role in role_set:
            root = self._session_root()
            return f"{root}::{role}", role

        # 已经是 namespaced 形式（如 userA::planner）时，自动识别角色
        if "::" in role:
            maybe_role = role.rsplit("::", 1)[-1].strip()
            if maybe_role in role_set:
                return role, maybe_role

        return role, None

    def _session_root(self) -> str:
        if "::" in self.session_id:
            return self.session_id.split("::", 1)[0]
        return self.session_id

    # ── 辅助 ──────────────────────────────────────────────────────

    async def _build_system_prompt(self, query_text: str = "") -> str:
        builder = ROLE_PROMPT_BUILDERS.get(self.role)
        if builder is None:
            return f"你是 Agent [{self.session_id}]，角色：{self.role}"

        if self.role != "main":
            return builder(self.session_id)

        # 直接调用原有 skills.loader，格式模型已知
        try:
            from skills.loader import build_skills_xml, scan_skills
            from skills.memory import build_all_memory_hints

            skills = scan_skills()
            skills_xml = build_skills_xml(skills)
            skill_names = [s["name"] for s in skills]
            memory_hint = build_all_memory_hints(skill_names)

            # 向量记忆召回
            query = query_text or f"session={self.session_id} role={self.role}"
            query_emb = await build_embedding(self.client, query)
            recalled = await recall_memories(self.session_id, query_emb)
            if recalled:
                memory_hint += "\n\n## 向量记忆召回\n" + "\n".join(f"- {x}" for x in recalled)

            log.info(f"[{self.session_id}] skills loaded: {len(skills)}")
        except Exception as e:
            log.error(f"[{self.session_id}] skills.loader 失败: {e}", exc_info=True)
            skills_xml = ""
            memory_hint = ""

        return builder(self.session_id, skills_xml=skills_xml, memory_hint=memory_hint)

    def _clean(self, content: str) -> str:
        return re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=re.DOTALL).strip()

    def _parse_tool_calls(self, msg) -> list:
        tool_calls = msg.tool_calls or []
        if not tool_calls and msg.content:
            for block in re.findall(r"<tool_call>(.*?)</tool_call>", msg.content, re.DOTALL):
                block = block.strip()
                s, e  = block.find("{"), block.rfind("}")
                if s == -1 or e == -1:
                    continue
                try:
                    obj  = json.loads(block[s:e+1])
                    name = obj.get("name") or (obj.get("function") or {}).get("name", "")
                    args = obj.get("arguments") or obj.get("parameters") or {}
                    if isinstance(args, str):
                        try:    args = json.loads(args)
                        except: args = {}
                    if name:
                        class _Fn:
                            def __init__(self, n, a):
                                self.name      = n
                                self.arguments = json.dumps(a, ensure_ascii=False)
                        class _TC:
                            def __init__(self, n, a):
                                self.id       = f"fallback_{uuid.uuid4().hex[:8]}"
                                self.function = _Fn(n, a)
                        tool_calls.append(_TC(name, args))
                except Exception:
                    continue
        return tool_calls
