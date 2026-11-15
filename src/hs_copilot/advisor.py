"""
炉石助手 · LLM 军师

把结构化局面 + 套牌信息喂给大模型（OpenAI 兼容接口），拿到战术建议。

设计要点：
- 强制 JSON 输出，便于程序化解析与降级
- 提示词里显式给出卡牌效果文本，让模型无需外部知识（新扩展也能懂）
- 附带局面启发式预判（斩杀检测等）作为参考锚点，提升模型稳定性
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from .cards import CardDB
from .config import Config
from .deck import DeckTracker
from .log_parser import GameStateReconstructor
from .planner import DeckProfile, GamePlan, my_turn_of
from .privacy import collect_secrets, scrub_messages


# ─────────────────────────────────────────────────────────────
# 启发式预判（不依赖 LLM 的硬规则，给模型提供锚点）
# ─────────────────────────────────────────────────────────────


def detect_lethal(recon: GameStateReconstructor, my_pid: int) -> dict | None:
    """检测斩杀机会：本回合**真正能打脸**的伤害 是否 ≥ 对手有效血量。

    关键教训：旧版把 ``me.board`` 里所有随从的攻击力一律累加。炉石客户端
    对刚召唤的随从写 ``EXHAUSTED=1``（召唤失调），它们本回合根本不能攻击；
    已攻击过、被冻结、突袭（只能打随从）的随从同样不能打脸。于是系统预判
    频繁虚报斩杀，而提示词又要求「存在斩杀必须优先给斩杀方案」，模型只能
    给出「用刚上场的随从打脸」这种不可执行的建议。
    """
    foe = recon.player_view(2 if my_pid == 1 else 1)
    target_hp = foe.hero_hp + foe.armor
    if target_hp <= 0:
        return None

    me = recon.player_view(my_pid)
    dmg = 0
    sources: list[str] = []
    excluded: list[str] = []

    # 对手场上的嘲讽会阻挡打脸，单独列出让模型先处理
    taunts = [m for m in foe.board if "嘲讽" in m.mechanics()]
    for m in me.board:
        atk, _ = m.stats
        if atk <= 0:
            continue
        st = m.attack_status()
        if not st["can_attack_hero"]:
            excluded.append(f"{m.display_name}（{st['reason']}）")
            continue
        dmg += atk
        sources.append(f"{m.display_name}(随从){atk}")

    # 英雄技能
    if me.hero_power:
        hp_dmg = me.hero_power.get_int("CORPUSDAMAGE", 0)
        if hp_dmg and me.mana >= me.hero_power.cost:
            dmg += hp_dmg
            sources.append(f"英雄技能{hp_dmg}")

    if dmg >= target_hp:
        return {
            "lethal": True,
            "need": target_hp,
            "available": dmg,
            "blockers": [m.display_name for m in taunts],
            "sources": sources,
            "excluded_minions": excluded,
            "说明": "只统计本回合能打脸的随从；刚召唤/已攻击/冻结/突袭随从未计入",
        }
    return None


def estimate_board_value(view, next_turn: bool = False) -> dict:
    """粗略评估场面价值。**区分本回合能打出的伤害与账面攻击力**。

    ``next_turn=True`` 用于对手侧：现在轮到我方时，对手随从普遍带着
    ``EXHAUSTED=1``（刚结束自己的回合），若按「本回合能否攻击」评估会得出
    「对手 0 攻击力、毫无威胁」的错误结论。对手应按其下个回合的就绪状态算。
    """
    total_atk = sum(e.stats[0] for e in view.board)
    total_hp = sum(e.stats[1] for e in view.board)
    taunts = sum(1 for e in view.board if "嘲讽" in e.mechanics())
    if next_turn:
        ready = [e for e in view.board if e.attack_status()["ready_next_turn"]]
        not_ready = [
            f"{e.display_name}（下个对手回合仍可能无法攻击）"
            for e in view.board if not e.attack_status()["ready_next_turn"]
        ]
    else:
        ready = [e for e in view.board if e.attack_status()["can_attack"]]
        not_ready = [
            f"{e.display_name}（{e.attack_status()['reason']}）"
            for e in view.board if not e.attack_status()["can_attack"]
        ]
    return {
        "minion_count": len(view.board),
        "total_atk": total_atk,
        "total_hp": total_hp,
        "taunt_count": taunts,
        # 真正能用于攻击/交换的攻击力，不等同于 total_atk
        "ready_minion_count": len(ready),
        "ready_atk": sum(e.stats[0] for e in ready),
        "按下一回合计算": next_turn,
        "not_ready_minions": not_ready,
        # 旧版用「卡号含 CS2」猜法术，纯属巧合式判断，会大量误判
        "spells_in_hand": sum(1 for e in view.hand if e.cardtype == "SPELL"),
    }


# ─────────────────────────────────────────────────────────────
# 建议数据结构
# ─────────────────────────────────────────────────────────────


@dataclass
class Advice:
    """大模型给出的建议。"""

    #: 推荐操作序列，每步是自然语言描述
    plan: list[str] = field(default_factory=list)
    #: 关键提示/风险
    warnings: list[str] = field(default_factory=list)
    #: 手牌编号建议（对应 hand 列表下标，1 起）
    hand_hints: list[dict] = field(default_factory=list)
    #: 是否存在斩杀
    lethal: bool = False
    #: 一句话总结
    summary: str = ""
    #: 原始输出（调试用）
    raw: str = ""
    #: 整局计划的一行摘要（由调用方填入，浮窗状态条也会单独显示）
    plan_line: str = ""
    #: 本回合在整局计划里推进了哪一步（模型自报，缺失时由本地补齐）
    plan_step: str = ""
    #: 校验器产出的驳回原因（仅供自动改正流程内部使用，不进 to_text）
    rejections: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = []
        if self.summary:
            lines.append(f"【结论】{self.summary}")
        if self.plan_step:
            lines.append(f"【整局】{self.plan_step}")
        if self.lethal:
            lines.append("【斩杀】可斩杀，本回合结束战斗")
        if self.plan:
            lines.append("【操作顺序】")
            for i, step in enumerate(self.plan, 1):
                lines.append(f"  {i}. {step}")
        if self.warnings:
            lines.append("【注意】")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if self.hand_hints:
            lines.append("【手牌编号】")
            for h in self.hand_hints:
                lines.append(f"  #{h.get('index')} {h.get('name')}：{h.get('reason', '')}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 提示词
# ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位炉石传说资深教练，擅长构筑与对战决策。

你的任务：在「【整局计划】」这一既定战略下，给出**本回合具体、可执行**的出牌步骤。
不要重新制定战略，也不要只做单回合贪心解场——每一步都要说明它推进了计划的哪个目标。

要求：
1. 建议必须是明确的操作序列（例如「先用英雄技能点脸，再打出 X解场」），不要泛泛而谈。
2. 严格遵守法力限制：优先直接采用「【系统预判】合法费用组合」里的候选；自行组合时按每条目「法力消耗」逐步累加，累计不得超过「局面.可用法力」。超出就删掉最后一步或改用低费牌。**只有打出手牌的步骤**才注明费用（格式如「打出妖术（3费）」）；随从攻击、英雄技能、地标启动不是打出手牌，不要给它们标注「0 费」或任何费用。
3. 若「【系统预判】斩杀机会」存在，必须优先给出斩杀方案并明确说明；**不得**自己把「本回合不能攻击」的随从攻击力加进去凑出一个斩杀。
4. 若场面不利，明确指出应该放弃当前交换、保存哪张牌、留几费。
5. 注意关键词交互：圣盾、嘲讽、风怒、冻结、潜行、亡语。
6. **卡面未知（最高优先级之一，违反即建议作废）**：payload 里带「卡面未知」的手牌，
   客户端还没写入卡号——无法确定它是什么牌、费用多少、效果如何。此时：
   - 严禁把这些编号写进 plan 的具体步骤，严禁写进 hand_hints，严禁用「实体N」这类内部编号；
   - 严禁写「若为低费随从 / 若是解牌 / 如果是过牌 / 可本回合打出 / 需按实际费用判断」
     这类条件猜测句；不要对未知牌做任何费用、类型、效果的断言；
   - 只依据「已揭示的手牌」「场面随从（含本回合能否攻击）」「英雄技能」「武器」
     「场上地标」「【整局计划】」给出明确可执行的操作序列；
   - 「【未揭示手牌推演】」只是套牌剩余候选池，用于判断资源倾向，不能断言某个编号是哪张牌；
   - 仍按真实「手牌总数」判断资源量，绝对不要说「手牌为空，只能空过」。
7. 「生命」「手牌总数」等为 0 时，先检查是否是对方回合/对局已结束；
   若步骤为 MAIN_ACTION 且轮到我方，不要断言对局结束。
   血量数据未就绪时一律按规则 18 处理。
8. 用中文回答，简洁有力，不要长篇大论。**直接给结论和操作序列，最多 4 步、3 条风险**；不要复述局面、不要解释推理过程、不要写客套话。
9. 「卡牌效果」来自中文卡表并按「卡号」精确匹配；条目标注「按卡名兜底」或「未命中」时仅供参考，不要凭卡名编造效果。
10. 「场上地标」不是随从：不能攻击、有耐久、通常每回合限用一次。不要把它算进随从数、随从交换或斩杀伤害。
11. 「牌库剩余」若为「未知」，不要断言牌库已空，也不要据此计算疲劳伤害。
12. 「对局状态」为已结束时只做简短复盘，不要给本回合出牌建议。
    例外：血量数据未就绪时不适用（见规则 18）。
13. 结合「近期战况」判断刚发生了什么；「当前生效关键词」是运行时状态，优先于卡面静态描述。
14. 手牌「编号」按 payload 中每条目给出的编号，不要用日志站位数字代替。
15. **攻击资格（最高优先级，违反即建议作废）**：随从条目的「本回合能否攻击」为「否」时，
    该随从本回合**绝对不能**攻击、不能参与随从交换、不能计入斩杀伤害、不能用来解场。
    「刚召唤=是」的随从是召唤失调：这回合只能站在场上，要等下个自己的回合才能动。
    「仅能攻击随从（突袭）」的随从可以撞随从，但不能打英雄。
    斩杀与交换一律以「【系统预判】本回合可用攻击」里列出的随从为准。
16. 「本回合不能攻击」列出的随从仍然算作场面资源（buff 载体、嘲讽、下回合威胁），
    但**不要**写出「让 X 攻击」「用 X 换掉 Y」「X 打脸」这类操作；需要说明时写「X 本回合不能动，留到下回合」。
17. 对手随从的「对手下回合能否攻击」才是威胁判断依据：即使它现在显示「刚召唤」，
    轮到对手时就能攻击，必须提前防范；不要因为对手随从当前疲劳就无视它。
18. **血量未就绪（最高优先级，违反即建议作废）**：若 payload 含「【数据告警】血量未就绪」
    或「局面.血量数据」不是「已就绪」，说明英雄血量只是**尚未从日志读到**，不是真的 0 血。
    此时：
    - 「生命」显示「未知」就按未知处理，绝对禁止断言「对局已结束 / 已分胜负 /
      双方或对手生命为 0 / 无需出牌 / 只能空过」；
    - 禁止按 0 血计算斩杀、疲劳、血量差，也不要给斩杀结论；
    - 仍要照常给出**本回合可执行的出牌建议**（依据法力、手牌、场面、卡牌效果），
      斩杀只认「【系统预判】斩杀机会」，可用攻击仅用于交换判断。
    规则 7 / 12 在本条面前让位：只有「对局状态」明确为已结束**且**血量数据已就绪时才做终局复盘。
19. **整局计划优先（最高优先级，违反即建议作废）**：payload 里的「【整局计划】」是本局既定战略，
    由读完整套牌后制定，跨回合不变。你必须：
    - 先读「当前阶段.目标」「下一里程碑」「资源纪律」「核心卡与用途」；
    - 本回合方案要**明确推进该目标**，并在 summary 里点出是在推进什么（例如
      「按任务铺场计划，先下 2 费随从累计任务进度」），而不是只写「解掉对面随从」；
    - 不得另起一套打法（例如把控制牌组当成快攻打脸、把快攻牌组拖成后期）；
    - 若认为计划确实需要调整，只能在 warnings 里提一句，本回合仍按原计划执行。
20. **资源纪律**：「核心卡与用途」中「必须保留=是」的牌是稀缺资源（唯一 AOE / 变形 /
    斩杀组件 / 任务组件）。没有「斩杀」「致命威胁」「不解场就输」这类理由时，
    不要为了单回合小赚把它打掉；宁可用场面交换、英雄技能或低费牌过渡。
    确需动用时，在对应步骤后面写明理由。
21. 「【计划进度】」列出手牌 / 场面 / 已消耗的核心卡：优先用手上已有的组件推进计划，
    不要把「已消耗」的牌当成还能打，也不要假设未揭示牌库里一定有某张牌。

严格输出 JSON，格式如下（不要包裹 markdown 代码块）：
{
  "summary": "一句话结论（必须点明在推进整局计划的哪个目标）",
  "lethal": false,
  "plan": ["第一步操作", "第二步操作"],
  "warnings": ["风险提示，可为空数组"],
  "hand_hints": [{"index": 1, "name": "卡名", "reason": "为什么"}],
  "plan_step": "本回合推进了整局计划的哪一步，一句话，不超过 30 字"
}

其中 index 是手牌从左到右的编号（从 1 开始）；**只允许填入带「卡号」的已揭示编号**，
卡面未知的编号一律不要出现在 hand_hints 里。"""


