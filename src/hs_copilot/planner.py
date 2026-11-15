"""
炉石助手 · 整局规划（Game Plan）

解决的问题：旧版每回合都把「当前手牌 + 当前场面」丢给模型，模型只能做
**单回合局部最优**，于是会把任务牌、终结手段、仅有的 AOE / 变形解牌随手打掉。
实测「超级进化萨」里模型建议用「妖术」变形一个普通大随从，而这副牌的
胜负手是「群山之灵任务 + 铺场进化 + 兆示减费死亡之翼」，妖术必须留给关键威胁。

本模块把决策拆成两层（借鉴 HearthCoach 的「LLM 慢规划 + 规则快评分」）：

* **慢层**：每局只做 1 次（阶段推进时最多再评 2 次），读完整套牌码解出的
  30/40 张中文卡表，让模型产出跨回合的「整局计划」：套牌定位、致胜条件、
  前/中/后期目标、核心卡与用途、资源纪律、斩杀组件、留牌优先级。
* **快层**：每个我方回合照旧请求模型，但 payload 里恒定带着这份计划，
  系统提示词要求「本回合方案必须推进既定战略，不得推翻计划」。

套牌来源优先级：config.yaml 手填 deckstring > 同目录 ``Decks.log`` 里
最后一次 ``Finding Game With Deck:`` 块（换卡组 / 换日志目录自动跟上）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .cards import CardDB
from .deck import DeckTracker

if TYPE_CHECKING:  # 避免与 advisor 形成运行时循环导入
    from .advisor import LLMClient
    from .log_parser import GameStateReconstructor


# ─────────────────────────────────────────────────────────────
# 套牌画像（确定性，不花钱也不花时间）
# ─────────────────────────────────────────────────────────────

#: 卡面文本 → 战术角色。规则只负责给模型「线索」，不替模型下结论。
ROLE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("任务与任务线", ("任务：", "任务线", "任务奖励")),
    ("兆示/累积体系", ("兆示", "的次数提升", "升级")),
    ("进化与变形", ("变形成为", "进化", "随机将", "洗入你的牌库")),
    ("铺场与站场", ("召唤", "获取两张", "置入你的手牌", "突袭")),
    ("终结手段", ("灾变", "巨型", "风怒", "对敌方英雄造成", "造成致命")),
    ("解场与去除", ("变形成为一只", "沉默", "消灭", "对所有敌方随从造成", "造成$", "造成#")),
    ("过牌与资源", ("抽一张", "抽两张", "发现一张", "获取一张", "可交易")),
    ("回复与护甲", ("恢复#", "恢复", "获得护甲", "吸血")),
    ("亡语与复生", ("亡语", "复生")),
]

THEME_LABELS = {
    "任务与任务线": "任务体系",
    "兆示/累积体系": "兆示累积",
    "进化与变形": "进化变形",
    "铺场与站场": "铺场",
    "终结手段": "高费终结",
    "解场与去除": "解场",
    "过牌与资源": "过牌",
    "回复与护甲": "回复",
    "亡语与复生": "亡语",
}


def _classify(name: str, cost: int, ctype: str, text: str, keywords: list[str]) -> list[str]:
    """按卡面文本与费用给一张牌打战术标签。"""
    roles: list[str] = []
    for role, needles in ROLE_PATTERNS:
        if any(n in text for n in needles):
            roles.append(role)
    kw = set(keywords or [])
    if "QUEST" in kw or "QUESTLINE" in kw:
        roles.append("任务与任务线")
    if "WINDFURY" in kw or "COLOSSAL" in kw or (ctype == "MINION" and cost >= 7):
        roles.append("终结手段")
    if "TAUNT" in kw and cost <= 3:
        roles.append("铺场与站场")
    if "OVERLOAD" in kw:
        roles.append("过载节奏")
    # 同类只留一次，保持顺序稳定
    return list(dict.fromkeys(roles))


@dataclass
class DeckProfile:
    """从套牌码解出的完整画像：给「慢层」读一次就够。"""

    deck_name: str = ""
    deck_id: str = ""
    format_name: str = ""
    hero: str = ""
    size: int = 0
    identified: int = 0
    unidentified: int = 0
    cards: list[dict] = field(default_factory=list)
    curve: dict[str, int] = field(default_factory=dict)
    themes: list[str] = field(default_factory=list)
    roles: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, deck: DeckTracker, db: CardDB) -> "DeckProfile":
        cards: list[dict] = []
        roles: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for name, count in sorted(deck.total.items(), key=lambda x: x[0]):
            c = db.by_name(name)
            text = (c.text if c else "")[:110]
            cost = c.cost if c else 0
            ctype = c.type if c else "UNKNOWN"
            kw = list(c.keywords) if c else []
            tags = _classify(name, cost, ctype, text, kw)
            cards.append({
                "名称": name,
                "费用": cost,
                "张数": count,
                "类型": ctype,
                "卡号": c.id if c else "",
                "效果": text,
                "战术角色": tags,
            })
            for t in tags:
                roles.setdefault(t, []).append(name)
                counts[t] = counts.get(t, 0) + 1
        themes = [
            THEME_LABELS.get(t, t)
            for t, _ in sorted(counts.items(), key=lambda x: -x[1])[:7]
        ]
        return cls(
            deck_name=deck.deck_name,
            deck_id=deck.deck_id,
            format_name=deck.format_name,
            hero=deck.hero,
            size=deck.size,
            identified=len(deck.total),
            unidentified=len(deck.unknown_dbfids),
            cards=sorted(cards, key=lambda x: (x["费用"], x["名称"])),
            curve=deck.curve(),
            themes=themes,
            roles=roles,
        )

    def to_prompt_dict(self) -> dict:
        d: dict[str, Any] = {
            "套牌名称": self.deck_name,
            "模式": self.format_name,
            "英雄": self.hero,
            "已识别张数": self.identified,
            "法力曲线": self.curve,
            "主题线索": self.themes,
            "战术角色": {k: v[:8] for k, v in self.roles.items()},
            "完整卡表": self.cards,
        }
        if self.unidentified:
            d["卡表缺口"] = (
                f"有 {self.unidentified} 张牌本地卡表未收录（dfbId 查不到），"
                "规划时按「未知牌」处理，不要凭空编造效果；可运行 refresh-cards 更新"
            )
        return d


# ─────────────────────────────────────────────────────────────
# 整局计划
# ─────────────────────────────────────────────────────────────

PLAN_SYSTEM_PROMPT = """你是一位炉石传说构筑与对局战略教练。现在不是让你解某一回合的场面，
而是让你**读完整套牌，制定一整局的游戏计划**。

