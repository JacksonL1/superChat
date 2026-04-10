"""
agent/executor.py
工具执行层（async 版）。
保留原有全部工具语义，改为 async，工作区读写改走 store/workspace.py。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import re
import shlex
import shutil
from pathlib import Path

from store.workspace import (
    read_workspace_file,
    update_todo,
    append_note,
    record_error,
    has_failed_before,
)
from skills.loader import load_skill_content  # 保留原有 skill 加载
from config import settings
from store.audit import log_audit_event


async def execute_tool(name: str, args: dict, session_id: str) -> str:
    """
    统一工具执行入口。
    所有工具调用都需要传入 session_id（工作区隔离）。
    """

    # ── load_skill ────────────────────────────────────────────────
    if name == "load_skill":
        path = args.get("skill_path", "")
        # load_skill_content 是同步的，放到线程池
        return await asyncio.to_thread(load_skill_content, path)

    # ── list_skill_files ──────────────────────────────────────────
    elif name == "list_skill_files":
        skill_name = args.get("skill_name", "")
        return await asyncio.to_thread(_list_skill_files, skill_name)

    # ── bash ──────────────────────────────────────────────────────
    elif name == "bash":
        command = args.get("command", "").strip()
        timeout = int(args.get("timeout", 60))

        if sys.platform == "win32":
            command = re.sub(r"\bpython3\b", "python", command)

        check_error = _validate_bash_command(command)
        if check_error:
            return f"ERROR: {check_error}"

        try:
            cmd_parts = shlex.split(command, posix=(sys.platform != "win32"))
        except ValueError as e:
            return f"ERROR: 命令解析失败：{e}"

        if not cmd_parts:
            return "ERROR: 命令为空"

        arg_error = _validate_bash_arguments(cmd_parts)
        if arg_error:
            return f"ERROR: {arg_error}"

        # 检查是否曾经失败
        if await has_failed_before(session_id, command):
            return (
                "ERROR: 该命令在本次会话中已失败过，"
                "请调用 read_workspace('ERRORS.md') 查看失败详情，换一种方式。"
            )

        try:
            await log_audit_event(session_id, "bash_start", command, meta={"timeout": timeout})
            proc = await _spawn_bash_process(cmd_parts)
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await log_audit_event(session_id, "bash_timeout", command, status="timeout")
                return f"ERROR: 命令超时（>{timeout}s）"

            output = stdout.decode("utf-8", errors="replace").strip()
            if stderr:
                output += f"\n[STDERR]\n{stderr.decode('utf-8', errors='replace').strip()}"
            if proc.returncode != 0:
                output += f"\n[返回码: {proc.returncode}]"
                await record_error(session_id, command, output)
                await log_audit_event(
                    session_id,
                    "bash_result",
                    output,
                    status="failed",
                    meta={"returncode": proc.returncode},
                )
            else:
                await log_audit_event(session_id, "bash_result", output or "(执行成功，无输出)", status="ok")

            return output or "(执行成功，无输出)"

        except Exception as e:
            err = f"ERROR: {e}"
            await record_error(session_id, command, err)
            await log_audit_event(session_id, "bash_exception", err, status="error")
            return err

    # ── read_file ─────────────────────────────────────────────────
    elif name == "read_file":
        path = args.get("path", "")
        try:
            return await asyncio.to_thread(Path(path).read_text, encoding="utf-8", errors="replace")
        except Exception as e:
            return f"ERROR: {e}"

    # ── update_todo ───────────────────────────────────────────────
    elif name == "update_todo":
        return await update_todo(session_id, args.get("content", ""))

    # ── append_note ───────────────────────────────────────────────
    elif name == "append_note":
        return await append_note(session_id, args.get("note", ""))

    # ── read_workspace ────────────────────────────────────────────
    elif name == "read_workspace":
        filename = args.get("file", "").strip()
        return await read_workspace_file(session_id, filename)

    # ── sessions_send（Agent 间通信工具）─────────────────────────
    # 实际实现在 AgentLoop 里，executor 只做占位说明
    elif name == "sessions_send":
        return "ERROR: sessions_send 必须通过 AgentLoop 调用，不经过 executor"

    return f"ERROR: 未知工具 '{name}'"


def _list_skill_files(skill_name: str) -> str:
    """同步辅助，跑在线程池里。"""
    from config import settings
    skill_dir = Path(settings.skills_dir) / skill_name
    if not skill_dir.exists():
        hits = [
            d for d in Path(settings.skills_dir).iterdir()
            if d.is_dir() and skill_name.lower() in d.name.lower()
        ]
        if not hits:
            return f"ERROR: 找不到 skill: {skill_name}"
        skill_dir = hits[0]
    files = sorted(f.resolve() for f in skill_dir.rglob("*") if f.is_file())
    return "完整路径列表：\n" + "\n".join(str(f) for f in files)




async def _spawn_bash_process(cmd_parts: list[str]) -> asyncio.subprocess.Process:
    workspace_root = str(Path(settings.bash_workspace_root).resolve())

    if settings.executor_sandbox_mode == "docker":
        if shutil.which("docker") is None:
            raise RuntimeError("docker 不可用，无法在沙箱中执行命令")

        docker_cmd = [
            "docker", "run", "--rm",
            "--network", settings.executor_sandbox_network,
            "-v", f"{workspace_root}:{settings.executor_sandbox_workdir}",
            "-w", settings.executor_sandbox_workdir,
            settings.executor_sandbox_image,
            *cmd_parts,
        ]
        return await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    if settings.executor_sandbox_mode == "host":
        return await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_root,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    raise RuntimeError(f"不支持的 executor_sandbox_mode: {settings.executor_sandbox_mode}")


def _validate_bash_arguments(parts: list[str]) -> str | None:
    """参数级沙箱校验：限制数量、长度、路径越界和高风险参数。"""
    if len(parts) - 1 > settings.bash_max_args:
        return f"参数过多（>{settings.bash_max_args}）"

    workspace_root = Path(settings.bash_workspace_root).resolve()

    for arg in parts[1:]:
        if len(arg) > settings.bash_max_arg_length:
            return f"参数过长：{arg[:60]}..."

        # flag 只允许基本字符，阻断非常规注入载荷
        if arg.startswith("-"):
            if not re.match(r"^-{1,2}[a-zA-Z0-9][a-zA-Z0-9_-]*$", arg):
                return f"非法参数标记：{arg}"
            continue

        lowered = arg.lower()
        forbidden_substrings = ["..", "~", "/etc", "/proc", "/sys", "/dev", ".ssh", "id_rsa", ".env"]
        if any(x in lowered for x in forbidden_substrings):
            return f"参数包含高风险路径片段：{arg}"

        # 看起来像路径时，限制在 workspace 根目录内
        if "/" in arg or "\\" in arg or arg.startswith("."):
            path = Path(arg).expanduser()
            candidate = path.resolve() if path.is_absolute() else (workspace_root / path).resolve()
            try:
                candidate.relative_to(workspace_root)
            except ValueError:
                return f"路径越界，禁止访问工作区外部：{arg}"

    return None


def _validate_bash_command(command: str) -> str | None:
    """命令层白名单与沙箱策略检查。返回错误文案或 None。"""
    if not command:
        return "命令为空"

    normalized = command.lower()
    blocked_patterns = [
        p.strip().lower()
        for p in settings.bash_blocked_patterns.split(",")
        if p.strip()
    ]
    for p in blocked_patterns:
        if p in normalized:
            return f"命中危险命令黑名单：{p}"

    if not settings.bash_allow_shell_operators:
        if re.search(r"[;&|`]|\$\(|<|>", command):
            return "不允许 shell 操作符（; & | ` $( ) < >）"

    try:
        parts = shlex.split(command, posix=(sys.platform != "win32"))
    except ValueError as e:
        return f"命令解析失败：{e}"

    if not parts:
        return "命令为空"

    entry = Path(parts[0]).name.lower()
    allowed = {
        c.strip().lower()
        for c in settings.bash_allowed_commands.split(",")
        if c.strip()
    }
    if entry not in allowed:
        return f"命令 '{entry}' 不在白名单中"

    return None