def _readable(name: str, card_db: CardDB) -> str:
    """把实体名转成可读卡名。

    运行时实体（英雄、变形体、衍生随从）的 name 字段常是卡号本身（如 ``HERO_04b``），
    需经卡表反查；查不到则原样返回，交由模型猜测。
    """
    if not name:
        return name
    if name.isascii() and ("_" in name or name.isupper()):
        c = card_db.by_id(name)
        if c and c.name:
            return c.name
    return name


TYPE_LABELS = {
    "MINION": "随从",
    "SPELL": "法术",
    "WEAPON": "武器",
    "HERO": "英雄",
    "HERO_POWER": "英雄技能",
    "LOCATION": "地标",
    "ENCHANTMENT": "附魔/光环",
}

#: 卡表里的英文关键词 → 中文，避免模型把 BATTLECRY / RUSH 当成生僻代号
KEYWORD_LABELS = {
    "BATTLECRY": "战吼", "DEATHRATTLE": "亡语", "TAUNT": "嘲讽",
    "DIVINE_SHIELD": "圣盾", "WINDFURY": "风怒", "CHARGE": "冲锋",
    "RUSH": "突袭", "STEALTH": "潜行", "LIFESTEAL": "吸血",
    "POISONOUS": "剧毒", "DISCOVER": "发现",
    "AURA": "光环", "SECRET": "奥秘", "COLOSSAL": "巨型",
    "DORMANT": "休眠", "REBORN": "复生", "OVERLOAD": "过载",
    "COMBO": "连击", "CHOOSE_ONE": "抉择", "QUEST": "任务",
    "SILENCE": "沉默", "FREEZE": "冻结", "IMMUNE": "免疫",
}