输入是：这副牌的全部卡牌（含中文效果文本、费用、数量、战术角色）、法力曲线、
主题线索，以及本局开局信息（双方英雄、先后手、模式）。

要求：
1. 先判断套牌定位（快攻 / 中速 / 控制 / 组合技 / 任务 / 节奏），并给出**一条明确的致胜路径**：
   靠什么赢、大概在第几个我方回合成型、需要哪些牌同时在场或在手。
2. 把整局拆成前期 / 中期 / 后期三段，每段写明「我方回合区间 + 目标 + 该做的 2-3 件事」，
   目标必须是**可检验的**（例如「完成任务：累计使用 6 个不同类型随从」、
   「保住 2 个以上随从活到升腾回合」），不要写「稳住场面」这种空话。
3. 列出核心卡：每张写「用途」和 ``keep``（true = 稀缺资源，非关键回合不能随手打掉，
   例如唯一 AOE / 变形 / 终结组件 / 任务组件）。
4. 给出资源纪律：哪些牌必须留、什么情况下才允许用、哪些牌可以当耗材。
5. 给出斩杀/终结组件的清单与成型条件；没有斩杀组合就写「靠场面持续输出」。
6. 给出起手留牌优先级（按卡名），并针对对手职业写一句对局应对。
7. 只依据输入卡表与效果文本，不要凭卡名编造不存在的效果；未收录的牌按未知处理。
8. 全部用中文，简洁具体，禁止空泛套话。严格输出 JSON，不要包裹 markdown 代码块：

