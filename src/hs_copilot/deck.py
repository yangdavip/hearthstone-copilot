"""
炉石助手 · 牌库推演

从套牌码（deckstring）解析完整卡表，结合对局进程推算：
- 套牌构成、法力曲线、关键牌
- 已打出/ 已入手 / 尚未见到的牌
- 起手留牌参考

deckstring 格式（依据官方 ``hearthstone`` 库``deckstrings.py`` 的权威实现，
并通过编解码闭环测试）::

    \\x00 <varint version=1> <varint format>
         <varint num_heroes> <varint hero_dbfId>*        # 英雄段
         <varint n_x1> <varint dbfId>*                   # 数量为1的牌
         <varint n_x2> <varint dbfId>*                   # 数量为2的牌
         <varint n_xn> (<varint dbfId> <varint count>)*  # 其他数量的牌
         <\\x00 | \\x01 + 副牌库段>
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable

from .cards import Card, CardDB

DECKSTRING_VERSION = 1

#: 常见模式（对应 FormatType）
FORMAT_NAMES = {
    1: "标准",
    2: "狂野",
    3: "专家",
    4: "竞技场",
    5: "乱斗",
    7: "冒险",
    8: "酒馆战棋",
    9: "双人",
    10: "英雄乱斗",
    11: "限时",
    12: "高乱",
    13: "御龙",
    14: "街机",
    15: "标准(探索)",
    16: "天梯",
    17: "祭典",
    18: "构筑",
    19: "狂野2024",
    20: "标准2024",
    21: "中立的",
}


def _read_varint(stream: BytesIO) -> int:
    shift = 0
    result = 0
    while True:
        c = stream.read(1)
        if c == b"":
            raise EOFError("套牌码意外结束")
        i = c[0]
        result |= (i & 0x7F) << shift
        shift += 7
        if not (i & 0x80):
            break
    return result


def _write_varint(stream: BytesIO, n: int) -> None:
    while True:
        towrite = n & 0x7F
        n >>= 7
        if n:
            stream.write(bytes((towrite | 0x80,)))
        else:
            stream.write(bytes((towrite,)))
            break


def _b64_decode(code: str) -> BytesIO:
    """炉石用标准 base64；部分平台会附加不可见字符，先清洗。"""
    s = "".join(ch for ch in code.strip() if ch.isalnum() or ch in "+/=-_")
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return BytesIO(base64.b64decode(s))


@dataclass
class ParsedDeck:
    """套牌码解析结果。"""

    cards: list[tuple[int, int]] = field(default_factory=list)
    heroes: list[int] = field(default_factory=list)
    format: int = 0
    sideboards: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(c for _, c in self.cards)

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.format, f"模式{self.format}")


def parse_deckstring(code: str) -> ParsedDeck:
    """解析套牌码。任何异常都收敛为「尽可能解析出已识别部分」。"""
    out = ParsedDeck()
    if not code or not code.strip():
        return out
    try:
        data = _b64_decode(code)
        if data.read(1) != b"\0":
            return out
        version = _read_varint(data)
        if version != DECKSTRING_VERSION:
            return out
        out.format = _read_varint(data)

        # 英雄段
        n_heroes = _read_varint(data)
        for _ in range(n_heroes):
            out.heroes.append(_read_varint(data))

        # 卡牌段：按数量分三组
        for _ in range(_read_varint(data)):
            out.cards.append((_read_varint(data), 1))
        for _ in range(_read_varint(data)):
            out.cards.append((_read_varint(data), 2))
        n_xn = _read_varint(data)
        for _ in range(n_xn):
            cid = _read_varint(data)
            cnt = _read_varint(data)
            out.cards.append((cid, cnt))

        out.cards.sort()

        # 副牌库段
        if data.read(1) == b"\1":
            for _ in range(_read_varint(data)):
                out.sideboards.append((_read_varint(data), 1, _read_varint(data)))
            for _ in range(_read_varint(data)):
                out.sideboards.append((_read_varint(data), 2, _read_varint(data)))
            n_sb_xn = _read_varint(data)
            for _ in range(n_sb_xn):
                cid = _read_varint(data)
                cnt = _read_varint(data)
                owner = _read_varint(data)
                out.sideboards.append((cid, cnt, owner))
            out.sideboards.sort(key=lambda x: (x[2], x[0]))
    except (EOFError, ValueError, IndexError):
        # 部分平台在标准串后追加扩展字节，尾部解析失败时保留已解出的内容
        pass
    return out


def write_deckstring(cards: Iterable[tuple[int, int]], hero: int, fmt: int = 2) -> str:
    """把 ``[(dbfId, count)] + hero`` 编回套牌码。"""

    def trisort(items: list[tuple[int, int]]) -> tuple[list, list, list]:
        x1 = [x for x in items if x[1] == 1]
        x2 = [x for x in items if x[1] == 2]
        xn = [x for x in items if x[1] not in (1, 2)]
        return sorted(x1), sorted(x2), sorted(xn)

    buf = BytesIO()
    buf.write(b"\0")
    _write_varint(buf, DECKSTRING_VERSION)
    _write_varint(buf, fmt)
    _write_varint(buf, 1)  # 英雄数
    _write_varint(buf, hero)
    x1, x2, xn = trisort(list(cards))
    for lst in (x1, x2):
        _write_varint(buf, len(lst))
        for cid, _ in lst:
            _write_varint(buf, cid)
    _write_varint(buf, len(xn))
    for cid, cnt in xn:
        _write_varint(buf, cid)
        _write_varint(buf, cnt)
    buf.write(b"\0")
    return base64.b64encode(buf.getvalue()).decode()


# ─────────────────────────────────────────────────────────────
# 套牌追踪
# ─────────────────────────────────────────────────────────────


@dataclass
class DeckTracker:
    """跟踪一副套牌在战斗中的消耗情况。"""

    card_db: CardDB
    deck_name: str = ""
    format_name: str = ""
    hero: str = ""
    #: 卡名 -> 总数
    total: dict[str, int] = field(default_factory=dict)
    #: 卡名 -> 已打出/已消耗数量
    seen: dict[str, int] = field(default_factory=dict)
    #: 卡名 -> 当前手中持有数
    in_hand: dict[str, int] = field(default_factory=dict)
    #: 卡名 -> 卡牌 id
    id_map: dict[str, str] = field(default_factory=dict)
    #: 套牌码里有、但本地卡表查不到的 dbfId（卡表需要 refresh-cards）
    unknown_dbfids: list[int] = field(default_factory=list)
    #: 自动识别来源：Decks.log 里的 Deck ID，用于换套牌时重建整局计划
    deck_id: str = ""

    @classmethod
    def from_deckstring(
        cls, code: str, db: CardDB, name: str = "我的套牌"
    ) -> "DeckTracker":
        t = cls(card_db=db, deck_name=name)
        parsed = parse_deckstring(code)
        t.format_name = parsed.format_name
        for dbf, count in parsed.cards:
            card = db.by_dbfid(dbf)
            if card:
                t.total[card.name] = t.total.get(card.name, 0) + count
                t.id_map[card.name] = card.id
            else:
                t.unknown_dbfids.extend([dbf] * count)
        if parsed.heroes:
            h = db.by_dbfid(parsed.heroes[0])
            if h:
                t.hero = h.name
                t.id_map[h.name] = h.id
        return t

    # ── 状态更新 ──
    def note_played(self, name: str) -> None:
        if name in self.total:
            self.seen[name] = self.seen.get(name, 0) + 1
            if self.in_hand.get(name):
                self.in_hand[name] -= 1

    def note_in_hand(self, names: Iterable[str]) -> None:
        self.in_hand = {}
        for n in names:
            self.in_hand[n] = self.in_hand.get(n, 0) + 1

    # ── 查询 ──
    @property
    def size(self) -> int:
        return sum(self.total.values())

    @property
    def played_count(self) -> int:
        return sum(self.seen.values())

    def remaining_count(self, name: str) -> int:
        return max(0, self.total.get(name, 0) - self.seen.get(name, 0))

    def unseen(self) -> dict[str, int]:
        """尚未见到的牌（可能在牌库中）。"""
        return {n: self.remaining_count(n) for n in self.total if self.remaining_count(n) > 0}

    def key_cards(self, limit: int = 8) -> list[dict]:
        """挑出值得留意的关键牌（剩余数 × 战术价值）。"""
        bonus_map = {
            "COMBO": 3,
            "SECRET": 3,
            "BATTLECRY": 2,
            "DEATHRATTLE": 2,
            "AURA": 2,
            "SPELLPOWER": 2,
            "DIVINE_SHIELD": 2,
        }
        scored: list[tuple[int, str, int]] = []
        for n, rem in self.unseen().items():
            c = self.card_db.by_name(n)
            bonus = max((bonus_map.get(k, 0) for k in (c.keywords if c else [])), default=0)
            if c and c.cost <= 2:
                bonus += 1
            scored.append((bonus * 10 + rem, n, rem))
        scored.sort(reverse=True)
        out = []
        for _, n, rem in scored[:limit]:
            c = self.card_db.by_name(n)
            out.append(
                {
                    "name": n,
                    "remaining": rem,
                    "cost": c.cost if c else None,
                    "keywords": c.keywords if c else [],
                }
            )
        return out

    def curve(self) -> dict[str, int]:
        c: dict[int, int] = {}
        for n, cnt in self.total.items():
            card = self.card_db.by_name(n)
            c[card.cost if card else 0] = c.get(card.cost if card else 0, 0) + cnt
        return {str(k): v for k, v in sorted(c.items())}

    def to_dict(self) -> dict:
        return {
            "name": self.deck_name,
            "format": self.format_name,
            "hero": self.hero,
            "size": self.size,
            "cards_identified": len(self.total),
            "played": self.played_count,
            "unseen_total": sum(self.unseen().values()),
            "key_cards_remaining": self.key_cards(),
            "mana_curve": self.curve(),
            "unidentified_cards": len(self.unknown_dbfids),
        }


def resolve_deck(db: CardDB, ref: str) -> DeckTracker:
    """按套牌码构建追踪器；空串返回空追踪器。"""
    ref = (ref or "").strip()
    if not ref:
        return DeckTracker(card_db=db)
    return DeckTracker.from_deckstring(ref, db)


# ─────────────────────────────────────────────────────────────
# 从 Decks.log 自动识别「这一局用的哪副牌」
# ─────────────────────────────────────────────────────────────

@dataclass
class ActiveDeck:
    """Decks.log 里最近一次用于匹配 / 编辑的套牌。"""

    name: str = ""
    deck_id: str = ""
    code: str = ""
    source: str = ""

    @property
    def key(self) -> str:
        """整局计划的缓存键：换套牌 / 换日志目录都要重新规划。"""
        return self.deck_id or self.code[:24]


_DECK_BODY_RE = re.compile(r"^[A-Z]\s+[\d:.]+\s+(.*)$")
#: 套牌码前缀随版本 / 格式变化：实测同一份 Decks.log 里既有 ``AAEC...``
#: 也有 ``AAEBAQcBrYoDAAAA``（老狂野卡组）。只认 AAEC 会漏掉后者。
#: 统一按 AAE 开头 + base64 字符集 + 足够长度判断。
_DECK_CODE_RE = re.compile(r"^AAE[A-Za-z0-9+/=_-]{12,}$")
#: 只有这两个标记后面跟的才是「当前正在用 / 刚编辑完」的套牌。
#: 列表型的 ``Deck Contents Received`` 会一次打印全部收藏卡组，不能拿来当当前套牌。
_ACTIVE_MARKERS = ("Finding Game With Deck:", "Finished Editing Deck:")


def find_active_deck(power_log: str | Path | None) -> ActiveDeck | None:
    """从 Power.log 同目录的 ``Decks.log`` 找出本局使用的套牌。

    实测日志（macOS 客户端）在每次点「开始匹配」时会写一段::

        I 00:54:47.7766860 Finding Game With Deck:
        I 00:54:47.7766860 ### 超级进化萨
        I 00:54:47.7766860 # Deck ID: 9465884738
        I 00:54:47.7766860 AAECAfrwBhSqigT9nwSooATOngae+QbEgQeilgfknQfQvwfTw...

    取**最后一段完整块**即可，比让用户在 config.yaml 手填 deckstring 可靠得多：
    换卡组、换日志目录都会自动跟上。返回 None 时调用方应回退到手填配置。
    """
    if not power_log:
        return None
    p = Path(power_log)
    decks_log = (p.parent / "Decks.log") if p.name.endswith(".log") else (p / "Decks.log")
    if not decks_log.exists():
        return None
    try:
        lines = decks_log.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None

    best: ActiveDeck | None = None
    pending: dict[str, str] | None = None
    for line in lines:
        m = _DECK_BODY_RE.match(line.strip())
        body = (m.group(1) if m else line).strip()
        if body in _ACTIVE_MARKERS:
            pending = {"name": "", "deck_id": "", "code": "", "source": body.rstrip(":")}
            continue
        if pending is None:
            continue
        if body.startswith("### "):
            pending["name"] = body[4:].strip()
        elif body.startswith("# Deck ID:"):
            pending["deck_id"] = body.split(":", 1)[1].strip()
        elif _DECK_CODE_RE.match(body):
            pending["code"] = body
            if pending["name"] or pending["code"]:
                best = ActiveDeck(
                    name=pending["name"] or "未命名套牌",
                    deck_id=pending["deck_id"],
                    code=pending["code"],
                    source=pending["source"],
                )
            pending = None
    return best


def resolve_active_deck(
    db: CardDB,
    configured_code: str,
    power_log: str | Path | None,
) -> tuple[DeckTracker, ActiveDeck | None]:
    """优先用配置里手填的套牌码，否则用 Decks.log 自动识别。"""
    ref = (configured_code or "").strip()
    active = find_active_deck(power_log)
    if ref:
        t = DeckTracker.from_deckstring(ref, db, name="手填套牌")
        if active:
            t.deck_name = active.name
            t.deck_id = active.deck_id or "manual"
        return t, active
    if active and active.code:
        try:
            t = DeckTracker.from_deckstring(active.code, db, name=active.name)
            t.deck_id = active.deck_id
            return t, active
        except Exception:
            return DeckTracker(card_db=db), None
    return DeckTracker(card_db=db), None