def describe_entity(e, card_db: CardDB | None, *, index: int | None = None) -> dict:
    """生成实体的完整描述：中文卡表效果文本 + 卡号匹配来源。

    旧版只拿中文卡名查卡表。卡表里同名条目极多（酒馆战棋英雄卡、
    旧版重印、衍生物、附魔载体），实测「梦魇之王萨维斯」被匹配成
    ``BG36_HERO_105``：0 费英雄、没有任何效果文本；「卑劣的脏鼠」
    被换成旧版 ``CFM_790``。模型拿到错的卡号/费用/描述，建议自然不可用。

    现在一律优先用日志里的 ``cardId`` 精确匹配，只有卡号缺失时才按名字
    兜底，并把匹配方式写进 payload，让模型能判断信息可信度。
    """
    card = card_db.by_id(e.card_id) if (card_db and e.card_id) else None
    by_name = False
    if card is None and card_db and e.display_name and not e.display_name.startswith("UNKNOWN ENTITY"):
        card = card_db.by_name(_readable(e.display_name, card_db))
        by_name = card is not None

    name = e.display_name
    # 「实体14」是 log_parser 在 name/cardId 都为空时的内部回退名，不是卡名。
    # 把它塞进 payload 会让模型以为这是某种牌，进而写出「实体14：若为低费随从…」
    # 这类凭空猜测的建议。统一改成可读占位，并明确禁止用于出牌步骤。
    opaque = (
        not name
        or name.startswith("UNKNOWN ENTITY")
        or (name.startswith("实体") and name[2:].isdigit())
    )
    if opaque:
        name = "（卡面未揭示）"
    d: dict = {"名称": name}
    if index is not None:
        d["编号"] = index
    if e.card_id:
        d["卡号"] = e.card_id
    else:
        # 与旧版提示词/回归测试保持一致的字段名
        d["卡面未知"] = True
        d["不可用于出牌步骤"] = True
        d["说明"] = (
            "该牌确实在此区域，但本局日志尚未写入卡号；"
            "禁止猜测费用与效果，禁止把它写进具体出牌步骤或 hand_hints"
        )

    if card:
        d["类型"] = TYPE_LABELS.get(card.type, card.type)
        d["法力消耗"] = card.cost
        d["卡牌效果"] = card.text or "（无额外效果文本）"
        if card.type in ("MINION", "WEAPON", "LOCATION"):
            d["卡面攻血/耐久"] = f"{card.attack}/{card.health}"
        if card.keywords:
            d["卡面关键词"] = [KEYWORD_LABELS.get(k, k) for k in card.keywords]
        if card.race:
            d["种族"] = card.race
        d["卡表匹配"] = (
            "按卡名兜底，卡表条目可能不是这张卡，效果文本仅供参考"
            if by_name else "按日志卡号精确匹配"
        )
    elif not e.card_id:
        d["卡牌效果"] = "（卡号未知，无法查卡表效果）"
    else:
        d["卡牌效果"] = "（卡表未收录此卡号，请按卡名与已有信息判断）"
        d["卡表匹配"] = "未命中"
    return d


def legal_play_combos(hand: list[dict], mana: int, limit: int = 3) -> list[dict]:
    """枚举费用合计不超过可用法力的手牌组合，给模型一个不会算错的锚点。

    实测模型会把「3 费妖术 + 5 费熔爪巨龙」当成 7 费内还能再挂 2 费技能。
    与其指望模型做算术，不如本地用卡表费用先算好合法组合喂给它。
    """
    cards = [
        (h.get("编号"), h.get("名称"), h.get("法力消耗"))
        for h in hand
        if isinstance(h.get("法力消耗"), int) and not h.get("卡面未知")
    ]
    if not cards or mana <= 0 or len(cards) > 12:
        return []
    found: list[tuple[int, int, list]] = []
    for mask in range(1, 1 << len(cards)):
        picked = [cards[i] for i in range(len(cards)) if mask & (1 << i)]
        total = sum(c[2] for c in picked)
        if total <= mana:
            found.append((total, len(picked), picked))
    found.sort(key=lambda x: (-x[0], -x[1]))
    out: list[dict] = []
    for total, _, picked in found[: limit * 4]:
        out.append({
            "合计费用": total,
            "剩余法力": mana - total,
            "手牌编号与费用": [f"#{c[0]} {c[1]}（{c[2]}费）" for c in picked],
        })
        if len(out) >= limit:
            break
    return out


