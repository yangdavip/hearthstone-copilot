"""
炉石助手 · 配置

配置来源优先级：环境变量 > config.yaml > 默认值。
LLM 走 OpenAI 兼容接口（国内模型均可），base_url / model / api_key 全部可在配置文件指定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


@dataclass
class LLMConfig:
    """OpenAI 兼容接口配置。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int = 1600
    timeout: float = 30.0
    #: 每次请求的重试次数
    max_retries: int = 2
    #: 校验器驳回方案时，是否把具体原因回传模型自动重写一次（最多一次）
    auto_correct: bool = True

    def masked_key(self) -> str:
        if not self.api_key:
            return "(未设置)"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


@dataclass
class WatchConfig:
    """实时监听行为。"""

    #: 轮到我方回合且处于可操作阶段时触发分析
    trigger_on_my_turn: bool = True
    #: 换牌阶段触发留牌建议
    trigger_on_mulligan: bool = False
    #: 分析结果是否语音播报
    voice: bool = False
    #: 两次分析之间的最小间隔（秒），避免同一回合反复触发
    cooldown: float = 3.0
    #: 是否启用整局规划：读套牌 → 生成跨回合战略 → 每个回合的建议服从它
    plan_enabled: bool = True


@dataclass
class PrivacyConfig:
    """发往大模型前的脱敏设置。"""

    #: 移除玩家名 / 名#编号 / 账号 ID / 时间戳 / 原始 tag / 本机路径 / 密钥
    redact: bool = True


def _merge_dicts(base: dict, overlay: dict) -> dict:
    """浅层递归合并：overlay 的标量覆盖 base，字典则继续向下合并。"""
    out = dict(base)
    for key, val in (overlay or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dicts(out[key], val)
        else:
            out[key] = val
    return out


@dataclass
class Config:
    """全局配置。"""

    #:炉石 Power.log 路径；None 表示自动定位最新日志
    log_path: str | None = None
    #: 自己对应的玩家编号（1 或 2）。多数情况为 1
    my_player_id: int = 1
    #: 本机玩家在炉石内的显示名（不含 #编号）。
    #: 玩家级 tag（法力等）挂在「名字#编号」实体上，而名字与 PlayerID 无算术关系。
    #: 名字会随对手/换边变化，**不适合做身份标识**，仅作辅助校验。
    player_name: str = ""
    #: 本机玩家的战网账号 ID ——日志里 `Player ... GameAccountId=[hi=.. lo=<此值>]` 的 lo 值。
    #: **这是唯一可靠的身份标识**：
    #:   - 玩家显示名会变（换对手、换边），不能用来定位自己
    #:   - my_player_id 也不可靠 —— 实测本机会在 PlayerID 1 与 2 之间来回换
    #:   - 而同一个账号的 lo 值在整份日志里恒定
    #: 用 `hs-copilot whoami --detect` 可自动探测该值。
    account_id: str = ""
    #: 套牌码（可选，填了才能做牌库推演与留牌建议）
    deckstring: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    cards_path: str = str(PROJECT_ROOT / "data" / "cards_zhCN.json")
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """从 YAML 载入配置，环境变量覆盖敏感项。

        ``config.local.yaml`` 与主配置同目录、被 ``.gitignore`` 忽略，
        用于存放 API Key、玩家名、账号 ID 等**不该进仓库**的值。它会在
        主配置之后合并覆盖，因此开源仓库里的 ``config.yaml`` 只留示例值，
        本机运行完全不受影响。
        """
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        local = p.parent / "config.local.yaml"
        if local.exists() and local.resolve() != p.resolve():
            overlay = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
            if isinstance(overlay, dict):
                raw = _merge_dicts(raw, overlay)

        llm_raw = raw.get("llm", {}) or {}
        watch_raw = raw.get("watch", {}) or {}
        privacy_raw = raw.get("privacy", {}) or {}

        # 卡牌数据路径：相对路径按「配置文件所在目录」解析，避免受 cwd 影响
        cards_raw = raw.get("cards_path") or ""
        if not cards_raw:
            cards_path = PROJECT_ROOT / "data" / "cards_zhCN.json"
        else:
            cp = Path(cards_raw)
            cards_path = (p.parent / cp).resolve() if not cp.is_absolute() else cp

        # 环境变量优先（便于 CI / 密钥不入库）
        api_key = (
            os.environ.get("HS_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or llm_raw.get("api_key", "")
            or ""
        )
        base_url = (
            os.environ.get("HS_LLM_BASE_URL") or llm_raw.get("base_url") or LLMConfig.base_url
        )
        model = os.environ.get("HS_LLM_MODEL") or llm_raw.get("model") or LLMConfig.model

        return cls(
            log_path=raw.get("log_path") or None,
            my_player_id=int(raw.get("my_player_id", 1)),
            player_name=(raw.get("player_name") or "").strip(),
            account_id=str(raw.get("account_id", "")).strip(),
            deckstring=(raw.get("deckstring") or "").strip(),
            cards_path=str(cards_path),
            llm=LLMConfig(
                base_url=base_url,
                api_key=api_key,
                model=model,
                temperature=float(llm_raw.get("temperature", 0.3)),
                max_tokens=int(llm_raw.get("max_tokens", 1600)),
                timeout=float(llm_raw.get("timeout", 30.0)),
                max_retries=int(llm_raw.get("max_retries", 2)),
                auto_correct=bool(llm_raw.get("auto_correct", True)),
            ),
            watch=WatchConfig(
                trigger_on_my_turn=bool(watch_raw.get("trigger_on_my_turn", True)),
                trigger_on_mulligan=bool(watch_raw.get("trigger_on_mulligan", False)),
                voice=bool(watch_raw.get("voice", False)),
                cooldown=float(watch_raw.get("cooldown", 3.0)),
                plan_enabled=bool(watch_raw.get("plan_enabled", True)),
            ),
            privacy=PrivacyConfig(
                redact=bool(privacy_raw.get("redact", True)),
            ),
            config_path=p,
        )


def find_latest_power_log() -> Path | None:
    """自动定位最新的 Power.log。

    炉石每次启动会新建 ``Logs/Hearthstone_<时间戳>/`` 目录，最新的即当前对局所在。
    """
    candidates = [
        Path("/Applications/Hearthstone/Logs"),
        Path.home() / "Library/Preferences/Blizzard/Hearthstone/Logs",
    ]
    logs: list[Path] = []
    for base in candidates:
        if not base.exists():
            continue
        for d in base.glob("Hearthstone_*"):
            f = d / "Power.log"
            if f.exists():
                logs.append(f)
        f = base / "Power.log"
        if f.exists():
            logs.append(f)
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)