{
  "archetype": "中速任务进化萨",
  "win_condition": "一句话致胜路径",
  "stages": [
    {"my_turn_from": 1, "my_turn_to": 3, "goal": "可检验的阶段目标", "actions": ["动作1", "动作2"]},
    {"my_turn_from": 4, "my_turn_to": 7, "goal": "...", "actions": []},
    {"my_turn_from": 8, "my_turn_to": 99, "goal": "...", "actions": []}
  ],
  "core_cards": [{"name": "卡名", "role": "用途", "keep": true}],
  "resource_rules": ["资源纪律1", "资源纪律2"],
  "lethal_setup": {"components": ["卡名"], "note": "成型条件"},
  "mulligan_priority": ["卡名"],
  "matchup_note": "针对当前对手的一句话",
  "next_milestone": "当前最该推进的下一件事"
}"""


@dataclass
class GamePlan:
    """跨回合复用的整局计划。回合建议只能服从它，不能每回合重新定义战略。"""

    deck_key: str = ""
    deck_name: str = ""
    archetype: str = ""
    win_condition: str = ""
    stages: list[dict] = field(default_factory=list)
    core_cards: list[dict] = field(default_factory=list)
    resource_rules: list[str] = field(default_factory=list)
    lethal_setup: dict = field(default_factory=dict)
    mulligan_priority: list[str] = field(default_factory=list)
    matchup_note: str = ""
    next_milestone: str = ""
    created_turn: int = 0
    updated_turn: int = 0
    revision: int = 1
    source: str = "llm"  # llm | fallback

    def current_stage(self, my_turn: int) -> dict:
        for s in self.stages:
            lo = int(s.get("my_turn_from", 0) or 0)
            hi = int(s.get("my_turn_to", 99) or 99)
            if lo <= my_turn <= hi:
                return s
        return self.stages[-1] if self.stages else {"goal": self.win_condition}

    @property
    def keep_cards(self) -> list[str]:
        return [
            str(c.get("name", "")) for c in self.core_cards
            if c.get("keep") and c.get("name")
        ]

    def to_prompt_dict(self, my_turn: int) -> dict:
        stage = self.current_stage(my_turn)
        return {
            "套牌定位": self.archetype,
            "致胜条件": self.win_condition,
            "当前阶段": {
                "我方回合": my_turn,
                "目标": stage.get("goal", ""),
                "该做的事": stage.get("actions", []),
            },
            "下一里程碑": self.next_milestone,
            "核心卡与用途": [
                {
                    "名称": c.get("name"),
                    "用途": c.get("role"),
                    "必须保留": "是" if c.get("keep") else "否",
                }
                for c in self.core_cards[:10]
            ],
            "资源纪律": self.resource_rules[:6],
            "终结组件": self.lethal_setup,
            "对局应对": self.matchup_note,
            "计划版本": self.revision,
            "使用规则": (
                "本局既定战略：每回合建议都必须推进它；"
                "不得为单回合小赚打掉「必须保留=是」的核心卡，也不得另起一套打法。"
                "确需调整只能在 warnings 里提一句。"
            ),
        }

    def short_line(self, my_turn: int) -> str:
        """浮窗里的一行摘要。"""
        goal = str(self.current_stage(my_turn).get("goal", ""))[:34]
        head = (self.archetype or self.deck_name or "整局计划")[:18]
        return f"整局：{head} → {goal}" if goal else f"整局：{head}"

    def to_dict(self) -> dict:
        return {
            "deck_name": self.deck_name,
            "archetype": self.archetype,
            "win_condition": self.win_condition,
            "stages": self.stages,
            "core_cards": self.core_cards,
            "resource_rules": self.resource_rules,
            "lethal_setup": self.lethal_setup,
            "mulligan_priority": self.mulligan_priority,
            "matchup_note": self.matchup_note,
            "next_milestone": self.next_milestone,
            "created_turn": self.created_turn,
            "updated_turn": self.updated_turn,
            "revision": self.revision,
            "source": self.source,
        }


def my_turn_of(global_turn: int) -> int:
    """全局 TURN 每个玩家回合 +1，我方回合数近似为 (TURN+1)//2。

    阶段边界本来就宽松（1-3 / 4-7 / 8+），先后手差 1 不影响目标判定。
    """
    return max(1, (max(1, global_turn) + 1) // 2)


def _extract_json(text: str) -> dict:
    """容忍 markdown 代码块与前后废话。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    return json.loads(s)