def build_prompt(
    recon: GameStateReconstructor,
    card_db: CardDB,
    deck: DeckTracker | None,
    my_pid: int,
    phase: str = "play",
    plan: GamePlan | None = None,
    profile: DeckProfile | None = None,
    plan_progress: dict | None = None,
) -> tuple[str, dict]:
    """构造 (system, user_payload)：完整的双方局面 + 中文卡牌效果文本 + 整局计划。

    旧版只给「当前手牌 + 当前场面」，模型每回合从零判断，必然退化成单回合
    贪心解场（把任务组件、终结手段、唯一解牌随手打掉）。现在把跨回合稳定的
    「【整局计划】」放在 payload 最前面，回合建议必须服从它。
    """
    foe_pid = 3 - my_pid
    me = recon.player_view(my_pid)
    foe = recon.player_view(foe_pid)

    def hand_entry(i: int, e) -> dict:
        d = describe_entity(e, card_db, index=i + 1)
        atk, hp = e.stats
        d["当前攻血"] = f"{atk}/{hp}"
        return d

    # 实时 tail 日志时，起手/新抽的手牌卡号会晚于回合开始才由 SHOW_ENTITY 写入
    # （离线全量回放会把后来的卡号回写进同一实体，看起来「全部已知」，是未来信息）。
    # 此刻不能把「实体N」丢给模型瞎猜，但可以用套牌表扣除局面里已揭示的牌，
    # 给出一个带费用/效果的概率候选池，让模型仍能做资源层面的整局规划。
    unknown_hand_count = sum(1 for e in me.hand if not e.card_id)

    def unknown_hand_pool() -> dict | None:
        if unknown_hand_count <= 0 or not deck or not deck.size or not card_db:
            return None
        revealed_names = {}
        for ent in recon.entities.values():
            c = card_db.by_id(ent.card_id) if ent.card_id else None
            if c and c.name in deck.total:
                revealed_names[c.name] = revealed_names.get(c.name, 0) + 1
        candidate_rows = []
        for card_name, total in deck.total.items():
            left = total - revealed_names.get(card_name, 0)
            if left <= 0:
                continue
            c = card_db.by_name(card_name)
            if not c:
                continue
            candidate_rows.append({
                "名称": card_name,
                "套牌剩余": left,
                "法力消耗": c.cost,
                "类型": TYPE_LABELS.get(c.type, c.type),
                "效果": (c.text or "无额外效果")[:90],
            })
        candidate_rows.sort(key=lambda x: (x["法力消耗"], x["名称"]))
        return {
            "未揭示张数": unknown_hand_count,
            "说明": (
                "客户端还没把这几张手牌的卡号写进日志，无法确定每个编号对应哪张牌。"
                "下面是把套牌表扣除局面中已揭示的牌之后剩下的候选，只是概率参考，不是手牌内容。"
            ),
            "候选池": candidate_rows[:14],
            "候选池卡种": len(candidate_rows),
            "使用规则": (
                "只用于判断资源倾向（还剩几张低费牌、AOE 是否已见、终结组件是否可能到手）；"
                "严禁把某个候选指派给某个手牌编号，也严禁据此写出具体出牌步骤。"
            ),
        }

    def board_entry(e, mine: bool) -> dict:
        """场面随从条目。**必须显式给出本回合能否攻击**，不能让模型默认能打。

        旧版只在「有限制」时输出一个含混的「本回合已行动/无法攻击」，
        没有该字段的模型会默认随从可攻击；对手随从带上同样措辞又会让模型
        以为它们下回合也动不了。这里按视角分开输出，并区分刚召唤 / 已攻击 /
        冻结 / 突袭（可打随从但不能打脸）。
        """
        d = describe_entity(e, card_db)
        atk, hp = e.stats
        d["站位"] = e.zone_pos
        d["当前攻血"] = f"{atk}/{hp}"
        d["当前生效关键词"] = e.mechanics()
        st = e.attack_status()
        d["刚召唤"] = "是" if st["just_summoned"] else "否"

        if mine:
            if st["can_attack_hero"]:
                d["本回合能否攻击"] = "是"
            elif st["can_attack"]:
                d["本回合能否攻击"] = "仅能攻击随从（突袭）"
            else:
                d["本回合能否攻击"] = "否"
            d["能否打脸"] = "是" if st["can_attack_hero"] else "否"
            d["状态依据"] = st["reason"]
            if not st["can_attack"]:
                # 保留旧字段名，便于既有消费方/测试兼容，但换成明确原因
                d["当前限制"] = [st["reason"]]
        else:
            # 现在是我方回合，对手随从本来就不能攻击；
            # 关键信息是「轮到对手时是否就绪」，避免用「本回合已行动」误导。
            d["对手下回合能否攻击"] = (
                "是" if st["ready_next_turn"] else "否（仍被冻结或受效果限制）"
            )
            if not st["ready_next_turn"]:
                d["状态说明"] = "被冻结或受效果限制，下个对手回合仍可能无法攻击"
            elif st["just_summoned"]:
                d["状态说明"] = "刚召唤，下个对手回合开始即可攻击，需提前防范"
            else:
                d["状态说明"] = "下个对手回合可攻击"
        return d

    def location_entry(e) -> dict:
        d = describe_entity(e, card_db)
        d["站位"] = e.zone_pos
        d["剩余耐久"] = e.stats[1]
        d["本回合是否可用"] = "否（已进入冷却）" if e.location_used else "是"
        return d

    def hero_power_entry(v) -> dict | None:
        e = v.hero_power
        if e is None:
            return None
        d = describe_entity(e, card_db)
        d["法力消耗"] = e.cost
        dmg = e.get_int("CORPUSDAMAGE", 0)
        if dmg:
            d["可造成直伤"] = dmg
        return d

    def weapon_entry(v) -> dict | None:
        e = v.weapon
        if e is None:
            return None
        d = describe_entity(e, card_db)
        d["当前攻击力"] = e.stats[0]
        d["剩余耐久"] = e.stats[1]
        return d

    def secrets_entry(v, mine: bool) -> dict:
        d = {"数量": len(v.secrets)}
        if mine and v.secrets:
            d["已知奥秘"] = [
                {"名称": e.display_name, "卡号": e.card_id} for e in v.secrets
            ]
        else:
            d["说明"] = "对手奥秘内容在日志中不可见，只能按职业与常见奥秘防范"
        return d

    def deck_count(v) -> Any:
        if v.deck_count_known:
            return v.deck_count
        return "未知（客户端日志不记录未揭示的牌库牌，不能据此推断疲劳或牌库已空）"

    finished = (
        recon.step in ("FINAL_GAMEOVER", "GAME_OVER")
        or recon.state in ("COMPLETE", "GAME_OVER")
        or recon.idle_seconds > 90
    )

    hero_name_me = _readable(me.hero.display_name, card_db) if me.hero else "未建立"
    hero_name_foe = _readable(foe.hero.display_name, card_db) if foe.hero else "未建立"

    # 新对局开局（或切换日志后的瞬间）英雄实体尚未建立、HEALTH 还没写进
    # 日志，hero_hp 会读到 0。这个 0 是**缺数据**，不是英雄真的死了：
    # 实测模型据此回复「对手生命为 0，对局已结束，无需再出牌」，
    # 而状态条下一秒就显示对手 28 血。必须把「数据未就绪」显式告知模型。
    hp_ready = bool(me.hero and foe.hero and me.hero_hp > 0 and foe.hero_hp > 0)
    hp_unready_in_game = (not hp_ready) and (not finished)
    hp_unknown = "未知（血量数据未就绪，禁止按 0 血判断）"

    # 战况日志里 UNKNOWN ENTITY 占位刷屏，过滤后只留有效事件
    recent = [
        line for line in recon.battle_log[-60:]
        if "UNKNOWN ENTITY" not in line
    ][-25:]

    payload: dict[str, Any] = {
        "阶段": phase,
        "局面": {
            "回合": recon.turn,
            "步骤": recon.step or "未知",
            "模式": deck.format_name if deck and deck.format_name else "未知",
            "可用法力": me.mana,
            "本回合可用法力说明": "plan 中所有步骤的法力消耗累计不得超过此数值",
            "轮到我方行动": recon.current_player == my_pid and recon.step == "MAIN_ACTION",
            "血量数据": (
                "已就绪" if hp_ready else (
                    "未就绪：英雄血量尚未从日志读到，但日志显示对局进行中；0 是缺数据，不是真的 0 血"
                    if hp_unready_in_game else
                    "未就绪：对局已结束，此快照仅用于复盘"
                )
            ),
            "对局状态": (
                "已结束或已离开对局（复盘快照，不要给本回合出牌建议）"
                if finished else "进行中"
            ),
        },
        "我方": {
            "职业英雄": hero_name_me,
            "生命": me.hero_hp if (hp_ready or finished) else hp_unknown,
            "护甲": me.armor,
            "法力": f"{me.mana}/{me.mana_max}",
            "过载锁定": me.overload_locked,
            "疲劳伤害": me.fatigue,
            "手牌总数": len(me.hand),
            "已揭示手牌数": len(me.hand) - unknown_hand_count,
            "未揭示手牌数": unknown_hand_count,
            "手牌": [hand_entry(i, e) for i, e in enumerate(me.hand)],
            "场面随从": [board_entry(e, True) for e in me.board],
            "场上地标": [location_entry(e) for e in me.locations],
            "英雄技能": hero_power_entry(me),
            "武器": weapon_entry(me),
            "奥秘": secrets_entry(me, True),
            "牌库剩余": deck_count(me),
            "墓地(近期)": [describe_entity(e, card_db) for e in me.graveyard[:8]],
        },
        "对手": {
            "职业英雄": hero_name_foe,
            "生命": foe.hero_hp if (hp_ready or finished) else hp_unknown,
            "护甲": foe.armor,
            "法力": f"{foe.mana}/{foe.mana_max}",
            "场面随从": [board_entry(e, False) for e in foe.board],
            "场上地标": [location_entry(e) for e in foe.locations],
            "英雄技能": hero_power_entry(foe),
            "武器": weapon_entry(foe),
            "手牌数": len(foe.hand),
            "手牌说明": "对手手牌内容客户端不可见，只有被揭示的牌才写进日志",
            "牌库剩余": deck_count(foe),
            "奥秘": secrets_entry(foe, False),
            "墓地(近期)": [describe_entity(e, card_db) for e in foe.graveyard[:8]],
        },
        "近期战况": recent or recon.events[-25:],
        "数据说明": {
            "卡牌效果": "来自中文卡表，按日志卡号精确匹配；标注「按卡名兜底」或「未命中」时仅供参考",
            "手牌编号": "每条目「编号」是手牌从左到右的顺序，与日志 zonePos 无关",
            "地标": "不是随从，不能攻击、有耐久、通常每回合限用一次",
            "卡面未知": "牌确实在对应区域，只是日志未记录卡号，不代表该区域为空；带此标记的编号禁止写进出牌步骤与 hand_hints",
            "未揭示手牌": "见「【未揭示手牌推演】」：只给候选池用于判断资源倾向，不能当作已知手牌",
            "可攻击状态": "「本回合能否攻击」来自日志 EXHAUSTED / NUM_ATTACKS_THIS_TURN / FROZEN 等权威 tag，优先于卡面静态描述",
            "刚召唤": "EXHAUSTED=1 且本回合未攻击＝召唤失调，本回合不能攻击，要等下个自己的回合",
            "血量数据": (
                "「生命」为「未知」时表示英雄实体 / HEALTH 尚未写入日志而对局仍在进行；"
                "禁止据此断言对局已结束，禁止按 0 血计算斩杀或疲劳"
            ),
        },
    }

    # 未揭示手牌的候选池放在靠前位置（整局计划之前），让模型先建立正确的
    # 「哪些信息不可用」认知，再读战略和局面，避免顺着卡面未知去编造步骤。
    unknown_pool_block = unknown_hand_pool()
    if unknown_pool_block:
        payload = {"【未揭示手牌推演】": unknown_pool_block, **payload}

    # 整局计划放在「阶段/局面」之前：模型先看到战略，再看本回合细节。
    # 血量告警的重排发生在这之后，因此最终顺序是
    # 告警 → 整局计划 → 计划进度 → 局面 → ...，两者都保证靠前。
    if plan is not None:
        my_turn = my_turn_of(recon.turn)
        plan_block: dict[str, Any] = {"【整局计划】": plan.to_prompt_dict(my_turn)}
        if plan_progress:
            plan_block["【计划进度】"] = plan_progress
        payload = {**plan_block, **payload}

    if hp_unready_in_game:
        # 放在 payload 最前面：模型第一眼就能看到，避免被「生命: 0」带偏。
        payload = {
            "【数据告警】血量未就绪": {
                "现象": "英雄实体尚未建立或血量读数为 0，但日志步骤 / 状态显示对局仍在进行中",
                "禁止": [
                    "禁止断言「对局已结束」「已分胜负」「双方 / 对手生命为 0」",
                    "禁止回复「无需出牌」「只能空过」「不需要操作」",
                    "禁止按 0 血计算斩杀、疲劳或血量差；本回合不要给斩杀结论",
                ],
                "应当": [
                    "把双方血量视为未知，仍按法力、手牌、场面与卡牌效果给出本回合正常出牌建议",
                    "斩杀只认「【系统预判】斩杀机会」；可用攻击仅用于交换判断",
                ],
            },
            **payload,
        }

    if deck and deck.size:
        deck_dict = deck.to_dict()
        if profile is not None:
            deck_dict["主题线索"] = profile.themes
            if profile.unidentified:
                deck_dict["卡表缺口"] = profile.unidentified
        if plan is not None:
            # 完整卡表已在慢层分析过一次；回合层只带剩余与关键牌，控制延迟
            deck_dict["说明"] = "完整卡表已在整局计划阶段分析；此处只列剩余与关键牌"
        payload["我的套牌"] = deck_dict

    # 给模型一个不会算错的「本回合可用攻击」锚点。
    # 只靠随从条目里的文字，模型仍会习惯性地把场面总攻击力当成本回合伤害。
    ready_all = [(e, e.attack_status()) for e in me.board]
    payload["【系统预判】本回合可用攻击"] = {
        "可打脸随从": [
            {"名称": e.display_name, "攻击力": e.stats[0], "站位": e.zone_pos}
            for e, st in ready_all if st["can_attack_hero"]
        ],
        "可打脸伤害（未计对手嘲讽）": sum(
            e.stats[0] for e, st in ready_all if st["can_attack_hero"]
        ),
        "仅能攻击随从（突袭）": [
            e.display_name for e, st in ready_all
            if st["can_attack"] and not st["can_attack_hero"]
        ],
        "本回合不能攻击": [
            {"名称": e.display_name, "攻击力": e.stats[0], "原因": st["reason"]}
            for e, st in ready_all if not st["can_attack"]
        ],
        "需先处理的对手嘲讽": [
            e.display_name for e in foe.board if "嘲讽" in e.mechanics()
        ],
        "说明": (
            "斩杀与随从交换只按上面「可打脸随从 / 可攻击随从」计算；"
            "刚召唤（召唤失调）、本回合已攻击、被冻结、受效果限制的随从"
            "本回合伤害一律为 0，只能作为下回合威胁"
        ),
    }

    lethal = detect_lethal(recon, my_pid)
    if lethal:
        payload["【系统预判】斩杀机会"] = lethal
    payload["【系统预判】费用约束"] = {
        "可用法力": me.mana,
        "手牌费用": {
            x["编号"]: x.get("法力消耗", "未知")
            for x in payload["我方"]["手牌"]
        },
        "说明": "操作序列逐步累加不得超过可用法力；实测模型会把 3+5 算成 7 费内，故显式给出",
    }
    combos = legal_play_combos(payload["我方"]["手牌"], me.mana)
    if combos:
        payload["【系统预判】合法费用组合"] = {
            "可用法力": me.mana,
            "候选": combos,
            "说明": "这些组合的费用合计均不超可用法力；可直接采用，或调整后重新核算",
        }
    payload["【系统预判】我方场面价值"] = estimate_board_value(me)
    # 对手随从在我方回合普遍 EXHAUSTED=1，必须按「下个对手回合」评估威胁
    payload["【系统预判】对手场面价值"] = estimate_board_value(foe, next_turn=True)

    user = json.dumps(payload, ensure_ascii=False, indent=1)
    return SYSTEM_PROMPT, user


