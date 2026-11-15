"""
炉石助手 · 发往大模型前的脱敏层

为什么需要：``Power.log`` 里的玩家显示名、``名#编号``、战网账号 lo、
日志时间戳与本机绝对路径，都属于本机个人信息。它们对战术判断毫无帮助
（payload 已经用「我方 / 对手」两个分组表达了身份），但会：

1. 把玩家真实 ID 发给第三方模型服务商；
2. 让模型靠名字猜「谁是我方」，反而干扰身份判定（这个名字与 PlayerID
   没有固定关系，先后手会换边）；
3. 把带时间戳、原始 tag 的日志行塞进上下文，浪费 token。

做法是**发送前**在字符串层做替换，不改动本地解析器与浮窗显示——
界面上仍然显示玩家自己的名字，只有出网的文本被脱敏。

参考同类项目 ``CuZaoo/hearthstone-agent-assistant`` 的脱敏层设计。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 中性占位符。区分本机/对手，保留「谁是谁」的语义但不暴露真名。
SELF = "[本机玩家]"
FOE = "[对手玩家]"

#: 结构化脱敏：这些模式与具体玩家无关，任何一份日志都适用。
#: 顺序有意义 —— 「名#编号」必须先于纯名替换，否则纯名替换后模式就断了。
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 玩家甲#12345 → [玩家#编号]
    (re.compile(r"[^\s\"'\[\],{}:=]+#\d+"), "[玩家#编号]"),
    # GameAccountId=[hi=144115200000000001 lo=100000001] → [账号]
    (re.compile(r"GameAccountId=\[hi=\d+\s+lo=\d+\]"), "[账号]"),
    # 任何 OpenAI/DeepSeek 形状的密钥（防止配置误入 payload）
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "[密钥]"),
    # 01:27:06.8446260 → [时间]
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?\b"), "[时间]"),
    # tag=EXHAUSTED value=1 → [游戏标签]。值里必须排除 JSON 结构字符，
    # 否则 \S+ 会把字符串结尾的 `",` 一起吃掉，脱敏后 JSON 直接解析失败。
    (re.compile(r"\btag=[A-Z_0-9]+\s+value=[^\s\"',\]}]+"), "[游戏标签]"),
    # /Users/xxx/... → [本机路径]
    (re.compile(r"/Users/[^\s\"'\]},]+"), "[本机路径]"),
    # /Applications/Hearthstone/Logs/... → [日志路径]
    (re.compile(r"/Applications/Hearthstone/Logs/[^\s\"'\]},]+"), "[日志路径]"),
]


def collect_secrets(recon: Any, cfg: Any) -> dict[str, str]:
    """从局面与配置里收集「需要精确替换的敏感串」。

    返回 ``原始串 -> 占位符``；调用方按长度降序替换，避免短串先被替换
    导致长串失配。
    """
    out: dict[str, str] = {}
    self_name = str(getattr(cfg, "player_name", "") or "").strip()
    if len(self_name) >= 2:
        out[self_name] = SELF

    names = getattr(recon, "player_names", None) or {}
    for raw in names.values():
        name = str(raw or "").rsplit("#", 1)[0].strip()
        if len(name) < 2:
            continue
        out.setdefault(name, SELF if name == self_name else FOE)

    for attr in ("current_player_name", "turn_owner_name"):
        raw = str(getattr(recon, attr, "") or "").rsplit("#", 1)[0].strip()
        if len(raw) >= 2:
            out.setdefault(raw, SELF if raw == self_name else FOE)

    account = str(getattr(cfg, "account_id", "") or "").strip()
    if account:
        out[account] = "[账号]"
    return out


def scrub(text: str, secrets: dict[str, str] | None = None) -> str:
    """对**一段即将发往模型**的文本做脱敏。

    ``secrets`` 为空时仍执行结构化脱敏（时间戳 / 原始 tag / 账号 / 路径 /
    密钥 / 名#编号），因此整局规划等不携带 recon 的调用也自动受保护。

    若文本本身是 JSON（prompt payload 就是），先解析、只替换字符串值、
    再序列化——保证任何替换规则都不会破坏 JSON 结构。
    """
    if not text:
        return text
    head = text.lstrip()
    if head.startswith(("{", "[")):
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if obj is not None:
            return json.dumps(_walk(obj, secrets or {}), ensure_ascii=False)
    return _scrub_text(text, secrets)


def _walk(obj: Any, secrets: dict[str, str]) -> Any:
    """递归只处理 JSON 的字符串值，结构字符一律不碰。"""
    if isinstance(obj, str):
        return _scrub_text(obj, secrets)
    if isinstance(obj, dict):
        return {k: _walk(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, secrets) for v in obj]
    return obj


def _scrub_text(text: str, secrets: dict[str, str] | None = None) -> str:
    """纯文本脱敏核心（调用方保证不在 JSON 结构层）。"""
    for pattern, placeholder in _PATTERNS:
        text = pattern.sub(placeholder, text)
    for raw, placeholder in sorted(
        (secrets or {}).items(), key=lambda kv: -len(kv[0])
    ):
        if raw:
            text = text.replace(raw, placeholder)
    return text


def scrub_messages(
    messages: list[dict[str, str]], secrets: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """批量脱敏；返回新列表，不修改调用方数据。"""
    return [
        {**m, "content": scrub(m.get("content", "") or "", secrets)}
        for m in messages
    ]
