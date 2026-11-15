"""
炉石助手 · Power.log 解析与状态还原

炉石客户端把对战中发生的一切写入 Power.log（需 log.config 开 [Power]）。
本模块把文本流还原成结构化 :class:`GameState`。

日志形态（实测，缩进不规整，**不能靠缩进判断层级**，改用动作名白名单）：

    D 20:57:25.1 PowerTaskList.DebugPrintPower() -     CREATE_GAME
    D 20:57:25.1 PowerTaskList.DebugPrintPower() -         GameEntity EntityID=1
    D 20:57:25.1 PowerTaskList.DebugPrintPower() -             tag=CARDTYPE value=GAME
    D 22:15:36.7 GameState.DebugPrintPower() -TAG_CHANGE Entity=[entityName=扛包收尸人 id=242 zone=PLAY zonePos=6 cardId=RLK_503 player=1] tag=DAMAGE value=2
    D 20:57:25.1 PowerTaskList.DebugPrintPower() - FULL_ENTITY - Creating ID=4 CardID=
    D 20:57:25.1 PowerTaskList.DebugPrintPower() -     tag=ZONE value=DECK
    D 20:59:35.8 GameState.DebugPrintPower() -     META_DATA - Meta=TARGET Data=0 InfoCount=1
    D 20:59:35.8 GameState.DebugPrintPower() -         Info[0] = [entityName=援军 id=65 zone=PLAY zonePos=0 cardId=CS2_101_H2 player=1]

设计要点：
- 流式增量，可 tail 实时跟读，也可离线全量回放
- 实体-标签状态机：局面是tag 状态的投影，不做二次记账
"""

from __future__ import annotations

import collections

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


class T:
    """Game Tag 名称常量。"""

    ZONE = "ZONE"
    ZONE_POSITION = "ZONE_POSITION"
    CARDTYPE = "CARDTYPE"
    ENTITY_ID = "ENTITY_ID"
    CONTROLLER = "CONTROLLER"
    DAMAGE = "DAMAGE"
    ATK = "ATK"
    HEALTH = "HEALTH"
    COST = "COST"
    RESOURCES = "RESOURCES"
    RESOURCES_USED = "RESOURCES_USED"
    TEMP_RESOURCES = "TEMP_RESOURCES"
    OVERLOAD = "OVERLOAD"
    OVERLOAD_OWED = "OVERLOAD_OWED"
    OVERLOAD_LOCKED = "OVERLOAD_LOCKED"
    STATE = "STATE"
    STEP = "STEP"
    TURN = "TURN"
    NUM_TURNS_PLAYED = "NUM_TURNS_PLAYED"
    CURRENT_PLAYER = "CURRENT_PLAYER"
    FIRST_PLAYER = "FIRST_PLAYER"
    NEXT_STEP = "NEXT_STEP"
    PLAYSTATE = "PLAYSTATE"
    PREDAMAGE = "PREDAMAGE"
    TO_BE_DESTROYED = "TO_BE_DESTROYED"
    EXHAUSTED = "EXHAUSTED"
    ATTACKING = "ATTACKING"
    CLASS = "CLASS"
    HERO_POWER_DOUBLE = "HERO_POWER_DOUBLE"
    LINKEDCARD = "LINKEDCARD"
    CARD_TARGET = "CARD_TARGET"
    NUM_CARDS_DRAWN_THIS_TURN = "NUM_CARDS_DRAWN_THIS_TURN"


#: 顶层动作白名单——只有匹配这些的行才开启新动作。
#: ``Player`` / ``GameEntity`` 必须列入：CREATE_GAME 块里它们各自成块，
#: 若当作子行处理，多个实体的 tag 会全部挂到同一动作上，玩家实体将无法识别。
TOP_LEVEL_ACTIONS = frozenset(
    {
        "CREATE_GAME",
        "RESET_GAME",
        "CHANGE_ENTITY",
        "CHANGE_CARD",
        "TAG_CHANGE",
        "FULL_ENTITY",
        "GAME_ENTITY",
        "Player",
        "PLAYER_ENTITY",
        "SHOW_ENTITY",
        "HIDE_ENTITY",
        "BLOCK_START",
        "BLOCK_END",
        "SUB_SPELL_START",
        "SUB_SPELL_END",
        "META_DATA",
        "FULL_EFFECT",
        "STEP",
        "BEGIN_MULLIGAN",
        "BEGIN_RANKING",
        "FORCED_REPLAY",
    }
)

#: 关键词 tag -> 中文。这些直接决定攻防判断，必须喂给 LLM。
MECHANIC_TAGS: dict[str, str] = {
    "TAUNT": "嘲讽",
    "STEALTH": "潜行",
    "DIVINE_SHIELD": "圣盾",
    "WINDFURY": "风怒",
    "FROZEN": "冻结",
    "SILENCED": "沉默",
    "CHARGE": "冲锋",
    "RUSH": "突袭",
    "POISONOUS": "剧毒",
    "LIFESTEAL": "吸血",
    "IMMUNE": "免疫",
    "UNTOUCHABLE": "无法被选中",
    "SPELLPOWER": "法术强度加成",
    "ADJACENT_BUFF": "相邻增益",
}

#: 英雄/玩家实体常用卡型
CARDTYPE_HERO = "HERO"
CARDTYPE_HERO_POWER = "HERO_POWER"

_ZONE_LABELS = {
    "PLAY": "战场",
    "HAND": "手牌",
    "DECK": "牌库",
    "GRAVEYARD": "墓地",
    "SECRET": "奥秘",
    "SETASIDE": "暂存",
    "REMOVEDFROMGAME": "已移出",
}

# ─────────────────────────────────────────────────────────────
# 行级正则
# ─────────────────────────────────────────────────────────────

#: 剥掉 "D 22:15:36.7002360 GameState.DebugPrintPower() - " 前缀
LINE_RE = re.compile(
    r"^[A-Z]\s+(?P<time>[\d:.]+)\s+(?P<src>\w+)\.\w+\(\)\s+-\s?(?P<rest>.*)$"
)

#: Entity=[entityName=援军 id=65 zone=PLAY zonePos=0 cardId=CS2_101_H2 player=1]
#: 注意：entityName 本身可能含方括号，如
#:   Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=4 zone=HAND ...]
#: 所以正文必须用「贪婪匹配到最后一个 ]」并回退，而不是非贪婪 —— 非贪婪会在
#: entityName 内部的 ] 处提前截断，导致 id/cardId/zone 全部解析失败。
ENTITY_BRACKET_RE = re.compile(r"Entity=\[(?P<body>.*)\](?P<tail>\s*tag=.*|.*)$")

#: FULL_ENTITY - Creating ID=4 CardID=CS2_029
CREATING_RE = re.compile(r"- Creating ID=(?P<id>\d+)\s+CardID=(?P<cid>\S*)")

#: FULL_ENTITY - Updating [entityName=... id=7 zone=DECK ...] CardID=XXX
#: **Updating 形态与 SHOW_ENTITY 同义**，都是把隐藏实体的真实卡号写进日志。
#: 实测我方手牌走的是这个形态（``SHOW_ENTITY`` 只覆盖了对手手牌），
#: 漏掉它会导致我方手牌永远没有卡号 —— 模型据此回复「手牌为空，只能空过」。
UPDATING_RE = re.compile(
    r"- Updating Entity=(?:\[(?P<bracket>.*)\]|(?P<id>\d+))\s+CardID=(?P<cid>\S*)"
)

#: SHOW_ENTITY - Updating Entity=38 CardID=TLC_229
#: 也可能是 Entity=[entityName=... id=4 ...] CardID=CS2_558（实体正文带方括号）
SHOWING_RE = re.compile(
    r"- Updating Entity=(?:\[(?P<bracket>.*)\]|(?P<id>\d+))\s+CardID=(?P<cid>\S*)"
)

#: GameEntity EntityID=1 / Player EntityID=2 PlayerID=1 / FullEntity EntityID=3
ENTITY_DECL_RE = re.compile(
    r"^(?P<kind>GameEntity|Player|FullEntity|ShowEntity)\s+EntityID=(?P<id>\d+)"
    r"(?:\s+PlayerID=(?P<pid>\d+))?"
)

#: Player EntityID=2 PlayerID=1 GameAccountId=[hi=144115200000000001 lo=100000001]
#: ``lo`` 是战网账号 ID，在整份日志里唯一且恒定 —— 本机玩家就靠它识别。
ACCOUNT_ID_RE = re.compile(r"GameAccountId=\[hi=(?P<hi>\d+)\s+lo=(?P<lo>\d+)\]")

#: "Entity=28"（TAG_CHANGE 的简写形态，无方括号）
#: 实测还有两种非数字标识：
#:   Entity=GameEntity         —— 全局标签（TURN/STEP/NEXT_STEP）挂在游戏实体上
#:   Entity=玩家甲#12345      —— 玩家名#编号
ENTITY_SHORT_RE = re.compile(r"Entity=(?P<id>\S+)\s*(?P<tail>.*)$")

#: "tag=DAMAGE value=2"
TAG_RE = re.compile(r"^tag=(?P<tag>\w+)\s+value=(?P<value>.+?)\s*$")

#: "META_DATA - Meta=TARGET Data=0 InfoCount=1"
META_RE = re.compile(r"Meta=(?P<meta>\w+)\s+Data=(?P<data>\S*)")

#: "Info[0] = [entityName=援军 id=65 ...]"
INFO_RE = re.compile(r"^Info\[\d+\]\s*=\s*\[(?P<body>.*)\]\s*$")

#: "Entities[0]=[...]"
ENTITIES_RE = re.compile(r"^Entities\[\d+\]\s*=\s*\[(?P<body>.*)\]\s*$")


def parse_entity_body(body: str) -> dict[str, str]:
    """解析 Entity=[...] 的键值对。

    难点：entityName 之后可能跟带空格的中文（如 ``entityName=拟态 i``、``entityName=未选中的卡牌``）。
    策略：定位下一个 ``key=`` 模式作为当前值的结束。
    """
    out: dict[str, str] = {}
    key_re = re.compile(r"(\w+)=")
    pos = 0
    n = len(body)
    while pos < n:
        m = key_re.search(body, pos)
        if not m:
            break
        key = m.group(1)
        val_start = m.end()
        nxt = re.compile(r"\s+\w+=").search(body, val_start)
        val_end = nxt.start() if nxt else n
        out[key] = body[val_start:val_end].strip()
        pos = val_end
    return out


