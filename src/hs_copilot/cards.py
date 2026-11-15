"""
炉石助手 · 卡牌知识库

数据源：HearthstoneJSON（https://hearthstonejson.com/）
提供：中文卡牌查询、费用/攻血/关键词、卡牌效果文本（供 LLM 理解新卡）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CARDS_PATH = Path(__file__).resolve().parents[2] / "data" / "cards_zhCN.json"

# 卡牌类型
TYPE_MINION = "MINION"
TYPE_SPELL = "SPELL"
TYPE_WEAPON = "WEAPON"
TYPE_HERO = "HERO"
TYPE_HERO_POWER = "HERO_POWER"

# 职业
CLASSES = {
    "DEATHKNIGHT": "死亡骑士",
    "DRUID": "德鲁伊",
    "HUNTER": "猎人",
    "MAGE": "法师",
    "PALADIN": "圣骑士",
    "PRIEST": "牧师",
    "ROGUE": "潜行者",
    "SHAMAN": "萨满祭司",
    "WARLOCK": "术士",
    "WARRIOR": "战士",
    "DEMONHUNTER": "恶魔猎手",
    "NEUTRAL": "中立",
}

ZONE_NAMES = {
    "PLAY": "战场",
    "HAND": "手牌",
    "DECK": "牌库",
    "GRAVEYARD": "墓地",
    "SECRET": "奥秘",
    "SETASIDE": "暂存",
    "REMOVEDFROMGAME": "已移出游戏",
}


def strip_html(text: str | None) -> str:
    """卡牌文本含 <b>/<i> 等标签，剥掉便于 LLM 理解。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\n", " ").strip()
    return text


#: 英雄卡在运行时被赋予的实体名（如 HERO_04b），需靠卡表反查中文职业名。
#: 日志中这类实体的 name 字段就是卡号本身，对 LLM 不可读。
def resolve_hero_name(card_id: str, db: "CardDB") -> str:
    """把英雄卡号解析为可读名称。"""
    c = db.by_id(card_id)
    if c and c.name:
        return c.name
    return card_id


@dataclass
class Card:
    """一张卡牌的静态信息。"""

    id: str
    name: str
    #:炉石套牌码使用 dbfId，与卡牌 id 字符串不同
    dbf_id: int = 0
    cost: int = 0
    attack: int = 0
    health: int = 0
    type: str = TYPE_MINION
    card_class: str = "NEUTRAL"
    rarity: str = ""
    set_name: str = ""
    text: str = ""
    mechanics: list[str] = field(default_factory=list)
    referenced_tags: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    collectible: bool = False
    race: str = ""
    referenced_keywords: dict[str, str] = field(default_factory=dict)

    @property
    def is_minion(self) -> bool:
        return self.type == TYPE_MINION

    @property
    def keywords(self) -> list[str]:
        """合并mechanics 与 referencedTags，去重。"""
        return list(dict.fromkeys(self.mechanics + self.referenced_tags))

    def to_dict(self) -> dict[str, Any]:
        """喂给 LLM 的紧凑表示——刻意不含 flavor/art 等无用字段，省 token。"""
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "cost": self.cost,
            "type": self.type,
        }
        if self.type == TYPE_MINION:
            d["atk"] = self.attack
            d["hp"] = self.health
        elif self.type == TYPE_WEAPON:
            d["atk"] = self.attack
        if self.text:
            d["text"] = self.text
        if self.keywords:
            d["keywords"] = self.keywords
        if self.race:
            d["race"] = self.race
        if len(self.classes) > 1:
            d["classes"] = self.classes
        return d


def _name_rank(c: Card) -> tuple[int, int, int, int]:
    """同名卡按名字反查时的优先级（越大越优先）。

    卡表里同名条目极多：酒馆战棋英雄卡（BG*）、衍生/附魔载体、
    旧版重印（CORE_* / EX1_*）。正式对战卡应是「非战棋 + 可收藏 +
    有中文效果文本 + 非英雄/附魔」的那一条。
    """
    cid = c.id or ""
    return (
        0 if cid.startswith("BG") else 1,
        1 if c.collectible else 0,
        1 if bool(c.text) else 0,
        1 if c.type not in ("HERO", "HERO_POWER", "ENCHANTMENT") else 0,
    )


class CardDB:
    """卡牌库。启动时加载一次，常驻内存。"""

    def __init__(self, cards: list[Card], dbfid_index: dict[int, str] | None = None) -> None:
        self._by_id: dict[str, Card] = {}
        self._by_name: dict[str, Card] = {}
        #: dbfId -> 卡名（deckstring 解析必需，炉石套牌码用的是 dbfId）
        self.dbfid_to_name: dict[int, str] = dbfid_index or {}
        for c in cards:
            self._by_id[c.id] = c
            if not c.name:
                continue
            # 同名卡很多：酒馆战棋英雄、衍生物、附魔载体与正式卡牌重名。
            # 旧版「保留首个」实测把「梦魇之王萨维斯」匹配成 BG36_HERO_105
            # （0 费英雄、无效果文本），导致模型收到完全错误的卡牌描述。
            prev = self._by_name.get(c.name)
            if prev is None or _name_rank(c) > _name_rank(prev):
                self._by_name[c.name] = c

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CardDB":
        p = Path(path) if path else DEFAULT_CARDS_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"卡牌数据不存在：{p}\n请先运行 `hs-copilot refresh-cards` 下载。"
            )
        raw = json.loads(p.read_text(encoding="utf-8"))
        # HearthstoneJSON v1 返回顶层是 list；部分镜像返回 {build, cards}
        if isinstance(raw, dict):
            raw = raw.get("cards", [])
        cards: list[Card] = []
        dbfid_index: dict[int, str] = {}
        for item in raw:
            name = item.get("name", "")
            dbf = item.get("dbfId")
            if dbf is not None and name:
                dbfid_index[dbf] = name
            cards.append(
                Card(
                    id=item.get("id", ""),
                    name=name,
                    dbf_id=dbf if dbf is not None else 0,
                    cost=item.get("cost", 0) or 0,
                    attack=item.get("attack", 0) or 0,
                    health=item.get("health", 0) or 0,
                    type=item.get("type", TYPE_MINION),
                    card_class=item.get("cardClass", "NEUTRAL"),
                    rarity=item.get("rarity", ""),
                    set_name=item.get("set", ""),
                    text=strip_html(item.get("text")),
                    mechanics=item.get("mechanics", []) or [],
                    referenced_tags=item.get("referencedTags", []) or [],
                    classes=item.get("classes", []) or [],
                    collectible=item.get("collectible", False),
                    race=item.get("race", "") or "",
                )
            )
        return cls(cards, dbfid_index)

    def __len__(self) -> int:
        return len(self._by_id)

    def by_id(self, card_id: str) -> Card | None:
        return self._by_id.get(card_id)

    def by_name(self, name: str) -> Card | None:
        return self._by_name.get(name)

    def get(self, card_id: str = "", name: str = "") -> Card | None:
        if card_id:
            return self._by_id.get(card_id)
        if name:
            return self._by_name.get(name)
        return None

    def by_dbfid(self, dbf: int) -> Card | None:
        """按 dbfId 查卡（套牌码内部使用 dbfId 而非卡牌 id 字符串）。"""
        name = self.dbfid_to_name.get(dbf)
        return self._by_name.get(name) if name else None

    def enrich(self, names: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批量补充卡牌静态信息，返回 {卡名: 精简字典}。"""
        out: dict[str, dict[str, Any]] = {}
        for n in names:
            c = self.by_name(n) or self.by_id(n)
            if c:
                out[n] = c.to_dict()
        return out