def fallback_plan(profile: DeckProfile, turn: int) -> GamePlan:
    """没配 Key / 模型返回非法 JSON 时的规则兜底计划（绝不中断使用）。"""
    finishers = profile.roles.get("终结手段", [])[:4]
    quest = profile.roles.get("任务与任务线", [])
    removal = profile.roles.get("解场与去除", [])[:4]
    spread = profile.roles.get("铺场与站场", [])[:4]
    if quest:
        win = f"先完成「{quest[0]}」，靠铺场与进化建立场面后收尾"
    elif finishers:
        win = f"用低费牌过渡并保住血量，中期拍下 {'、'.join(finishers[:2])} 终结"
    else:
        win = "按法力曲线逐步铺场，用场面持续输出压低对手血量"
    keep = list(dict.fromkeys(finishers + removal + quest))[:8]
    return GamePlan(
        deck_key=profile.deck_id or profile.deck_name,
        deck_name=profile.deck_name,
        archetype="、".join(profile.themes[:2]) + "套牌" if profile.themes else "未知体系",
        win_condition=win,
        stages=[
            {"my_turn_from": 1, "my_turn_to": 3,
             "goal": "按曲线站场，完成任务 / 兆示等累积条件",
             "actions": ["优先打出低费随从与累积组件", "保留稀缺解牌"]},
            {"my_turn_from": 4, "my_turn_to": 7,
             "goal": "铺开场面并用进化 / buff 建立不可解的场面",
             "actions": ["把随从站住到进化回合", "只在关键威胁出现时用解牌"]},
            {"my_turn_from": 8, "my_turn_to": 99,
             "goal": "拍下终结组件或攒齐斩杀",
             "actions": ["计算斩杀", "不再无谓保留资源"]},
        ],
        core_cards=[{"name": n, "role": "核心组件", "keep": n in keep}
                    for n in list(dict.fromkeys(finishers + spread + removal))[:10]],
        resource_rules=[
            *[f"{n} 是稀缺资源，非关键威胁不要随手打掉" for n in removal[:3]],
            "斩杀组件留到能一击致命的回合",
            "不要为了单回合小赚破坏铺场节奏",
        ],
        lethal_setup={"components": finishers, "note": "靠场面持续输出或高费终结组件"},
        mulligan_priority=list(dict.fromkeys(quest + spread))[:6],
        matchup_note="本地兜底计划：请补充 LLM Key 以获得按对局定制的整局规划",
        next_milestone="按曲线站场并推进套牌的累积条件",
        created_turn=turn,
        updated_turn=turn,
        source="fallback",
    )


def build_plan_prompt(
    profile: DeckProfile,
    recon: "GameStateReconstructor",
    my_pid: int,
    card_db: CardDB,
) -> tuple[str, dict]:
    """慢层输入：完整套牌 + 开局信息。"""
    me = recon.player_view(my_pid)
    foe = recon.player_view(3 - my_pid)
    hero_me = me.hero.display_name if me.hero else "未建立"
    hero_foe = foe.hero.display_name if foe.hero else "未建立"
    payload = {
        "套牌画像": profile.to_prompt_dict(),
        "本局信息": {
            "模式": profile.format_name or "未知",
            "我方英雄": hero_me,
            "对手英雄": hero_foe,
            "先后手": (
                "我方先手" if recon.first_player == my_pid
                else "我方后手（有幸运币）" if recon.first_player else "未知"
            ),
            "注意": (
                "只依据套牌与对手英雄制定**跨回合**战略；"
                "不要写入当前回合、当前手牌等会过期的信息，计划要能整局复用。"
            ),
        },
    }
    return PLAN_SYSTEM_PROMPT, payload


