from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FilterResult:
    allowed: bool
    risk_score: int
    reason: str = ""


_PROMPT_INJECTION_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"ignore\s+(all|previous)\s+instructions", re.I), 50, "疑似提示词越权指令"),
    (re.compile(r"you\s+are\s+now\s+(system|developer)", re.I), 45, "疑似角色劫持指令"),
    (re.compile(r"reveal\s+(the\s+)?system\s+prompt", re.I), 40, "疑似敏感提示词泄露请求"),
]

_DANGEROUS_COMMAND_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"rm\s+-rf", re.I), 60, "疑似危险删除命令"),
    (re.compile(r"curl\s+[^\n]*\|\s*sh", re.I), 60, "疑似远程脚本执行命令"),
    (re.compile(r"wget\s+[^\n]*\|\s*sh", re.I), 60, "疑似远程脚本执行命令"),
    (re.compile(r"(cat|sed|awk)\s+[^\n]*(/etc/passwd|\.ssh|id_rsa)", re.I), 55, "疑似敏感文件读取行为"),
]

_EMAIL_STYLE_ATTACK_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"from:\s*security\s+team", re.I), 20, "疑似伪造邮件社工模板"),
    (re.compile(r"urgent\s+action\s+required", re.I), 20, "疑似高压诱导内容"),
    (re.compile(r"click\s+the\s+link\s+below", re.I), 20, "疑似钓鱼链接诱导"),
]


def inspect_external_input(content: str, risk_threshold: int = 60) -> FilterResult:
    text = (content or "").strip()
    if not text:
        return FilterResult(allowed=False, risk_score=100, reason="输入为空")

    score = 0
    reasons: list[str] = []

    for pattern, weight, reason in (
        _PROMPT_INJECTION_PATTERNS
        + _DANGEROUS_COMMAND_PATTERNS
        + _EMAIL_STYLE_ATTACK_PATTERNS
    ):
        if pattern.search(text):
            score += weight
            reasons.append(reason)

    # 超长输入通常伴随注入载荷，额外提高风险分
    if len(text) > 12000:
        score += 20
        reasons.append("输入异常过长")

    if score >= risk_threshold:
        reason = "；".join(sorted(set(reasons))) or "检测到高风险外部输入"
        return FilterResult(allowed=False, risk_score=score, reason=reason)

    return FilterResult(allowed=True, risk_score=score)