# ─────────────────────────────────────────────────────────────
# 客户端
# ─────────────────────────────────────────────────────────────

class LLMClient:
    """OpenAI 兼容接口客户端。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = OpenAI(
            api_key=cfg.llm.api_key or "sk-placeholder",
            base_url=cfg.llm.base_url,
            timeout=cfg.llm.timeout,
            max_retries=cfg.llm.max_retries,
        )

    def available(self) -> bool:
        return bool(self.cfg.llm.api_key)

    def chat_messages(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        secrets: dict[str, str] | None = None,
    ) -> str:
        """**所有模型请求的统一出口**：发送前统一脱敏。

        把脱敏放在这里，而不是散落在 build_prompt / planner 里，是为了保证
        以后新增的任何调用路径都不会漏掉（整局规划层也走 chat_json）。
        返回新列表，不改动调用方持有的 messages。
        """
        payload = (
            scrub_messages(messages, secrets)
            if self.cfg.privacy.redact
            else messages
        )
        resp = self._client.chat.completions.create(
            model=self.cfg.llm.model,
            messages=payload,
            temperature=self.cfg.llm.temperature,
            max_tokens=max_tokens or self.cfg.llm.max_tokens,
        )
        return resp.choices[0].message.content or ""

    def chat_json(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        secrets: dict[str, str] | None = None,
    ) -> str:
        """一次性 JSON 请求。整局规划层复用同一个客户端与模型配置。"""
        return self.chat_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            secrets=secrets,
        )

    def advise(
        self,
        recon: GameStateReconstructor,
        card_db: CardDB,
        deck: DeckTracker | None,
        my_pid: int,
        phase: str = "play",
        plan: GamePlan | None = None,
        profile: DeckProfile | None = None,
        plan_progress: dict | None = None,
    ) -> Advice:
        """请求军师建议。失败时降级为本地启发式，不抛异常。"""
        system, user = build_prompt(
            recon, card_db, deck, my_pid, phase,
            plan=plan, profile=profile, plan_progress=plan_progress,
        )
        secrets = collect_secrets(recon, self.cfg) if self.cfg.privacy.redact else {}

        if not self.available():
            adv = self._fallback(recon, my_pid, phase)
            self._apply_plan(adv, plan, recon)
            return adv

        try:
            content = self.chat_json(system, user, secrets=secrets)
            adv = self._parse(content)
            self._run_validators(adv, recon, my_pid)

            # 校验器驳回 → 把具体原因回传模型自动改正一次。
            # 复用 system + user + 原输出作为前缀，只追加一条修正指令，
            # 服务商的 prompt 前缀缓存可直接命中，额外延迟约一次短请求。
            if adv.rejections and self.cfg.llm.auto_correct:
                fixed = self._revise(
                    system, user, content, adv.rejections,
                    secrets, recon, my_pid,
                )
                if fixed is not None:
                    adv = fixed
                else:
                    adv.warnings.insert(
                        0, "自动改正未通过，以下为本地校验后的方案"
                    )

            adv.rejections = []  # 内部字段，不展示给用户
            self._apply_plan(adv, plan, recon)
            return adv
        except Exception as e:  # 网络/额度/格式问题都降级，不打断使用
            adv = self._fallback(recon, my_pid, phase)
            adv.warnings.insert(0, f"LLM 调用失败（{type(e).__name__}），以下为本地启发式建议")
            self._apply_plan(adv, plan, recon)
            return adv

    def _run_validators(
        self, adv: Advice, recon: GameStateReconstructor, my_pid: int
    ) -> None:
        """依次跑全部校验器；它们一边修正建议，一边累积可解释的驳回原因。"""
        adv.rejections = []
        # 解析失败也算一次「驳回」：让模型重发合法 JSON，比直接降级可用率高。
        if adv.summary == "模型输出格式异常，仅供参考":
            adv.rejections.append("输出不是合法 JSON，无法解析；请严格按格式输出")
        self._validate_mana(adv, recon.player_view(my_pid).mana)
        self._validate_attack(adv, recon, my_pid)
        self._validate_readiness(adv, recon, my_pid)
        self._validate_unknown_hand(adv, recon, my_pid)

    def _revise(
        self,
        system: str,
        user: str,
        original: str,
        rejections: list[str],
        secrets: dict[str, str],
        recon: GameStateReconstructor,
        my_pid: int,
    ) -> Advice | None:
        """校验器驳回后自动改正**一次**；仍不通过就返回 None，绝不无限重试。"""
        reasons = "\n".join(f"- {r}" for r in rejections[:6])
        correction = (
            "上面的建议被本地校验器驳回，逐条原因如下：\n"
            + reasons
            + "\n\n请只输出修正后的 JSON（不要解释、不要 markdown 代码块），"
            "字段与原来一致：summary / lethal / plan / warnings / hand_hints / plan_step。"
            "要求：逐条消除上述驳回原因；保留原本正确的步骤与【整局计划】的战略；"
            "不要引入新的费用、攻击资格或未知手牌问题。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": original},
            {"role": "user", "content": correction},
        ]
        try:
            content = self.chat_messages(messages, secrets=secrets)
            fixed = self._parse(content)
            self._run_validators(fixed, recon, my_pid)
            return None if fixed.rejections else fixed
        except Exception:
            return None

    @staticmethod
    def _apply_plan(adv: Advice, plan: GamePlan | None, recon: GameStateReconstructor) -> None:
        """把整局计划落到建议文本上，并做一次温和的资源纪律提醒。

        只提醒、不拦截：模型可能在正确的终结回合打出「必须保留」的牌，
        硬拦会误伤。真正的战略约束放在系统提示词规则 19/20。
        """
        if plan is None:
            return
        my_turn = my_turn_of(recon.turn)
        adv.plan_line = plan.short_line(my_turn)
        if not adv.plan_step:
            goal = str(plan.current_stage(my_turn).get("goal", ""))[:44]
            adv.plan_step = f"阶段目标：{goal}" if goal else "按整局计划推进"
        text = " ".join(adv.plan)
        finish_words = ("斩杀", "致命", "终结", "收尾", "结束战斗")
        if not adv.lethal and not any(w in text for w in finish_words):
            # 必须是「打出 / 使用」某张需保留的牌才算动用。
            # 实测误报：「保留法力，等下回合配合升腾使用」被当成打掉了升腾，
            # 因此带保留 / 延后语义的步骤一律跳过。
            used = []
            for name in plan.keep_cards:
                if not name:
                    continue
                for step in adv.plan:
                    if name not in step:
                        continue
                    if not any(v in step for v in ("打出", "使用", "拍下", "交出", "消耗")):
                        continue
                    if any(v in step for v in ("保留", "留到", "不要", "别用", "暂不", "等下回合", "下回合再")):
                        continue
                    used.append(name)
                    break
            if used:
                adv.warnings.append(
                    "整局计划标记为需保留：" + "、".join(used[:3])
                    + "；确认这一步非用不可，否则留到关键回合"
                )

    @staticmethod
    def _validate_unknown_hand(
        adv: Advice, recon: GameStateReconstructor, my_pid: int
    ) -> None:
        """返回前硬校验：卡面未知的手牌不得被写成具体出牌步骤或 hand_hints。

        实测事故：实时跟读时起手牌卡号晚于回合开始才写入日志，payload 里出现
        「实体14 / 卡面未知」，模型据此编出「#1 实体14：若为低费随从可本回合打出」
        这种毫无信息量的建议。提示词规则 6 已禁止，这里再兜底一次。
        """
        hand = recon.player_view(my_pid).hand
        unknown = {i + 1 for i, e in enumerate(hand) if not e.card_id}
        if not unknown:
            return

        removed_hints = 0
        if adv.hand_hints:
            kept = []
            for h in adv.hand_hints:
                try:
                    idx = int(h.get("index", 0))
                except (TypeError, ValueError):
                    idx = 0
                nm = str(h.get("name", ""))
                if (
                    idx in unknown
                    or "卡面未知" in nm
                    or "未揭示" in nm
                    or re.search(r"实体\d+", nm)
                ):
                    continue
                kept.append(h)
            removed_hints = len(adv.hand_hints) - len(kept)
            adv.hand_hints = kept

        guess = re.compile(
            r"若为|若是|如果是|假如|可能为|需按实际费用|视实际费用|可本回合打出"
        )

        def clean(steps: list[str]) -> list[str]:
            return [
                s for s in steps
                if not re.search(r"实体\d+", str(s)) and not guess.search(str(s))
            ]

        plan_before = len(adv.plan)
        warn_before = len(adv.warnings)
        adv.plan = clean(adv.plan)
        adv.warnings = clean(adv.warnings)
        removed_steps = (
            plan_before - len(adv.plan) + warn_before - len(adv.warnings)
        )
        if removed_hints or removed_steps:
            adv.rejections.append(
                f"引用了卡面未揭示的手牌（编号 {sorted(unknown)}）或写了「若为…」这类条件猜测；"
                "hand_hints 只允许带卡号的编号，未知牌不能出现在具体出牌步骤里"
            )
        if not adv.plan:
            adv.plan = [
                "按【整局计划】使用已揭示手牌与场面资源行动；"
                "未揭示手牌等日志写入卡号后再安排具体出牌"
            ]
        adv.warnings.insert(
            0,
            f"日志尚未揭示 {len(unknown)} 张手牌的卡号，本回合建议只基于已揭示信息；"
            "已移除针对未知牌的猜测步骤",
        )

    @staticmethod
    def _validate_readiness(adv: Advice, recon: GameStateReconstructor, my_pid: int) -> None:
        """返回前兜底：血量数据未就绪时，撤销「对局已结束 / 只能空过」的误判。

        实测事故：新局开局瞬间英雄 HEALTH 还没写进日志，payload 里血量读到 0，
        模型据此回复「对手生命为 0，对局已结束，无需再出牌」，而下一秒
        状态条就显示对手 28 血。提示词里已有规则 18，这里再做一层硬保护。
        """
        finished = (
            recon.step in ("FINAL_GAMEOVER", "GAME_OVER")
            or recon.state in ("COMPLETE", "GAME_OVER")
            or recon.idle_seconds > 90
        )
        me = recon.player_view(my_pid)
        foe = recon.player_view(3 - my_pid)
        hp_ready = bool(me.hero and foe.hero and me.hero_hp > 0 and foe.hero_hp > 0)
        if finished or hp_ready:
            return

        bad_words = (
            "对局已结束", "已结束", "已分胜负", "生命为 0", "生命为0",
            "双方生命", "无需出牌", "无需再出牌", "只能空过", "空过",
            "不需要出牌", "不需要操作", "无需操作", "结束回合",
        )
        negations = ("不要", "禁止", "不得", "并非", "尚未刷新", "不代表")

        def bad(s: str) -> bool:
            # 「不要空过」「并非对局已结束」是正确表述，不能误判
            if any(n in s for n in negations):
                return False
            return any(w in s for w in bad_words)

        if not any(bad(s) for s in [adv.summary] + adv.plan):
            return

        adv.rejections.append(
            "英雄血量只是尚未从日志读到（读数 0 不代表真实 0 血），对局仍在进行中；"
            "禁止断言「对局已结束 / 已分胜负 / 无需出牌 / 只能空过」，"
            "也不许按 0 血算斩杀，仍要给本回合可执行的出牌建议"
        )
        adv.warnings.insert(
            0,
            "⚠️ 英雄血量尚未从日志读到（读数 0 不代表真实血量），而对局仍在进行中；"
            "已忽略模型关于「对局已结束 / 0 血 / 只能空过」的误判",
        )
        adv.summary = "血量数据尚未刷新，按当前法力、手牌与场面给出本回合建议"
        adv.lethal = False
        adv.plan = [s for s in adv.plan if not bad(s)] or [
            f"按 {me.mana} 费与 {len(me.hand)} 张手牌正常出牌，优先处理场面威胁；"
            "血量数据刷新后再判断斩杀",
        ]

    @staticmethod
    def _validate_attack(adv: Advice, recon: GameStateReconstructor, my_pid: int) -> None:
        """返回前兜底校验：模型仍可能让刚召唤的随从去攻击。

        提示词里已明令禁止，但实测 LLM 会照着「场面总攻击力」凑斩杀。
        这里按日志权威状态复核：若 plan 里出现「不可攻击随从 + 攻击动词」，
        把警告置顶；若系统预判根本没有斩杀，同时撤销模型自己标的 lethal。
        """
        me = recon.player_view(my_pid)
        blocked = [
            (e.display_name, e.attack_status()["reason"])
            for e in me.board if not e.attack_status()["can_attack"]
        ]
        real_lethal = detect_lethal(recon, my_pid)
        if not blocked and real_lethal:
            return

        verbs = ("攻击", "打脸", "撞", "换掉", "交换", "解掉", "斩杀", "打英雄", "踢", "撞脸")
        negations = ("不能", "不要", "无法", "下回合", "等下", "留到", "不可")
        bad_steps = [
            step for step in adv.plan
            if any(name and name in step for name, _ in blocked)
            and any(v in step for v in verbs)
            and not any(n in step for n in negations)
        ]
        if bad_steps:
            adv.rejections.append(
                "方案让本回合不能攻击的随从参与了攻击 / 交换："
                + "、".join(n for n, _ in blocked)
                + "；请改用「【系统预判】本回合可用攻击」里的随从，或把这一步留到下回合"
            )
            adv.warnings.insert(
                0,
                "⚠️ 方案里含本回合不能攻击的随从："
                + "；".join(f"{n}（{r}）" for n, r in blocked)
                + "。这几只本回合不能打，请改用「【系统预判】本回合可用攻击」里的随从，或留到下回合",
            )
        if adv.lethal and not real_lethal:
            adv.rejections.append(
                "本地按日志权威状态复核后没有斩杀（刚召唤 / 已攻击 / 冻结的随从不能打），"
                "lethal 必须为 false"
            )
            adv.lethal = False
            adv.warnings.insert(
                0,
                "⚠️ 本回合可用伤害不足以斩杀（刚召唤/已攻击/冻结的随从不能打），"
                "已撤销斩杀标记，请按场面交换或铺场处理",
            )

    @staticmethod
    def _validate_mana(adv: Advice, mana: int) -> None:
        """返回前兜底校验：模型仍可能算错费用，超费时把警告放到最前面。

        系统提示词要求每步写成「（N费）」，因此可直接从 plan 文本汇总费用。
        """
        costs = [
            int(x)
            for x in re.findall(r"[（(](\d+)\s*费[）)]", "\n".join(adv.plan))
        ]
        total = sum(costs)
        if total > mana:
            adv.rejections.append(
                f"方案标注的费用合计 {total} 费，超过本回合可用 {mana} 费；"
                "删掉最贵的一步或改用低费牌，并在每步注明费用"
            )
            adv.warnings.insert(
                0,
                f"⚠️ 方案超费：操作序列标注的费用合计 {total} 费，"
                f"超过本回合可用 {mana} 费，请删减后再执行",
            )

    def _parse(self, content: str) -> Advice:
        """从模型输出里抽出 JSON，容忍 markdown 包裹与前后废话。"""
        data = None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return Advice(raw=content, summary="模型输出格式异常，仅供参考")

        return Advice(
            plan=_as_str_list(data.get("plan")),
            warnings=_as_str_list(data.get("warnings")),
            hand_hints=_as_hints(data.get("hand_hints")),
            lethal=bool(data.get("lethal")),
            summary=str(data.get("summary", "")),
            plan_step=str(data.get("plan_step", ""))[:60],
            raw=content,
        )

    def _fallback(self, recon: GameStateReconstructor, my_pid: int, phase: str) -> Advice:
        """不依赖 LLM 的兜底建议（斩杀检测 + 场面原则）。"""
        lethal = detect_lethal(recon, my_pid)
        if lethal:
            return Advice(
                summary="检测到斩杀机会，按下列顺序执行",
                lethal=True,
                plan=lethal["sources"] or ["用可攻击的随从全部打脸"],
                warnings=(
                    ["注意先处理嘲讽随从"] if lethal["blockers"] else []
                ) + (
                    ["以下随从本回合不能攻击，未计入：" + "、".join(lethal["excluded_minions"])]
                    if lethal.get("excluded_minions") else []
                ),
            )
        me = recon.player_view(my_pid)
        foe = recon.player_view(2 if my_pid == 1 else 1)
        plan: list[str] = []
        if not self.available():
            plan.append("未配置 LLM API Key，以下为本地基础建议")
        # 只比较本回合真正能打出的攻击力，别把刚召唤的随从算成战力
        my_board = sum(e.stats[0] for e in me.board if e.attack_status()["can_attack"])
        foe_board = sum(e.stats[0] for e in foe.board)
        if my_board > foe_board and me.hand:
            plan.append("场面占优，可用能攻击的随从交换或直接打脸推进")
        elif me.board:
            plan.append("场面劣势，优先用低费牌解场，保留高费牌")
        else:
            plan.append("场面已空，考虑用随从或法术重新建立优势")
        excluded = [
            e.display_name for e in me.board if not e.attack_status()["can_attack"]
        ]
        if excluded:
            plan.append("本回合不能攻击（留到下回合）：" + "、".join(excluded))
        return Advice(summary="本地启发式建议（未接入 LLM）", plan=plan)


def _as_str_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v]
    return []


def _as_hints(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        if isinstance(item, dict) and "index" in item:
            out.append(
                {
                    "index": item.get("index"),
                    "name": item.get("name", ""),
                    "reason": item.get("reason", ""),
                }
            )
    return out