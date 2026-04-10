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
from store.session_store import get_session_meta, patch_session_meta
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

_MAX_TOOL_ROUNDS = 200
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

        # ── bash 白名单问询：若存在待审批命令，优先处理用户回复 ─────────
        if self.role == "main":
            direct = await self._maybe_handle_bash_approval(incoming)
            if direct is not None:
                # 直接回复，不进入 LLM 循环
                if incoming.should_reply():
                    reply = incoming.make_reply(direct)
                    await self.bus.deliver_reply(reply)
                if self.announce_callback:
                    await self.announce_callback(
                        self.session_id, direct,
                        is_progress=False, is_final=True,
                    )
                return

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

    async def _maybe_handle_bash_approval(self, incoming: AgentMessage) -> str | None:
        """
        若 session.meta 存在 pending_bash_approval，则把用户输入解析为：
        - 仅本次允许 / 永久允许 / 拒绝
        并在允许时自动继续执行之前被拦截的命令。
        返回要直接回复用户的文本；若不处理则返回 None。
        """
        root_session = self._session_root()
        meta = await get_session_meta(root_session)
        pending = meta.get("pending_bash_approval")
        if not isinstance(pending, dict):
            return None

        entry = str(pending.get("entry") or "").strip().lower()
        command = str(pending.get("command") or "").strip()
        timeout = int(pending.get("timeout") or 60)
        origin_session = str(pending.get("origin_session") or root_session).strip() or root_session
        origin_task_message = str(pending.get("origin_task_message") or "").strip()
        if not entry or not command:
            await patch_session_meta(root_session, {"pending_bash_approval": None})
            return None

        text = (incoming.content or "").strip().lower()
        if not text:
            return None

        allow_once = text in {"1", "y", "yes", "是", "好", "允许", "同意", "本次", "仅本次", "仅本次使用", "一次", "这次"}
        allow_forever = text in {"2", "forever", "always", "永久", "永久允许", "一直", "总是", "加入白名单", "永久使用"}
        deny = text in {"0", "n", "no", "否", "不", "拒绝", "取消", "不允许"}

        if not (allow_once or allow_forever or deny):
            return self._bash_approval_prompt(entry, command, title="检测到上一条命令需要白名单授权。")

        await patch_session_meta(root_session, {"pending_bash_approval": None})
        if deny:
            await log_audit_event(self.session_id, "bash_approval", f"deny: {command}", status="rejected", meta={"entry": entry})
            return "已取消执行该命令。"

        # 允许：更新临时白名单；永久允许则写入动态白名单文件
        from agent.executor import temp_allowlist_add, add_dynamic_allowlist_entry

        temp_allowlist_add(origin_session, entry)
        if allow_forever:
            add_dynamic_allowlist_entry(entry)

        await log_audit_event(
            self.session_id,
            "bash_approval",
            f"allow({'forever' if allow_forever else 'once'}): {command}",
            meta={"entry": entry},
        )

        # 自动继续执行此前被拦截的命令（用户已明确授权）
        result = await execute_tool("bash", {"command": command, "timeout": timeout}, origin_session)
        header = f"已{'永久' if allow_forever else '仅本次'}允许 `{entry}`，现在继续执行：`{command}`\n\n{result}"

        # 授权后自动“继续原任务”：若拦截发生在子会话（如 main::executor），就把原任务重新投递一次
        if origin_session != root_session and origin_task_message:
            try:
                flags = Flags.ANNOUNCE_SKIP
                resume_message = (
                    "继续刚才被白名单授权打断的任务。\n\n"
                    "原任务如下：\n"
                    f"{origin_task_message}\n\n"
                    "已执行并授权的命令：\n"
                    f"- 入口命令：{entry}\n"
                    f"- 完整命令：{command}\n\n"
                    "该命令的执行结果如下：\n"
                    f"{result}\n\n"
                    "继续要求：\n"
                    "- 不要重复执行上面这条命令。\n"
                    "- 如果该命令已经失败，请读取 ERRORS.md，并改用其他方法继续任务。\n"
                    "- 只基于当前结果继续后续步骤。"
                )
                outgoing = AgentMessage(
                    from_session=root_session,
                    to_session=origin_session,
                    content=resume_message,
                    type=MessageType.TASK,
                    flags=flags,
                    reply_to=root_session,
                )
                reply = await self.bus.send(outgoing, wait_reply=True, reply_timeout=120.0)
                if reply is None:
                    return header + "\n\n（已尝试继续原任务，但子会话超时未回复）"
                return header + "\n\n继续原任务结果：\n" + (reply.content or "")
            except Exception as e:
                return header + f"\n\n（继续原任务失败：{e}）"

        return header

    def _bash_approval_prompt(self, entry: str, command: str, title: str = "检测到命令不在白名单中，需要你授权后才能执行。") -> str:
        return (
            f"{title}\n"
            f"- 命令入口：`{entry}`\n"
            f"- 完整命令：`{command}`\n\n"
            "请回复：\n"
            "- `1`：仅本次允许\n"
            "- `2`：永久允许（写入动态白名单）\n"
            "- `0`：拒绝执行"
        )

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

                # ── bash 白名单问询：中断工具循环，转为用户交互 ─────────
                if isinstance(result, str) and result.startswith("ERROR: NEED_BASH_APPROVAL:"):
                    m = re.search(r"entry=([a-z0-9._-]+)\s+command=(.*)$", result, flags=re.I)
                    if m:
                        entry = m.group(1).strip().lower()
                        cmd = m.group(2).strip()
                        root_session = self._session_root()
                        await patch_session_meta(root_session, {
                            "pending_bash_approval": {
                                "entry": entry,
                                "command": cmd,
                                "timeout": int(fn_args.get("timeout", 60)),
                                "origin_session": self.session_id,
                                # 用于授权后“继续原任务”
                                "origin_task_message": incoming.content,
                            }
                        })
                        await log_audit_event(self.session_id, "bash_approval_needed", cmd, status="blocked", meta={"entry": entry})
                        return self._bash_approval_prompt(entry, cmd)

                # ── 强制直出：只要根会话存在 pending，就直接提示用户授权 ──
                if self.role == "main":
                    root_session = self._session_root()
                    meta = await get_session_meta(root_session)
                    pending = meta.get("pending_bash_approval")
                    if isinstance(pending, dict):
                        entry = str(pending.get("entry") or "").strip().lower()
                        cmd = str(pending.get("command") or "").strip()
                        if entry and cmd:
                            return self._bash_approval_prompt(entry, cmd)

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