@dataclass
class Entity:
    """一个游戏实体（卡牌、英雄、技能等）的当前状态。"""

    id: int
    card_id: str = ""
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    #: 实体声明阶段读到的 tag（不会被后续 TAG_CHANGE 覆盖）。
    #: CARDTYPE/PLAYER_ID/HERO_ENTITY 只在声明期出现一次，
    #: 若与变更期tag 混存，实体会在收到 PLAYSTATE 等tag 后丢失类型信息。
    declared: dict[str, str] = field(default_factory=dict)
    #: 首次出现顺序，用于站位相同时保持稳定排序
    seq: int = 0

    def get(self, tag: str, default: str = "") -> str:
        v = self.tags.get(tag)
        if v is None:
            v = self.declared.get(tag)
        return v if v is not None else default

    def get_int(self, tag: str, default: int = 0) -> int:
        v = self.tags.get(tag)
        if v is None or v == "":
            v = self.declared.get(tag)
        if v is None or v == "":
            return default
        try:
            return int(float(v))
        except ValueError:
            return default

    @property
    def zone(self) -> str:
        """当前区域。优先用tags（实时值），declared 只是声明期快照。"""
        v = self.tags.get(T.ZONE)
        if v is None:
            v = self.declared.get(T.ZONE)
        return v or ""

    @property
    def zone_pos(self) -> int:
        """站位。同样优先用 tags —— 随从上手时 tags 才有真实 ZONE_POSITION，
        declared 里通常是 None 或旧值（SETASIDE/HAND 阶段的残留）。"""
        v = self.tags.get(T.ZONE_POSITION)
        if v is None or v == "":
            v = self.declared.get(T.ZONE_POSITION)
        if v is None or v == "":
            return 0
        try:
            return int(float(v))
        except ValueError:
            return 0

    @property
    def controller(self) -> int:
        """控制者玩家编号（1 / 2）。"""
        return self.get_int(T.CONTROLLER, 0)

    @property
    def cardtype(self) -> str:
        return self.get(T.CARDTYPE, "")

    #: 由 GameStateReconstructor 回填：玩家实体的显示名（log_parser 侧按槽位写入）
    _pname: str = ""

    @property
    def display_name_override(self) -> str:
        """优先用玩家名（比卡号可读）。"""
        return self._pname or self.display_name

    @property
    def is_hero(self) -> bool:
        return self.cardtype == CARDTYPE_HERO

    @property
    def display_name(self) -> str:
        return self.name or (self.card_id or f"实体{self.id}")

    @property
    def stats(self) -> tuple[int, int]:
        """(攻击力, 当前生命值)。DAMAGE 计入血量。"""
        atk = self.get_int(T.ATK, 0)
        hp = self.get_int(T.HEALTH, 0)
        dmg = self.get_int(T.DAMAGE, 0)
        return atk, max(0, hp - dmg)

    @property
    def cost(self) -> int:
        return self.get_int(T.COST, 0)

    def mechanics(self) -> list[str]:
        """当前生效的关键词状态。"""
        return [label for tag, label in MECHANIC_TAGS.items() if self.get_int(tag, 0) == 1]

    def is_dead(self) -> bool:
        return self.zone in ("GRAVEYARD", "REMOVEDFROMGAME", "")

    @property
    def is_valid_minion(self) -> bool:
        """是否是「场上的随从」（计入场面数与场面列表）。

        必须严格：炉石场上除了随从，还并存多种非随从实体，都不能计入：
        - ``HERO``        英雄本体
        - ``HERO_POWER``  英雄技能
        - ``WEAPON``      武器
        - ``ENCHANTMENT`` 附魔/光环（如 buff 载体）
        - ``LOCATION``    地标（有耐久、不能攻击、每回合限用一次，不是随从）
        - ``PLAYER``      玩家实体
        - 卡号为空或 ``UNKNOWN ENTITY [cardType=INVALID]`` 的占位实体
          （换牌/选牌界面 ``DebugPrintEntityChoices`` 会批量输出）
        - ``INVALID`` / ``GAME`` 等无效卡型

        实测教训：原先只用「卡号非空+ zone=PLAY」判定，会把英雄与技能
        一起算进随从数，导致对方场面数虚高（本例22 个实体里只有 7 个是真随从）。
        后来又发现地标 0/2 被当成「不能攻击的随从」混进场面列表，
        模型据此以为场上多了一个随从，给出的交换与斩杀计算全错。
        """
        # 明确排除的非随从卡型
        if self.cardtype in ("HERO", "HERO_POWER", "WEAPON", "ENCHANTMENT",
                             "PLAYER", "GAME", "INVALID", "LOCATION"):
            return False
        # 占位实体（换牌/选牌界面）
        if not self.card_id:
            return False
        if self.name.startswith("UNKNOWN ENTITY"):
            return False
        # 非 PLAY 区一律不算场面
        if self.zone != "PLAY":
            return False
        return True

    @property
    def is_location(self) -> bool:
        """是否是场上的地标（与随从分列，不能参与攻击/交换）。"""
        return (
            self.cardtype == "LOCATION"
            and self.zone == "PLAY"
            and bool(self.card_id)
        )

    @property
    def location_used(self) -> bool:
        """地标本回合是否已使用（客户端写 LOCATION_ACTION_COOLDOWN=1）。"""
        return self.get_int("LOCATION_ACTION_COOLDOWN", 0) == 1

    def attack_status(self) -> dict:
        """判断这个随从本回合能否攻击、能否打脸，以及原因。

        客户端在随从入场时写 ``tag=EXHAUSTED value=1``（召唤失调），
        在**下个自己的回合开始时**清成 0；攻击后写
        ``NUM_ATTACKS_THIS_TURN``。实测日志：

        ``TAG_CHANGE Entity=[... 光明之翼 id=67 zone=HAND ...] tag=ZONE value=PLAY``
        ``TAG_CHANGE Entity=[... 光明之翼 id=67 ...] tag=EXHAUSTED value=1``

        旧版把这种状态写成含混的「本回合已行动/无法攻击」，而且只在
        状态存在时才输出字段 —— 模型默认所有随从都能打，斩杀计算也把
        刚召唤随从的攻击力全算进去，于是频繁给出「用刚上场的随从打脸」
        这种不可执行的建议。

        返回字段：
        ``can_attack``      本回合能否攻击任意目标
        ``can_attack_hero`` 能否打脸（突袭只能打随从）
        ``just_summoned``   是否刚召唤（对判断下回合威胁有用）
        ``ready_next_turn`` 下个自己的回合开始时是否可用（对手随从视角）
        ``reason``          中文原因，直接喂给 LLM
        ``short``           浮窗用的短标签
        """
        exhausted = self.get_int("EXHAUSTED", 0)
        frozen = self.get_int("FROZEN", 0)
        cant = self.get_int("CANT_ATTACK", 0)
        attacks = self.get_int("NUM_ATTACKS_THIS_TURN", 0)
        turns = self.get_int("NUM_TURNS_IN_PLAY", 0)
        charge = self.get_int("CHARGE", 0) == 1
        rush = (self.get_int("RUSH", 0) == 1
                or self.get_int("ATTACKABLE_BY_RUSH", 0) == 1)
        windfury = self.get_int("WINDFURY", 0) == 1
        extra = self.get_int("EXTRA_ATTACKS_THIS_TURN", 0)
        max_attacks = (2 if windfury else 1) + extra

        # 刚召唤：EXHAUSTED=1、一次都没打过、在场回合不超过 1。
        # NUM_TURNS_IN_PLAY 偶尔缺失，用「疲劳且未攻击」兜底。
        just_summoned = exhausted == 1 and attacks == 0 and (turns <= 1 or turns == 0)

        can = True
        can_hero = True
        reason = "状态正常，本回合可以攻击"
        short = ""
        if frozen:
            can, can_hero = False, False
            reason = "被冻结，本回合不能攻击（需先解冻）"
            short = "冻结"
        elif cant:
            can, can_hero = False, False
            reason = "受效果限制，本回合无法攻击"
            short = "无法攻击"
        elif exhausted:
            if charge:
                reason = "冲锋：刚上场也可以攻击（忽略召唤失调）"
                short = "冲锋"
            elif rush:
                can_hero = False
                reason = "突袭：刚上场只能攻击随从，不能打英雄"
                short = "突袭·只能打随从"
            elif attacks >= max_attacks:
                can, can_hero = False, False
                reason = "本回合已攻击过（攻击次数已达上限）"
                short = "已攻击"
            else:
                can, can_hero = False, False
                if just_summoned:
                    reason = "刚召唤·召唤失调：本回合不能攻击，要等下个自己的回合"
                    short = "刚上场·不能攻击"
                else:
                    reason = "本回合无法攻击（EXHAUSTED），下个自己的回合恢复"
                    short = "本回合不能攻击"
        elif attacks >= max_attacks:
            can, can_hero = False, False
            reason = "本回合已攻击过（攻击次数已达上限）"
            short = "已攻击"

        return {
            "can_attack": can,
            "can_attack_hero": can and can_hero,
            "just_summoned": just_summoned,
            "ready_next_turn": not (frozen or cant),
            "reason": reason,
            "short": short,
            "tags": {
                "EXHAUSTED": exhausted,
                "FROZEN": frozen,
                "CANT_ATTACK": cant,
                "NUM_ATTACKS_THIS_TURN": attacks,
                "NUM_TURNS_IN_PLAY": turns,
                "CHARGE": int(charge),
                "RUSH": int(rush),
                "WINDFURY": int(windfury),
            },
        }

    def to_brief(self) -> dict:
        """给 LLM 用的紧凑表示。"""
        d: dict = {"name": self.display_name}
        if self.card_id:
            d["id"] = self.card_id
        atk, hp = self.stats
        if atk or hp:
            d["stats"] = f"{atk}/{hp}"
        if self.cost:
            d["cost"] = self.cost
        if self.zone_pos:
            d["pos"] = self.zone_pos
        mech = self.mechanics()
        if mech:
            d["mechanics"] = mech
        if self.is_valid_minion:
            st = self.attack_status()
            d["can_attack"] = st["can_attack"]
            d["can_attack_hero"] = st["can_attack_hero"]
            d["just_summoned"] = st["just_summoned"]
            d["attack_reason"] = st["reason"]
        return d