class PlanEngine:
    """维护「每局一份计划」：低频生成、跨回合复用、阶段推进时重评。

    回合建议层只读取 :attr:`plan`，不修改它 —— 计划若每回合都变，
    就等于没有计划，模型仍会退化成单回合贪心。
    """

    def __init__(self, client: "LLMClient", card_db: CardDB, enabled: bool = True):
        self.client = client
        self.card_db = card_db
        self.enabled = enabled
        self.plan: GamePlan | None = None
        self.profile: DeckProfile | None = None
        self._deck_key = ""
        self._last_turn = 0
        self._stage_key = ""
        self._revisions = 0

    def reset(self) -> None:
        self.plan = None
        self.profile = None
        self._last_turn = 0
        self._stage_key = ""
        self._revisions = 0

    def update(
        self,
        recon: "GameStateReconstructor",
        deck: DeckTracker | None,
        my_pid: int,
        force: bool = False,
    ) -> tuple[GamePlan | None, DeckProfile | None]:
        """返回当前可用的 (plan, profile)；必要时生成一次。"""
        if not self.enabled or deck is None or not deck.size:
            return None, None
        turn = max(0, recon.turn)
        key = deck.deck_id or f"{deck.deck_name}:{deck.size}:{len(deck.total)}"
        # 回合号回退 = 新对局；套牌变了也要重新规划
        new_game = (turn < self._last_turn) or (key != self._deck_key)
        if new_game or force:
            self.reset()
            self._deck_key = key
        if self.profile is None:
            self.profile = DeckProfile.build(deck, self.card_db)

        need = force or self.plan is None
        my_turn = my_turn_of(turn)
        if not need and self.plan is not None and self._revisions < 3:
            stage = self.plan.current_stage(my_turn)
            stage_key = f"{stage.get('my_turn_from')}-{stage.get('my_turn_to')}"
            # 进入新阶段且距上次规划至少 4 个全局回合才重评，避免每回合摇摆
            if (
                self._stage_key and stage_key != self._stage_key
                and turn >= self.plan.updated_turn + 4
            ):
                need = True
        if need:
            self._generate(recon, my_pid, turn, force=force)
        if self.plan:
            stage = self.plan.current_stage(my_turn)
            self._stage_key = f"{stage.get('my_turn_from')}-{stage.get('my_turn_to')}"
        self._last_turn = max(self._last_turn, turn)
        return self.plan, self.profile

    def _generate(
        self,
        recon: "GameStateReconstructor",
        my_pid: int,
        turn: int,
        force: bool = False,
    ) -> None:
        assert self.profile is not None
        system, payload = build_plan_prompt(self.profile, recon, my_pid, self.card_db)
        plan: GamePlan | None = None
        try:
            content = self.client.chat_json(
                system, json.dumps(payload, ensure_ascii=False), max_tokens=1400
            )
            data = _extract_json(content)
            if isinstance(data, dict) and (data.get("win_condition") or data.get("stages")):
                plan = GamePlan(
                    deck_key=self._deck_key,
                    deck_name=self.profile.deck_name,
                    archetype=str(data.get("archetype", ""))[:60],
                    win_condition=str(data.get("win_condition", ""))[:200],
                    stages=[
                        {
                            "my_turn_from": int(s.get("my_turn_from", 0) or 0),
                            "my_turn_to": int(s.get("my_turn_to", 99) or 99),
                            "goal": str(s.get("goal", ""))[:120],
                            "actions": [str(a)[:60] for a in (s.get("actions") or [])[:4]],
                        }
                        for s in (data.get("stages") or [])[:4] if isinstance(s, dict)
                    ],
                    core_cards=[
                        {
                            "name": str(c.get("name", ""))[:40],
                            "role": str(c.get("role", ""))[:80],
                            "keep": bool(c.get("keep", False)),
                        }
                        for c in (data.get("core_cards") or [])[:14] if isinstance(c, dict)
                    ][:14],
                    resource_rules=[str(x)[:100] for x in (data.get("resource_rules") or [])[:8]],
                    lethal_setup=data.get("lethal_setup") or {},
                    mulligan_priority=[str(x)[:40] for x in (data.get("mulligan_priority") or [])[:8]],
                    matchup_note=str(data.get("matchup_note", ""))[:160],
                    next_milestone=str(data.get("next_milestone", ""))[:160],
                    created_turn=turn if self.plan is None else self.plan.created_turn,
                    updated_turn=turn,
                    revision=(self.plan.revision + 1) if self.plan and not force else 1,
                    source="llm",
                )
        except Exception:
            plan = None
        if plan is None or not plan.stages:
            plan = fallback_plan(self.profile, turn)
            plan.deck_key = self._deck_key
        self.plan = plan
        self._revisions += 1

    def progress(self, recon: "GameStateReconstructor", my_pid: int) -> dict:
        """给回合提示词的「计划进度」：把战略落成当前可核对的事实。"""
        if not self.plan or not self.profile:
            return {}
        me = recon.player_view(my_pid)
        my_turn = my_turn_of(recon.turn)
        hand = [e.display_name for e in me.hand]
        on_board = [e.display_name for e in me.board]
        used = [e.display_name for e in me.graveyard]
        core = [str(c.get("name", "")) for c in self.plan.core_cards if c.get("name")]
        return {
            "我方回合": my_turn,
            "当前阶段目标": self.plan.current_stage(my_turn).get("goal", ""),
            "下一里程碑": self.plan.next_milestone,
            "手牌中的核心卡": [n for n in core if n in hand],
            "已在场的核心卡": [n for n in core if n in on_board],
            "已消耗的核心卡": [n for n in core if n in used],
            "必须保留的牌": self.plan.keep_cards[:10],
            "计划版本": self.plan.revision,
            "说明": "核心卡状态来自日志可见区域；对手手牌与未揭示牌库不可见",
        }