@dataclass
class Action:
    """一条顶层动作及其子标签。"""

    action: str
    time: str = ""
    entity_id: int = 0
    entity_fields: dict[str, str] = field(default_factory=dict)
    tags: list[tuple[str, str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    #: 本动作属于哪个对局（CREATE_GAME 递增）。用于在拆散 CREATE_GAME 块后
    #: 仍能正确识别「对局边界」，而不必把 CREATE_GAME 当成单条动作。
    game_id: int = 0


class PowerLogParser:
    """把 Power.log 文本流解析为 :class:`Action` 序列。

    用法::

        p = PowerLogParser()
        for line in open(path, encoding="utf-8", errors="ignore"):
            act = p.feed_line(line)
            if act:
                handle(act)
    """

    def __init__(self) -> None:
        self._pending: Action | None = None
        self._last_meta: str = ""
        self._game_id = 0
        #: 非数字实体标识 → 虚拟实体 id。GameEntity 是游戏实体（TURN/STEP 挂在它上面）。
        self._named_ids: dict[str, int] = {"GameEntity": 1}
        #:玩家名#编号 的出现顺序（0 起），用于把法力 tag 关联到玩家。
        #: 炉石把玩家的 RESOURCES 等关键 tag 挂在「名#号」实体上，而非玩家实体（2/3），
        #: 且编号无法反推 GameAccountId，因此按首次出现顺序认定为玩家 1、2...
        self._pname_order: list[str] = []
        #: Player 实体 id → PlayerID
        self._player_eid_to_pid: dict[int, int] = {}
        #: 本局带FIRST_PLAYER=1 的「名#号」实体集合
        self._fp_names: set[str] = set()
        #: 本局带 FIRST_PLAYER=1 的 **Player 实体** 对应的 PlayerID
        #: （FIRST_PLAYER 恒为 1，只出现一次，故用单值而非集合）
        self._fp_player_pid: int = 0
        #: FIRST_PLAYER 配对结果：名字 → PlayerID
        self._name_to_pid: dict[str, int] = {}
        #: 玩家名 → 本机身份评分依据：该名字出现过的 PlayerID 集合
        #: （本机账号会随先后手在 1/2 间切换，对手则固定一侧）
        self._name_pids: dict[str, set[int]] = {}
        #: 配置指定的本机玩家名（空则按出现顺序推断）
        self._player_name: str = ""

    def _bind_updating_entity(self, act: Action, tail: str) -> None:
        """处理 ``- Updating Entity=...CardID=xxx``（SHOW_ENTITY 与 FULL_ENTITY 共用）。

        两种 Entity 写法都要支持：
        - ``Entity=38``简写
        - ``Entity=[entityName=UNKNOWN ENTITY [cardType=INVALID] id=7 ...]`` 方括号
        """
        m = UPDATING_RE.search(tail)
        if not m:
            return
        fields: dict[str, str] = {}
        if m.group("bracket") is not None:
            f2 = parse_entity_body(m.group("bracket"))
            raw_id = f2.get("id", "")
            eid = int(raw_id) if raw_id.isdigit() else 0
            fields.update({k: v for k, v in f2.items() if k != "id"})
        else:
            eid = int(m.group("id"))
        # 方括号里的 cardId= 在隐藏实体上恒为空（占位形态），
        # 真实卡号只来自行尾 CardID=xxx。必须最后写入，否则被空串覆盖，
        # 手牌/场面会整批失去卡号，卡表效果文本也随之查不到。
        fields["cardId"] = m.group("cid")
        self._bind_entity(act, eid, fields)

    # ── 内部小工具 ──
    def _resolve_id(self, raw: str) -> int:
        """把 ``Entity=`` 后面的标识解析为整数实体 id。

        实测有三种写法：
        - ``28``             数字实体 id
        - ``GameEntity``     游戏实体，全局标签（TURN/STEP/NEXT_STEP）挂在它上面
        - ``玩家甲#12345``  玩家名#编号，法力等玩家级 tag 挂在它上面

        后两种映射为稳定的虚拟 id（负数），使这些 tag 能被稳定追踪。
        ``player_slot_of`` 提供虚拟 id → 玩家编号 的换算。
        """
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        if raw not in self._named_ids:
            self._named_ids[raw] = -(len(self._named_ids) + 1)
        return self._named_ids[raw]

    def named_vids(self) -> list[int]:
        """本局所有「名#号」虚拟实体 id。"""
        return [vid for name, vid in self._named_ids.items() if "#" in name]

    def vid_of(self, raw: str) -> int:
        """「名#号」原始串→ 虚拟实体 id；不存在返回 0。"""
        return self._named_ids.get(raw, 0)

    def player_name_of(self, entity_id: int) -> str:
        """「名#号」虚拟实体 id → 玩家显示名（不含 #编号）；非玩家实体返回空串。"""
        for name, vid in self._named_ids.items():
            if vid == entity_id and "#" in name:
                return name.rsplit("#", 1)[0]
        return ""

    def set_player_name(self, name: str) -> None:
        """指定本机玩家名（来自 config.player_name）。"""
        self._player_name = (name or "").strip()

    def reset_player_order(self) -> None:
        """新对局开始时清空玩家名顺序表与虚拟 id 映射。

        玩家名在整份日志里会累积（换英雄、观战、多个好友对局），
        所以每个对局都要重新按出现顺序认定「本局的两名玩家」。
        旧对局的「名#号」虚拟 id 也一并作废，否则 recon 侧会残留旧实体。
        """
        self._pname_order.clear()
        self._name_to_pid.clear()
        # 注意：**不清** _fp_names / _fp_player_pid。
        # FIRST_PLAYER 的两侧（Player 实体与「名#号」实体）出现时机相差可达上万行，
        # 且可能跨越 CREATE_GAME 边界（实测名字侧先到、Player 侧在对局中途才到）。
        # 它本身是权威标识而非顺序推断的产物，跨对局保留才不会漏掉配对信息。
        for name in list(self._named_ids):
            if "#" in name:
                del self._named_ids[name]

    def note_player_entity(self, eid: int, pid: str) -> None:
        """记录 Player 实体 id → PlayerID，供 FIRST_PLAYER 绑定用。"""
        if pid.isdigit():
            self._player_eid_to_pid[eid] = int(pid)

    def note_first_player(self, eid: int) -> None:
        """记录本局的先手玩家 PlayerID（``FIRST_PLAYER=1`` 落在 Player 实体上）。

        ``FIRST_PLAYER`` 恒为 1 且每局只出现一次，因此用单值记录。
        该 tag 同时出现在 Player 实体与「名#号」实体上，两者标记同一个人，
        于是构成玩家显示名↔ PlayerID 的权威关联
        （详见 :meth:`resolve_first_player_pairs`）。
        """
        pid = self._player_eid_to_pid.get(eid)
        if not pid:
            return
        self._fp_player_pid = pid
        # 立即回填：此前名字可能已按「出现顺序」兜底绑定过，此处纠正为权威值。
        # 之所以必须当场纠正而不是等结束，是因为玩家级 tag（法力等）在这之前
        # 就已按错误映射同步过，事后修正无法追溯。
        for raw in self._fp_names:
            name = raw.rsplit("#", 1)[0]
            self._name_to_pid[name] = pid
            self._rebind_pname_slot(name, pid)

    def player_slot_of(self, entity_id: int) -> int:
        """「名#号」虚拟实体 id → 本局玩家编号（1/2）；非玩家实体返回 0。

        绑定优先级：
        1. **FIRST_PLAYER 配对**（权威）：``resolve_first_player_pairs()`` 会把
           带``FIRST_PLAYER=1`` 的名字实体与带同一 tag 的 Player 实体配对，
           直接给出该名字的 PlayerID。
        2. **出现顺序**（兜底）：本局内首次出现的前两名依次对应玩家 1、2。
           仅在 FIRST_PLAYER 缺失时使用，且必须按对局重置，否则历史对局的
           玩家名会挤占前两名（实测同一份日志里出现过 7 个玩家名）。
        """
        # 不能靠 _name_of_vid()：它可能先命中一个不含「#」的同值键
        # （历史对局残留），于是明明已有权威配对却直接返回 0。
        # player_name_of() 按虚拟 id 直接定位名字实体，返回纯名字，更可靠。
        name = self.player_name_of(entity_id)
        if not name:
            return 0

        pid = self._name_to_pid.get(name)
        if pid:
            self._name_pids.setdefault(name, set()).add(pid)
            return pid

        if name in self._pname_order:
            slot = self._pname_order.index(name) + 1
            self._name_pids.setdefault(name, set()).add(slot)
            return slot
        # 本局只有两名玩家。只收录前两名，超出的直接忽略——
        # 否则历史对局/观战的其他玩家名会挤进来，导致本局玩家被认成 3/4 号。
        if len(self._pname_order) >= 2:
            return 0
        self._pname_order.append(name)
        slot = len(self._pname_order)
        self._name_pids.setdefault(name, set()).add(slot)
        return slot

    def _rebind_pname_slot(self, name: str, pid: int) -> None:
        """纠正某玩家名的 PlayerID 归属，并通知recon 迁移已同步的玩家级 tag。

        FIRST_PLAYER 的两侧出现顺序不固定：若名字先到，它会先按「出现顺序」
        被兜底绑定（可能错），并把 ``RESOURCES`` 等 tag 同步到错误的 Player 实体。
        因此在权威信息到齐的当下就要纠正，事后修正无法追溯已同步的数据。
        """
        # 修正出现顺序表：把该名字放到 pid-1 的位置
        if name in self._pname_order:
            self._pname_order.remove(name)
        while len(self._pname_order) < pid - 1:
            self._pname_order.append("")
        if pid - 1 < len(self._pname_order):
            self._pname_order[pid - 1] = name
        else:
            self._pname_order.append(name)

        hook = getattr(self, "_on_rebind", None)
        if hook is not None:
            hook(name, pid)

    def _vid_of_name(self, name: str) -> int | None:
        for raw, vid in self._named_ids.items():
            if "#" in raw and raw.rsplit("#", 1)[0] == name:
                return vid
        return None

    def seed_from_scan(self, sides: dict[str, set[int]], cur_pid_hint: int = 0) -> None:
        """用全局扫描结果（:func:`scan_player_name_sides`）预置本局名字→pid 映射。

        背景：``CREATE_GAME`` 会清空「名#号」虚拟 id，解析器只能看到**当前局**的名字，
        而判定本机需要**跨对局**的统计（哪个名字横跨 PlayerID 1/2）。
        因此启动时先做一次全局扫描，把「本机是谁」这一结论注入解析器，
        后续解析就有了权威依据，不必依赖不可靠的出现顺序。
        """
        cands = [nm for nm, pids in sides.items() if len(pids) > 1]
        if len(cands) != 1:
            # 不唯一时不猜，交给后续 FIRST_PLAYER 配对
            return
        me = cands[0]
        for nm in sides:
            self._name_pids.setdefault(nm, set()).update(sides[nm])
        # 当前局：若本机名字在局内出现，把另一侧定为对手
        cur = [nm for nm in sides if nm in self._named_ids_pure()]
        if me in cur and len(cur) >= 2:
            foe = [nm for nm in cur if nm != me][0]
            self._my_name = me
            self._foe_name = foe

    def _named_ids_pure(self) -> set[str]:
        """本局出现过的玩家名（纯名字，不含 #编号）。"""
        return {
            raw.rsplit("#", 1)[0] for raw in self._named_ids if "#" in raw
        }

    def resolve_first_player_pairs(self) -> dict[str, int]:
        """用 FIRST_PLAYER 把「玩家显示名」与 PlayerID 权威配对。

        ``FIRST_PLAYER=1`` 会同时出现在两处，标记的是**同一个人**：

        - ``TAG_CHANGE Entity=2 tag=FIRST_PLAYER value=1``   （Player 实体侧）
        - ``TAG_CHANGE Entity=EAGhost#5498 tag=FIRST_PLAYER value=1`` （名字侧）

        这是日志里唯一可靠的名字↔PlayerID 关联：

        - ``GameAccountId`` 的 lo/hi 与名字后的 ``#编号`` 无任何算术关系；
        - 名字的**出现顺序**与 PlayerID 无关——实测某局里 EAGhost 先出现，
          却对应 PlayerID 2（我曾据此把敌我认反）。

        必须在所有tag 应用完之后调用（两侧出现顺序不固定）。
        """
        out: dict[str, int] = {}
        if not self._fp_player_pid:
            return out
        for raw in self._fp_names:
            name = raw.rsplit("#", 1)[0]
            self._name_to_pid[name] = self._fp_player_pid
            self._name_pids.setdefault(name, set()).add(self._fp_player_pid)
            self._rebind_pname_slot(name, self._fp_player_pid)
            out[name] = self._fp_player_pid

        # FIRST_PLAYER 只标记先手，本局另一个名字必是后手（3 - 先手）。
        # 不补这一步时，后手名字的 player_slot_of() 返回 0，挂在
        # 「名#号」实体上的 RESOURCES 等玩家级 tag 永远不会同步到它的
        # Player 实体 —— 实测表现为「本机法力恒为 0/0」，模型据此认为
        # 一费都没有，建议完全不可用。
        # 只有恰好剩一个未配对名字时才推导，避免多局日志里误认对手。
        other_pid = 3 - self._fp_player_pid
        others = [nm for nm in self._named_ids_pure() if nm not in out]
        if len(others) == 1:
            name = others[0]
            self._name_to_pid[name] = other_pid
            self._name_pids.setdefault(name, set()).add(other_pid)
            self._rebind_pname_slot(name, other_pid)
            out[name] = other_pid
        return out

    def slot_of_name(self, name: str) -> int:
        """纯玩家名 → 本局 PlayerID（0 = 未识别）。"""
        self.resolve_first_player_pairs()
        pid = self._name_to_pid.get(name)
        if pid:
            return pid
        for i, raw in enumerate(self._pname_order):
            if raw and raw.rsplit("#", 1)[0] == name:
                return i + 1
        return 0

    def name_seen_on_pids(self) -> dict[str, set[int]]:
        """每个玩家名出现过的 PlayerID 集合（跨对局累积）。

        **这是判定本机最可靠的特征**：自己的账号会随先后手在 PlayerID 1↔2
        之间来回切换，而每个对手在所有对局里都固定在同一侧。
        实测日志中 ``玩家甲`` 出现在 {1, 2}，其余 6 个名字各只出现一侧。
        与 ``GameAccountId`` 的 ``lo`` 横跨两侧完全一致，两者互为印证。
        """
        return {k: set(v) for k, v in self._name_pids.items() if v}


    def name_seen_on_pids(self) -> dict[str, set[int]]:
        """每个玩家名出现过的 PlayerID 集合（转发到 parser）。

        本机账号会随先后手在 PlayerID 1↔2 之间切换，而每个对手固定一侧——
        这是判定「哪个名字是我」最可靠的特征，比 account_id 更直观，
        且与 ``GameAccountId`` 的 ``lo`` 横跨两侧互为印证。
        """
        slot = getattr(self, "_slot_resolver", None)
        if slot is None:
            return {}
        parser = getattr(slot, "__self__", None)
        if parser is None:
            return {}
        return parser.name_seen_on_pids()

    def resolve_my_pid(self) -> int:
        """按配置的 player_name 判定本机玩家对应的 PlayerID；找不到返回 0。

        优先用 ``FIRST_PLAYER`` 权威配对结果（见
        :meth:`resolve_first_player_pairs`），它把名字与 PlayerID 直接对应；
        配对缺失时才退回出现顺序。

        之所以不用 ``GameAccountId`` 的 lo 做主判据：那是账号 ID，
        界面上显示不出「我是谁」，且需要用户手工核对数字；
        玩家名既能定位、又能直接显示，是更合适的主判据。
        """
        want = (self._player_name or "").strip()
        if not want:
            return 0
        self.resolve_first_player_pairs()
        pid = self._name_to_pid.get(want)
        if pid:
            return pid
        for i, raw in enumerate(self._pname_order):
            if raw and raw.rsplit("#", 1)[0] == want:
                return i + 1
        return 0

    def _take_pending(self) -> Action | None:
        a, self._pending = self._pending, None
        if a is not None:
            self._note_first_player_tag(a)
        return a

    def _note_first_player_tag(self, a: Action) -> None:
        """在动作提交时检测 ``FIRST_PLAYER=1``，用于把名字与 PlayerID 配对。

        该 tag 会出现在两类实体上：
        - Player 实体（2/3）→ 提供 PlayerID
        - 「名#号」实体 → 提供玩家显示名

        两者标记同一个人，于是构成权威的 name ↔ PlayerID 关联。
        """
        if not a.entity_id:
            return
        for tag, value in a.tags:
            if tag != T.FIRST_PLAYER or value != "1":
                continue
            if a.entity_id < 0:
                # 「名#号」虚拟实体：记下名字，等Player 实体侧出现后配对
                raw = self._name_of_vid(a.entity_id)
                if raw:
                    self._fp_names.add(raw)
            else:
                # Player 实体：记下实体 id，稍后由名字侧查询PlayerID
                self.note_first_player(a.entity_id)
            return

    def _name_of_vid(self, vid: int) -> str:
        """虚拟实体 id → 原始「名#号」字符串。"""
        for name, x in self._named_ids.items():
            if x == vid:
                return name
        return ""

    def _start(self, name: str, time: str) -> None:
        self._pending = Action(action=name, time=time)

    def _bind_entity(self, a: Action, eid: int, fields: dict[str, str]) -> None:
        if eid:
            a.entity_id = eid
        a.entity_fields.update(fields)

    def _apply_tail_tag(self, a: Action, tail: str) -> None:
        """处理 Entity=... 后紧跟的 ``tag=X value=Y``（TAG_CHANGE 单行形态）。"""
        tm = TAG_RE.match(tail.strip())
        if tm:
            a.tags.append((tm.group("tag"), tm.group("value").strip()))

    # ── 主入口 ──
    def feed_line(self, line: str) -> Action | None:
        line = line.rstrip("\n")
        if "DebugPrintPower" not in line:
            return None
        m = LINE_RE.match(line)
        if not m:
            return None
        rest = m.group("rest")
        if not rest.strip():
            return None
        time_str = m.group("time")
        body = rest.strip()

        # ── 顶层动作行 ──
        head = body.split(None, 1)[0] if body.split() else ""
        if head in TOP_LEVEL_ACTIONS:
            finished = self._take_pending()
            if finished is not None:
                #给刚结束的动作补上所属对局号
                finished.game_id = self._game_id
            if head in ("CREATE_GAME", "RESET_GAME"):
                self._game_id += 1
                self.reset_player_order()
            self._start(head, time_str)
            act = self._pending
            assert act is not None
            act.game_id = self._game_id

            tail = body[len(head) :].strip()
            if head == "FULL_ENTITY":
                cm = CREATING_RE.search(tail)
                if cm:
                    self._bind_entity(
                        act,
                        int(cm.group("id")),
                        {"cardId": cm.group("cid")},
                    )
                elif "Updating" in tail:
                    # FULL_ENTITY - Updating [实体] CardID=X：与 SHOW_ENTITY 同义，
                    # 是**我方手牌**卡号的唯一来源，不能漏
                    self._bind_updating_entity(act, tail)
            elif head == "SHOW_ENTITY":
                # 手牌/牌库中的隐藏实体在此暴露真实卡号——补全手牌卡名的关键来源
                self._bind_updating_entity(act, tail)
            elif head in ("GAME_ENTITY", "Player", "PLAYER_ENTITY"):
                # Player EntityID=2 PlayerID=1 GameAccountId=[hi=.. lo=..]
                # —— 各自成为独立动作，后随的缩进 tag 块会正确挂到本实体上。
                dm = ENTITY_DECL_RE.match(body)
                if dm:
                    f: dict[str, str] = {}
                    if dm.group("pid"):
                        f["player"] = dm.group("pid")
                    am = ACCOUNT_ID_RE.search(body)
                    if am:
                        f["accountId"] = am.group("lo")
                    eid = int(dm.group("id"))
                    if dm.group("pid"):
                        # 记录 Player 实体 → PlayerID，供 FIRST_PLAYER 配对使用
                        self.note_player_entity(eid, dm.group("pid"))
                    self._bind_entity(act, eid, f)
            elif head in ("TAG_CHANGE", "CHANGE_ENTITY", "HIDE_ENTITY"):
                eb = ENTITY_BRACKET_RE.search(body)
                if eb:
                    f = parse_entity_body(eb.group("body"))
                    eid = f.get("id", "")
                    self._bind_entity(
                        act,
                        self._resolve_id(f.get("id", "")),
                        {k: v for k, v in f.items() if k != "id"},
                    )
                    self._apply_tail_tag(act, eb.group("tail"))
                else:
                    se = ENTITY_SHORT_RE.search(body)
                    if se:
                        raw_id = se.group("id")
                        self._bind_entity(act, self._resolve_id(raw_id), {"_raw_id": raw_id})
                        self._apply_tail_tag(act, se.group("tail"))
            elif head == "META_DATA":
                mm = META_RE.search(tail)
                if mm:
                    act.meta["meta"] = mm.group("meta")
                    act.meta["data"] = mm.group("data")
                    self._last_meta = mm.group("meta")
            elif head == "BLOCK_START":
                bm = re.search(r"BlockType=(\w+)", tail)
                if bm:
                    act.meta["block_type"] = bm.group(1)

            return finished  # 上一条动作在此完成

        # ── 子标签行（归属当前动作） ──
        a = self._pending
        if a is None:
            return None

        # 实体声明行已全部提升为顶层动作（见 TOP_LEVEL_ACTIONS），此处无需再处理

        # META_DATA（可能是子行）
        mm = META_RE.search(body)
        if mm and body.startswith("META_DATA"):
            a.meta["meta"] = mm.group("meta")
            a.meta["data"] = mm.group("data")
            self._last_meta = mm.group("meta")
            return None

        # Info[n] = [...] / Entities[n]=[...]
        im = INFO_RE.match(body) or ENTITIES_RE.match(body)
        if im:
            fields = parse_entity_body(im.group("body"))
            key = self._last_meta or "INFO"
            a.meta.setdefault(key, []).append(fields)
            return None

        # tag=X value=Y
        tm = TAG_RE.match(body)
        if tm:
            a.tags.append((tm.group("tag"), tm.group("value").strip()))
            return None

        return None

    def flush(self) -> Action | None:
        """输入结束时吐出最后一个动作。"""
        a = self._take_pending()
        if a is not None:
            a.game_id = self._game_id
        return a


# ─────────────────────────────────────────────────────────────
# 状态还原
# ─────────────────────────────────────────────────────────────


@dataclass
class PlayerView:
    """单个玩家的局面视图。"""

    pid: int
    hero: Entity | None = None
    hero_power: Entity | None = None
    weapon: Entity | None = None
    hand: list[Entity] = field(default_factory=list)
    board: list[Entity] = field(default_factory=list)
    #: 场上地标：与随从分列（不能攻击、有耐久、每回合限用一次）
    locations: list[Entity] = field(default_factory=list)
    secrets: list[Entity] = field(default_factory=list)
    deck_count: int = 0
    deck_entities: int = 0
    #: 炉石日志不记录未揭示的牌库牌，DECK 区实体数为 0 时牌库剩余数不可信
    deck_count_known: bool = False
    graveyard: list[Entity] = field(default_factory=list)
    mana: int = 0
    mana_used: int = 0
    mana_max: int = 0
    fatigue: int = 0
    overload_locked: int = 0
    max_hand_size: int = 10

    @property
    def hero_hp(self) -> int:
        if not self.hero:
            return 0
        return self.hero.stats[1]

    @property
    def hero_atk(self) -> int:
        if not self.hero:
            return 0
        return self.hero.stats[0]

    @property
    def armor(self) -> int:
        """护甲来自英雄的 armor 属性（非 HEALTAG），单独存。"""
        if not self.hero:
            return 0
        return self.hero.get_int("ARMOR", 0)

    @property
    def effective_hp(self) -> int:
        return self.hero_hp + self.armor

    @property
    def num_cards_in_hand(self) -> int:
        return len(self.hand)

    def to_dict(self) -> dict:
        d: dict = {
            "mana": f"{self.mana}/{self.mana_max}",
            "hand_count": len(self.hand),
            "deck_count": self.deck_count,
            "graveyard_count": len(self.graveyard),
            "fatigue": self.fatigue,
        }
        if self.hero:
            atk, hp = self.hero.stats
            d["hero"] = {
                "name": self.hero.display_name,
                "class": self.hero.get(T.CLASS),
                "atk": atk,
                "hp": hp,
                "armor": self.armor,
            }
        if self.hero_power:
            atk, hp = self.hero_power.stats
            d["hero_power"] = {
                "name": self.hero_power.display_name,
                "cost": self.hero_power.cost,
                "atk": atk,
                "text": self.hero_power.get_int("CORPUSDAMAGE", 0) or None,
            }
        if self.weapon:
            atk, hp = self.weapon.stats
            d["weapon"] = {"name": self.weapon.display_name, "atk": atk, "durability": hp}
        if self.board:
            d["board"] = [e.to_brief() for e in self.board]
        if self.locations:
            d["locations"] = [
                {
                    "name": e.display_name,
                    "id": e.card_id,
                    "durability": e.stats[1],
                    "pos": e.zone_pos,
                    "used_this_turn": e.location_used,
                }
                for e in self.locations
            ]
        if self.hand:
            d["hand"] = [e.to_brief() for e in self.hand]
        if self.secrets:
            d["secrets"] = [{"name": e.display_name, "id": e.card_id} for e in self.secrets]
        if not self.deck_count_known:
            d["deck_count_note"] = "未知：客户端日志不记录未揭示的牌库牌，0 不代表牌库已空"
        if self.overload_locked:
            d["overload_locked"] = self.overload_locked
        return d


#: 会从「名#号」玩家实体同步到玩家实体（2/3）的 tag
PLAYER_SYNC_TAGS = frozenset(
    {
        T.RESOURCES,
        T.RESOURCES_USED,
        T.TEMP_RESOURCES,
        T.OVERLOAD,
        T.OVERLOAD_LOCKED,
        T.OVERLOAD_OWED,
        "NUM_TIMES_HERO_POWER_PLAYED_THIS_GAME",
        "MAXRESOURCES",
    }
)


class GameStateReconstructor:
    """消费 Action 序列，维护实体表并投影出可读局面。"""

    def __init__(self) -> None:
        self.entities: dict[int, Entity] = {}
        self.player_names: dict[int, str] = {}
        self.step: str = ""
        self.next_step: str = ""
        self.turn: int = 0
        self.state: str = ""
        self.current_player: int = 0
        self.first_player: int = 0
        #: 当前行动方的显示名，来自 ``CURRENT_PLAYER value=1`` 的「名#号」实体。
        #: 日志明确写了「谁在行动」，比「名#号 → PlayerID」槽位映射更直接：
        #: 一份 Power.log 含多场对局，槽位映射会残留旧局的配对（实测因此
        #: 把对手回合误判成我方回合，每个回合都弹建议）。
        self.current_player_name: str = ""
        #: 最近一次「玩家级 TURN」标签所在的名字实体。炉石每个回合开始都会
        #: 写 ``Entity=玩家甲#12345 tag=TURN value=4``（该玩家自己的回合数），
        #: 直接带名字、每回合必写，是判断「现在轮到谁」最可靠的信号：
        #: 既不依赖 CURRENT_PLAYER 布尔标记，也不依赖槽位映射。
        self.turn_owner_name: str = ""
        self.game_mode: str = ""
        self.game_started: bool = False
        self.log_time: str = ""
        self.events: list[str] = []
        self._max_events = 300
        #: 战况事件流（带时间戳），GUI 实时日志面板消费
        self.battle_log: list[str] = []
        self._max_log = 200
        #: 「名#号」虚拟实体 id → 玩家编号，由 _slot_resolver 回填
        self._pname_slot: dict[int, int] = {}
        #: 实体首次出现的全局序号，用于站位相同时的稳定排序
        self._seq_counter = 1
        #: PlayerID → 战网账号 lo 值（本机玩家判定用）
        self.pid_account: dict[int, str] = {}
        #: 配置指定的本机账号 lo 值
        self.my_account: str = ""
        #: 日志已静默多久（秒），由 LogWatcher 每轮写入；>0 表示不在对局中
        self.idle_seconds: float = 0.0
        #: 配置指定的本机玩家名（主判据，能直接显示「我是谁」）
        self.my_player_name: str = ""
        #: 历史对局的实体表，仅供调试（局面投影只用当前 entities）
        self._archival: list[dict[int, "Entity"]] = []
        #: 已应用到的对局号；变化即表示新对局开始
        self._cur_game = 0

        #: 关注的战况事件类型（GUI 实时日志用）
        self._COMBAT_TAGS = {
            T.DAMAGE: "造成伤害",
            T.ATK: "攻击力变化",
            T.HEALTH: "生命变化",
            T.ZONE: "区域变更",
            "TO_BE_DESTROYED": "即将消灭",
            "EXHAUSTED": "无法行动",
            "FROZEN": "被冻结",
            "SILENCED": "被沉默",
            "DIVINE_SHIELD": "获得圣盾",
            "TAUNT": "获得嘲讽",
        }

    # ── 实体管理 ──
    def _entity(self, eid: int) -> Entity:
        e = self.entities.get(eid)
        if e is None:
            e = Entity(id=eid, seq=self._seq_counter)
            self._seq_counter += 1
            self.entities[eid] = e
        return e

    def reset_game(self) -> None:
        """新对局重置。

        三个实测坑决定了这个实现：
        1. 进入战斗时炉石发两次 CREATE_GAME，而 FULL_ENTITY 声明只在第一次，
           所以不能无条件清空实体表（英雄 HEALTH/CLASS 会丢）。
        2. **实体 id 会跨对局复用** —— 同一份日志里 id=4 在上一局是心灵扫荡者，
           下一局又变成进击的募援官。残留旧实体会让新对局场面被污染
           （表现为场面出现上一局随从、攻血为 0）。
        3. 但玩家实体（2/3）与英雄实体在整份日志里只声明一次，跨对局复用。

        做法：新对局整体换代（entities 换新表），旧表归档到 _archival 仅供调试；
        玩家/英雄实体从归档继承，保证法力与血量可读。
        """
        old = self.entities
        # 继承跨对局复用的玩家与英雄实体（它们的声明只出现一次）
        keep = {
            eid: ent
            for eid, ent in old.items()
            if ent.cardtype in ("PLAYER", "HERO", "HERO_POWER")
        }
        if old:
            self._archival.append(old)
        self.entities = dict(keep)
        self.player_names.clear()
        self.step = ""
        self.next_step = ""
        self.turn = 0
        self.state = ""
        self.current_player = 0
        self.current_player_name = ""
        self.turn_owner_name = ""
        self.first_player = 0
        self.game_started = True
        self.events.clear()
        self.battle_log.clear()
        self._pname_slot.clear()


    def _sync_player_tags(self, eid: int, tag: str, value: str) -> None:
        """把玩家级tag 从「名#号」虚拟实体同步到对应的玩家实体（2/3）。

        炉石把 RESOURCES（法力）等玩家级 tag 挂在 ``玩家甲#12345`` 这类
        「名#号」实体上，而玩家实体（2/3）通过 GameAccountId 关联，
        两者编号不同且无算术关系（实测 >>11、除以常数等均不成立）。
        这里按出现顺序（parser.player_slot_of）建立映射，把关键 tag
        复制到玩家实体上，player_view 才能读到法力。
        """
        if tag not in PLAYER_SYNC_TAGS:
            return
        pid = self._pname_slot.get(eid, 0)
        if not pid:
            return
        target_id = self.player_entity_id(pid)
        if not target_id or target_id == eid:
            return
        target = self.entities.get(target_id)
        if target is not None:
            target.tags[tag] = value

    def rebind_player_tags(self, name: str, pid: int) -> None:
        """玩家名归属被``FIRST_PLAYER`` 纠正后，迁移已同步的玩家级 tag。

        名字实体的 FIRST_PLAYER 与 Player 实体的 FIRST_PLAYER 谁先出现不固定。
        若名字先到，它会先按「出现顺序」被兜底绑定，并可能把``RESOURCES``
        等tag 同步到**错误**的 Player 实体。权威信息到齐时必须：
        1. 把该名字虚拟实体上的玩家级 tag 复制到正确的 Player 实体；
        2. 从错误目标上移除这些 tag（若它并非自己写的）。

        实测这正是「我方/敌方场面反了 + 法力恒为 0」的根因。
        """
        target_id = self.player_entity_id(pid)
        if not target_id:
            return
        dst = self.entities.get(target_id)
        if dst is None:
            return

        # 找到该名字的虚拟实体
        resolver = getattr(self, "_pname_resolver", None)
        slot = getattr(self, "_slot_resolver", None)
        vid: int | None = None
        if resolver is not None and slot is not None:
            for eid in list(self._pname_slot):
                if resolver(eid) == name:
                    vid = eid
                    break
        if vid is None:
            return
        src = self.entities.get(vid)
        if src is None:
            return

        # 0) 纠正显示名归属（此前可能按出现顺序绑错）
        for wrong_pid, wrong_name in list(self.player_names.items()):
            if wrong_name == name and wrong_pid != pid:
                del self.player_names[wrong_pid]
        self.player_names[pid] = name

        # 1) 迁移到正确目标
        for tag in PLAYER_SYNC_TAGS:
            v = src.tags.get(tag)
            if v is not None:
                dst.tags[tag] = v

        # 2) 清掉错误目标上的残留
        for other_pid, other_eid in self._pname_slot.items():
            if other_eid == vid or other_pid == pid:
                continue
            wrong_id = self.player_entity_id(other_pid)
            if not wrong_id or wrong_id == target_id:
                continue
            w = self.entities.get(wrong_id)
            if w is None:
                continue
            for tag in PLAYER_SYNC_TAGS:
                # 只清掉与该名字实体取值相同的（说明是误同步来的）
                if src.tags.get(tag) is not None and w.tags.get(tag) == src.tags.get(tag):
                    w.tags.pop(tag, None)

    def resync_player_tags(self) -> int:
        """权威配对完成后，把「名#号」实体上的玩家级 tag 重放到正确 Player 实体。

        FIRST_PLAYER 两侧出现时机可差上万行，期间名字按出现顺序兜底绑定，
        法力（RESOURCES）等 tag 会被同步到错误的 Player 实体；而始终没配上
        的那一侧则压根没同步过。配对完成后统一重放一次，保证两边都拿到
        属于自己的值。返回被更新的 Player 实体数。
        """
        slot = getattr(self, "_slot_resolver", None)
        parser = getattr(slot, "__self__", None)
        if parser is None:
            return 0
        # 用权威结果覆盖早期兜底映射
        for vid in parser.named_vids():
            pid = parser.player_slot_of(vid)
            if pid:
                self._pname_slot[vid] = pid

        values: dict[int, dict[str, str]] = {}
        for vid, pid in self._pname_slot.items():
            src = self.entities.get(vid)
            if src is None:
                continue
            d = values.setdefault(pid, {})
            for tag in PLAYER_SYNC_TAGS:
                v = src.tags.get(tag)
                if v is not None:
                    d[tag] = v

        applied = 0
        for e in self.entities.values():
            if e.cardtype != "PLAYER":
                continue
            pid = e.get_int("PLAYER_ID", 0) or e.get_int(T.CONTROLLER, 0)
            vals = values.get(pid)
            if not vals:
                continue
            for tag, v in vals.items():
                e.tags[tag] = v
            applied += 1
        return applied

    def apply_player_names(self) -> None:
        """在全部 tag 应用完毕后，刷新 Player 实体上的显示名。

        名字↔PlayerID 的绑定由 ``FIRST_PLAYER`` 决定（见
        :meth:`resolve_first_player_pairs`），而两侧出现顺序不固定，
        因此不能在``apply`` 里随手写名字——必须等配对完成后再统一刷新。
        """
        resolver = getattr(self, "_pname_resolver", None)
        slot_of = getattr(self, "_slot_resolver", None)
        if resolver is None or slot_of is None:
            return
        fresh: dict[int, str] = {}
        for eid in self._pname_slot:
            name = resolver(eid)
            if not name:
                continue
            pid = self._pname_slot[eid]
            if pid in (1, 2):
                fresh[pid] = name
        # ★ 兜底：若配置了 player_name 但本局日志里尚未出现该名字实体
        # （炉石要等玩家实际操作才暴露自己的名字），用 resolve_my_pid()
        # 判定出的 PlayerID 直接标注——否则界面会显示「(未识别)」，
        # 让人误以为身份识别失败。
        want = (self.my_player_name or "").strip()
        if want:
            mypid = self.resolve_my_pid()
            if mypid and mypid not in fresh:
                fresh[mypid] = want
                other = 2 if mypid == 1 else 1
                # 对手名字若已知也补上，避免只显示一半
                known = [v for k, v in fresh.items() if k != mypid and v]
                if known and other not in fresh:
                    fresh[other] = known[0]

        if not fresh:
            return
        # 只覆盖有权威绑定的项，保留其余
        self.player_names.update(fresh)
        for pid, name in fresh.items():
            tgt = self.entities.get(self.player_entity_id(pid))
            if tgt is not None:
                tgt._pname = name

    def _record(self, text: str) -> None:
        self.events.append(text)
        if len(self.events) > self._max_events:
            del self.events[: len(self.events) - self._max_events]

    def _describe_action(self, a: "Action") -> str | None:
        """把一条动作转成人类可读的战况描述（用于 GUI 实时日志）。"""
        if a.action in ("CREATE_GAME", "RESET_GAME"):
            return "── 对局开始 ──"
        if a.action == "BLOCK_START":
            bt = a.meta.get("block_type", "")
            return {"PLAY": "出牌", "POWER": "效果结算", "DEATHS": "死亡结算",
                    "TRIGGER": "触发效果", "GAME": "游戏事件",
                    "ATTACK": "攻击", "DECK": "抽牌"}.get(bt, None)
        if a.action == "SHOW_ENTITY":
            cid = a.entity_fields.get("cardId", "")
            if cid:
                return f"揭示卡牌 {cid}"
            return None
        if a.action == "HIDE_ENTITY":
            return "隐藏实体"
        if a.action == "TAG_CHANGE":
            ent = self.entities.get(a.entity_id)
            name = ent.display_name if ent else f"实体{a.entity_id}"
            for tag, value in a.tags:
                label = self._COMBAT_TAGS.get(tag)
                if not label:
                    continue
                if tag == T.DAMAGE:
                    return f"{name} 受到 {value} 点伤害"
                if tag in (T.ATK, T.HEALTH):
                    return f"{name} {label} → {value}"
                if tag == T.ZONE:
                    return f"{name} → {value}"
                return f"{name} {label}"
            return None
        return None

    # ── 应用动作 ──
    def apply(self, a: Action) -> None:
        # 对局边界：CREATE_GAME 的块被拆散成多条动作（GameEntity + 各 Player），
        # 因此改用 game_id 变化来识别新对局，而不是单看 action 名。
        if a.action in ("CREATE_GAME", "RESET_GAME"):
            self.game_mode = a.entity_fields.get("gameMode", "") or self.game_mode
            self.reset_game()
            self._cur_game = a.game_id
            return
        if self._cur_game and a.game_id and a.game_id != self._cur_game:
            self.reset_game()
        if a.game_id:
            self._cur_game = a.game_id

        # 战况事件流（GUI 实时日志）
        line = self._describe_action(a)
        if line:
            self.battle_log.append(f"{a.time}  {line}")
            if len(self.battle_log) > self._max_log:
                del self.battle_log[: len(self.battle_log) - self._max_log]

        self.log_time = a.time or self.log_time
        eid = a.entity_id
        f = a.entity_fields

        # 「名#号」虚拟实体 → 玩家编号 的映射（由 parser 维护）
        if eid and eid < 0:
            slot = getattr(self, "_slot_resolver", None)
            if slot is not None:
                pid = slot(eid)
                if pid:
                    self._pname_slot[eid] = pid
                    # 记录显示名，便于界面与自检输出「我是谁」
                    # 显示名**不**在此处写入：此刻 FIRST_PLAYER 权威信息可能还没到
                    # （名字侧与 Player 侧出现顺序不固定），此时写入必然是错的。
                    # 统一交给 rebind_player_tags / resolve_first_player_pairs 处理。

        # 实体描述写回实体表
        if eid:
            ent = self._entity(eid)
            if f.get("accountId") and not ent.declared.get("accountId"):
                ent.declared["accountId"] = f["accountId"]
            if f.get("cardId"):
                ent.card_id = f["cardId"]
            # 隐藏实体在揭示前的占位名是 UNKNOWN ENTITY [...]，不能覆盖
            # 已经解析出的中文卡名，否则提示词里会出现一串乱码实体名。
            if f.get("entityName") and not f["entityName"].startswith("UNKNOWN ENTITY"):
                ent.name = f["entityName"]

            # Entity=[...] 里的 zone/zonePos 是**变更前**的快照，不是当前状态：
            #   TAG_CHANGE Entity=[... zone=PLAY ...] tag=ZONE value=GRAVEYARD
            #   TAG_CHANGE Entity=[... zone=PLAY ...] tag=DAMAGE value=0   ← 结算后
            # 第二条会把刚进墓地的实体又写回 PLAY，实测造成「已死随从复活」、
            # 场面随从数超过 7、站位重复。区域与站位的**唯一权威来源**是
            # 下方 for 循环里的 tag=ZONE / tag=ZONE_POSITION。
            # 因此仅在实体尚无任何区域记录时用它初始化一次；已有区域的实体
            # 一律忽略快照（避免复活），只认下方 for 循环的权威 tag。
            allow_snapshot = not ent.zone
            if allow_snapshot:
                if f.get("zone"):
                    ent.tags[T.ZONE] = f["zone"]
                if f.get("zonePos"):
                    ent.tags[T.ZONE_POSITION] = f["zonePos"]
                if f.get("player"):
                    ent.tags[T.CONTROLLER] = f["player"]
            if f.get("player"):
                pid = f["player"]
                if pid.isdigit():
                    pid_i = int(pid)
                    if f.get("accountId"):
                        self.pid_account[pid_i] = f["accountId"]
                    elif ent.declared.get("PLAYER_ID") and ent.declared.get("accountId"):
                        self.pid_account[pid_i] = ent.declared["accountId"]

        for tag, value in a.tags:
            if eid:
                ent = self._entity(eid)
                old_zone = ent.zone
                # 声明期（FULL_ENTITY/GAME_ENTITY/SHOW_ENTITY 的初始 tag 块）
                # 存入 declared，变更期存入 tags，两者互不覆盖。
                # 实测：玩家实体的 CARDTYPE/PLAYER_ID 只在声明期出现一次，
                # 若混存，实体收到后续 PLAYSTATE 等 tag 后会「看起来失去类型」。
                if a.action in ("FULL_ENTITY", "CREATE_GAME", "RESET_GAME", "GAME_ENTITY"):
                    ent.declared.setdefault(tag, value)
                ent.tags[tag] = value
                # 「名#号」玩家实体的关键 tag 同步到对应玩家实体，
                # 这样 player_view 读玩家实体即可拿到法力等信息。
                self._sync_player_tags(eid, tag, value)
                if tag == T.ZONE and value != old_zone:
                    self._record(f"{ent.display_name} → {value}")
                if tag == T.ZONE_POSITION:
                    self._record(f"{ent.display_name} 位置 {value}")

            # 全局标签
            if tag == T.STEP:
                self.step = value
            elif tag == T.NEXT_STEP:
                self.next_step = value
            elif tag == T.TURN:
                # TURN 也会挂在「名#号」实体上，但那是**该玩家自己的回合数**
                # （恒从 1 重新数），不是全局回合号。照单全收会让 self.turn
                # 来回跳，回合去重失效。只认 GameEntity（eid=1）上的全局 TURN。
                #
                # 但这条玩家级 TURN 恰恰是「现在轮到谁」最可靠的信号：
                #   TURN=8  → 玩家甲#12345 tag=TURN value=4   （我方第 4 回合）
                #   TURN=9  → 憔悴的豺狼人#510300 tag=TURN value=5（对手第 5 回合）
                # 每回合开始必写、直接带名字，不受 CURRENT_PLAYER 布尔标记
                # 与「名#号 → PlayerID」槽位残留影响。实测靠编号判定会把
                # 对手回合误判成我方（浮窗连续 13/14 回合各弹一次建议）。
                if eid and eid < 0:
                    resolver = getattr(self, "_pname_resolver", None)
                    if resolver is not None:
                        nm = (resolver(eid) or "").rsplit("#", 1)[0].strip()
                        if nm:
                            self.turn_owner_name = nm
                if eid == 1 or (
                    eid and eid in self.entities
                    and self.entities[eid].cardtype == "GAME"
                ):
                    self.turn = int(value) if value.isdigit() else 0
            elif tag == T.STATE:
                self.state = value
            elif tag == T.CURRENT_PLAYER:
                # CURRENT_PLAYER 是挂在实体上的 0/1 **布尔标记**，不是玩家编号：
                #   Entity=奔波儿灞#53170 tag=CURRENT_PLAYER value=0
                #   Entity=玩家甲#12345 tag=CURRENT_PLAYER value=1
                # 旧版直接 int(value) 当 PlayerID，结果恒为 1 —— 本机若是
                # PlayerID 2，就永远识别不到自己的回合，一次建议都不给。
                pid = 0
                raw_name = ""
                if eid and eid < 0:
                    slot = getattr(self, "_slot_resolver", None)
                    if slot is not None:
                        pid = slot(eid) or 0
                    name_resolver = getattr(self, "_pname_resolver", None)
                    if name_resolver is not None:
                        raw_name = name_resolver(eid) or ""
                elif eid:
                    ent = self.entities.get(eid)
                    if ent is not None:
                        pid = ent.get_int("PLAYER_ID", 0) or ent.get_int(T.CONTROLLER, 0)
                # 「谁在行动」以日志里 value=1 那一侧的名字为准。
                # 后面判断是否我方时优先用这个名字，绕开槽位映射的残留误差。
                if value == "1" and raw_name:
                    self.current_player_name = raw_name.rsplit("#", 1)[0]
                if value == "0":
                    # 「这一方不是当前行动方」。日志总是成对写 0/1：
                    #   对手回合开始 → 我方名=0、对手名=1
                    # 若只忽略 0、死等后面的 1，一旦 1 因槽位未就绪解析失败，
                    # current_player 就会停在上一回合的旧值，把**对手回合**
                    # 误判成我方回合（表现为每个回合都弹建议）。
                    # 因此 0 本身也携带信息：非行动方是 pid，则行动方是 3-pid。
                    if pid in (1, 2) and self.current_player == pid:
                        self.current_player = 3 - pid
                elif pid in (1, 2):
                    self.current_player = pid
                elif value.isdigit() and int(value) in (1, 2) and eid and eid > 0:
                    # 兼容旧版日志：Player 实体直接把玩家编号写在 value 里。
                    # 注意只对**正 id**（真实 Player 实体）这么做；
                    # 「名#号」虚拟实体（负 id）上的 1 是布尔标记，绝不能当编号。
                    self.current_player = int(value)
            elif tag == T.FIRST_PLAYER:
                self.first_player = int(value) if value.isdigit() else 0

    # ── 局面投影 ──
    def is_my_action_phase(self, my_pid: int) -> bool:
        """是否「轮到我方且已可操作」——唯一允许生成回合建议的时机。

        实测事故：`gui` 的「启动即分析」与 watcher 的「首轮补触发」各自手写
        条件，结果在**对手回合**也请求了模型（浮窗显示「第 2 回合建议」，
        模型自己回复「非我方行动回合」）。所有建议生成点统一走这里。
        """
        if not my_pid or self.step != "MAIN_ACTION":
            return False
        if self.state in ("COMPLETE", "GAME_OVER", "WON", "LOST", "TIED"):
            return False
        if float(getattr(self, "idle_seconds", 0.0) or 0.0) > 90:
            return False

        # 优先用「玩家级 TURN 那一侧的名字」判断（每回合必写、最可靠），
        # 其次是 CURRENT_PLAYER=1 的名字，最后才退回 PlayerID 编号。
        owner = (self.turn_owner_name or self.current_player_name or "").strip()
        mine = {
            n.strip().rsplit("#", 1)[0]
            for n in (self.player_names.get(my_pid, ""), self.my_player_name)
            if n and not n.startswith("UNKNOWN")
        }
        if owner:
            return owner in mine
        # 名字不可用时退回编号判据
        return bool(self.current_player and self.current_player == my_pid)

    def name_seen_on_pids(self) -> dict[str, set[int]]:
        """每个玩家名出现过的 PlayerID 集合（转发到 parser）。

        本机账号会随先后手在 PlayerID 1↔2 之间切换，而每个对手固定一侧——
        这是判定「哪个名字是我」最可靠的特征，比 account_id 更直观，
        且与 ``GameAccountId`` 的 ``lo`` 横跨两侧互为印证。
        """
        slot = getattr(self, "_slot_resolver", None)
        if slot is None:
            return {}
        parser = getattr(slot, "__self__", None)
        if parser is None:
            return {}
        return parser.name_seen_on_pids()

    def resolve_my_pid(self) -> int:
        """判定本机玩家对应的 PlayerID；无法判定返回 0。

        两个独立依据，任一命中即可，互相印证：

        1. ``player_name``（主）—— 玩家显示名。用 ``FIRST_PLAYER`` 权威配对
           把名字与 PlayerID 对应（该 tag 同时出现在 Player 实体与「名#号」
           实体上，标记同一个人）。玩家名唯一且能显示在界面上。
        2. ``account_id``（辅）—— ``GameAccountId`` 的 ``lo``，战网账号 ID，
           在整份日志里恒定，可作为交叉校验。

        **两者都不依赖 ``my_player_id``**：本机账号会随先后手在 1/2 间切换
        （实测同一名字出现过 PlayerID 1 与 2），固定值必然在某些对局指错人。
        """
        # ① player_name —— FIRST_PLAYER 配对优先
        want = (self.my_player_name or "").strip()
        if want:
            parser = self._parser_ref()
            if parser is not None:
                pid = parser.slot_of_name(want)
                if pid:
                    return pid
        # ② account_id
        if self.my_account:
            for pid, acc in self.pid_account.items():
                if acc == self.my_account:
                    return pid
        return 0

    def _parser_ref(self):
        """取回解析器引用（用于查询名字绑定）。"""
        slot = getattr(self, "_slot_resolver", None)
        return getattr(slot, "__self__", None)

    def player_entity_id(self, pid: int) -> int:
        """玩家编号(1/2) → 其 Player 实体的 entity id。

        日志中玩家实体 id 通常是 2/3，与 PLAYER_ID 不同，需通过 PLAYER_ID tag 建立映射。
        若实体表被截断（长对局 + 旧版10MB 上限）导致 Player 实体缺失，返回 0。
        """
        for e in self.entities.values():
            if e.cardtype == "PLAYER" and e.get_int("PLAYER_ID", 0) == pid:
                return e.id
        return 0

    def hero_of(self, pid: int) -> Entity | None:
        """定位英雄本体。

        优先用玩家实体的 HERO_ENTITY 标签；若 Player 实体缺失（日志截断），
        退化用「CONTROLLER 匹配 + 卡号以 HERO_ 开头 + 在PLAY 区」识别。
        """
        pe = self.entities.get(self.player_entity_id(pid))
        if pe:
            hid = pe.get_int("HERO_ENTITY", 0)
            if hid:
                h = self.entities.get(hid)
                if h:
                    return h
        for e in self.entities.values():
            if (
                e.cardtype == CARDTYPE_HERO
                or e.card_id.startswith("HERO_")
            ):
                if e.controller == pid and (e.zone == "PLAY" or e.zone == ""):
                    return e
        return None

    def hero_power_of(self, pid: int) -> Entity | None:
        """定位英雄技能。

        必须取PLAY 区的技能实体：牌库/暂存区里也存在同名实体
        （如尚未召唤/已被替换的技能卡），若不限定区域会取到错误对象——
        实测曾让猎人玩家显示成「图腾召唤」（萨满技能，来自对手牌库区的实体）。
        """
        best: Entity | None = None
        for e in self.entities.values():
            if e.cardtype != CARDTYPE_HERO_POWER:
                continue
            ctrl = e.controller
            if ctrl and ctrl != pid:
                continue
            if ctrl == 0:
                continue
            if e.zone == "PLAY":
                # PLAY 区的技能即当前生效技能，直接采用
                return e
            best = best or e
        return best

    def player_view(self, pid: int) -> PlayerView:
        v = PlayerView(pid=pid)
        for e in self.entities.values():
            if e.cardtype == "PLAYER":
                continue
            # 按控制者过滤；CONTROLLER 缺失时（SETASIDE 阶段的英雄技能等）
            # 允许通过 hero_of / hero_power 归属兜底
            ctrl = e.controller
            if ctrl and ctrl != pid:
                continue
            if ctrl == 0 and e.cardtype not in (CARDTYPE_HERO, CARDTYPE_HERO_POWER):
                continue
            ct, z = e.cardtype, e.zone
            if ct == CARDTYPE_HERO or e.card_id.startswith("HERO_"):
                if v.hero is None:
                    v.hero = e
                continue
            if ct == CARDTYPE_HERO_POWER:
                # 只收 PLAY 区（当前生效）的技能；权威来源见下方 hero_power_of
                if z == "PLAY" and v.hero_power is None:
                    v.hero_power = e
                continue
            if z == "PLAY":
                if e.cardtype == "WEAPON":
                    if v.weapon is None:
                        v.weapon = e
                elif e.is_location:
                    # 地标不是随从：单列，避免混进场面数、误导交换与斩杀计算
                    v.locations.append(e)
                elif e.is_valid_minion:
                    # 只收有效随从：排除换牌界面的 UNKNOWN ENTITY 占位
                    v.board.append(e)
            elif z == "HAND":
                # 自己的手牌在抽牌时先以「无卡号占位实体」入区
                # （entityName=UNKNOWN ENTITY [cardType=INVALID] cardId= 为空），
                # 卡号要等后续 SHOW_ENTITY 才补上。若按「卡号非空」过滤，
                # 我方手牌会整体消失 —— 模型据此回复「手牌为空，只能空过」，
                # 而实际上手里是有牌的。
                # 对手手牌不同：客户端不可见，只有被揭示（打出/召唤等）才写入日志。
                if e.controller == pid or e.card_id:
                    v.hand.append(e)
            elif z == "SECRET":
                v.secrets.append(e)
            elif z == "DECK":
                v.deck_entities += 1
            elif z == "GRAVEYARD":
                v.graveyard.append(e)

        # 英雄权威来源为 HERO_ENTITY 指向的实体
        hero = self.hero_of(pid)
        if hero is not None:
            v.hero = hero
        # 英雄技能同样需要权威来源（只认 PLAY 区，避免取到牌库/暂存区的同名实体）
        hp = self.hero_power_of(pid)
        if hp is not None:
            v.hero_power = hp

        v.board.sort(key=lambda e: (e.zone_pos, e.seq))
        v.locations.sort(key=lambda e: (e.zone_pos, e.seq))
        v.hand.sort(key=lambda e: (e.zone_pos, e.seq))
        v.graveyard.sort(key=lambda e: e.zone_pos, reverse=True)

        pe = self.entities.get(self.player_entity_id(pid))
        v.deck_count = v.deck_entities
        # 日志只在卡牌被揭示/移动时才写 DECK 区实体，未揭示的牌库牌不出现。
        # 因此 0 只能表示「不知道」，不能当成「牌库已空」交给模型。
        v.deck_count_known = v.deck_entities > 0
        v.fatigue = pe.get_int("FATIGUE", 0) if pe else 0
        v.mana = pe.get_int(T.RESOURCES, 0) if pe else 0
        v.mana_used = pe.get_int(T.RESOURCES_USED, 0) if pe else 0
        v.mana_max = v.mana + v.mana_used if pe else 0
        v.overload_locked = pe.get_int(T.OVERLOAD_LOCKED, 0) if pe else 0
        v.max_hand_size = pe.get_int("MAXHANDSIZE", 10) if pe else 10
        return v

    def board_state(self, my_pid: int = 1) -> dict:
        """导出结构化局面，喂给 LLM。

        ``my_pid`` 必须传本机 PlayerID —— 旧版硬编码 1，GUI 判定出本机是
        PlayerID 2 时 `--json` 输出的敌我跟界面正好相反。
        """
        me = self.player_view(my_pid)
        foe = self.player_view(3 - my_pid)
        return {
            "game": {
                "mode": self.game_mode or "未知模式",
                "turn": self.turn,
                "step": self.step,
                "state": self.state,
                "current_player": self.current_player,
                "my_player_id": my_pid,
            },
            "me": me.to_dict(),
            "opponent": foe.to_dict(),
            "recent_events": self.events[-30:],
            "recent_battle_log": self.battle_log[-30:],
        }


def scan_player_identity(path: str | Path) -> dict:
    """扫描整份日志，产出玩家身份表。

    返回::

        {
          "names":  {名字: {出现过的 PlayerID}},
          "lo_map": {lo: {出现过的 PlayerID}},
          "name_of_lo": {lo: 名字},   # 由 FIRST_PLAYER 权威配对得出
        }

    **唯一可靠的判据是``lo``（战网账号 ID）横跨 PlayerID 1/2** ——
    本机账号会随先后手换边，而每个对手在所有对局里固定一侧。
    实测某份日志中 ``lo=100000001`` 出现在 PlayerID {1, 2}，其余 8 个各只一侧。

    注意**不能**用「名字横跨两侧」来判定：每一局两名玩家各占一侧，
    因此两个名字都会横跨，该判据无法区分敌我（我曾据此误判过）。
    名字↔``lo`` 的对应则由 ``FIRST_PLAYER`` 权威配对：
    ``FIRST_PLAYER=1`` 同时出现在 Player 实体与「名#号」实体上，标记同一个人。
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore")

    bounds = [m.start() for m in re.finditer(
        r"PowerTaskList\.DebugPrintPower\(\)\s+-\s+CREATE_GAME", text)]
    bounds.append(len(text))

    names: dict[str, set[int]] = collections.defaultdict(set)
    lo_map: dict[str, set[int]] = collections.defaultdict(set)
    #: lo → {名字: 作为先手出现的对局数}
    name_votes: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    #: 名字 → 参与对局数（本机与所有对手都会打，但本机对局数通常最多）
    game_count: collections.Counter = collections.Counter()

    for i in range(len(bounds) - 1):
        seg = text[bounds[i]:bounds[i + 1]]
        # 本局 Player 实体 id → (PlayerID, lo)
        local: dict[str, tuple[int, str]] = {}
        for m in re.finditer(
            r"Player EntityID=(\d+) PlayerID=(\d+) GameAccountId=\[hi=\d+ lo=(\d+)\]",
            seg,
        ):
            local[m.group(1)] = (int(m.group(2)), m.group(3))
        if not local:
            continue
        for _eid, (pid, lo) in local.items():
            lo_map[lo].add(pid)

        # 本局出现的名字
        local_names: list[str] = []
        for m in re.finditer(r"Entity=([^\s#]+)#\d+ tag=", seg):
            if m.group(1) not in local_names:
                local_names.append(m.group(1))
        for nm in local_names:
            names[nm]  # touch
            game_count[nm] += 1

        # FIRST_PLAYER 权威配对：先手 = 某个 PlayerID
        fp_pid = 0
        for m in re.finditer(r"TAG_CHANGE Entity=(\d+) tag=FIRST_PLAYER value=1", seg):
            got = local.get(m.group(1))
            if got:
                fp_pid = got[0]
                break
        if not fp_pid:
            # 降级：无法配对时按出现顺序（仅用于给出粗略分布）
            for idx, nm in enumerate(local_names[:2]):
                if idx + 1 in (1, 2):
                    names[nm].add(idx + 1)
            continue

        fp_names = set(
            m.group(1)
            for m in re.finditer(
                r"Entity=([^\s#]+)#\d+ tag=FIRST_PLAYER value=1", seg
            )
        )
        other_pid = 3 - fp_pid
        for nm in local_names:
            pid = fp_pid if nm in fp_names else other_pid
            names[nm].add(pid)
            if nm in fp_names:
                for _eid, (p, lo) in local.items():
                    if p == fp_pid:
                        # 同一账号每局配对到的名字可能不同（换角色时
                        # 英雄名会变），用投票取众数，避免 first-seen 偏差
                        name_votes[lo][nm] += 1
                        break

    name_of_lo = {
        lo: cnt.most_common(1)[0][0] for lo, cnt in name_votes.items() if cnt
    }
    return {
        "names": {k: v for k, v in names.items() if v},
        "lo_map": {k: v for k, v in lo_map.items() if v},
        "name_of_lo": name_of_lo,
        "game_count": dict(game_count),
    }


def parse_log_file(
    path: str | Path,
    recon: GameStateReconstructor | None = None,
    player_name: str = "",
    account_id: str = "",
    parser: PowerLogParser | None = None,
) -> GameStateReconstructor:
    """离线全量回放一个 Power.log，返回终局状态。

    ``player_name``：本机玩家显示名（辅助校验）。
    ``account_id``：本机战网账号 lo 值（**可靠的身份标识**）。
    ``parser``：可选的解析器实例。实时监听回放时需要与后续增量跟读共用
      同一个实例，否则「名#号」虚拟实体 id / FIRST_PLAYER 配对会对不上。
    """
    recon = recon or GameStateReconstructor()
    recon.my_account = (account_id or "").strip()
    recon.my_player_name = (player_name or "").strip()
    parser = parser or PowerLogParser()
    parser.set_player_name(player_name)
    # recon 需要用 parser 的「名#号 → 玩家编号」换算来同步法力资源
    recon._slot_resolver = parser.player_slot_of
    # 名字归属被纠正时，通知 recon 迁移玩家级 tag（法力等）
    parser._on_rebind = recon.rebind_player_tags
    recon._pname_resolver = parser.player_name_of
    with Path(path).open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            action = parser.feed_line(line)
            if action:
                recon.apply(action)
    last = parser.flush()
    if last:
        recon.apply(last)
    # 玩家名↔PlayerID 的权威配对依赖 FIRST_PLAYER（两侧出现顺序不固定），
    # 因此必须在全部 tag 应用完成后统一结算+ 刷新显示名。
    parser.resolve_first_player_pairs()
    # 权威配对可能纠正了名字归属：统一重放玩家级 tag（法力等），
    # 并覆盖早期按出现顺序建立的槽位映射，避免一侧法力恒为 0/0。
    recon.resync_player_tags()
    recon.apply_player_names()
    return recon


def iter_actions(path: str | Path) -> Iterator[Action]:
    """流式产出动作，供实时 tail 使用。"""
    parser = PowerLogParser()
    with Path(path).open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            action = parser.feed_line(line)
            if action:
                yield action
    last = parser.flush()
    if last:
        yield last