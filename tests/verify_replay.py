"""离线回放验证：切出未截断的对局段落，验证状态还原准确性。

背景：现有Power.log 尾部被 10MB 上限截断，实体初始声明（CARDTYPE/RESOURCES 等）已丢失，
还原结果会缺失法力与牌库数。本脚本按CREATE_GAME 边界切出完整对局做权威验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hs_copilot.cards import CardDB
from hs_copilot.config import find_latest_power_log
from hs_copilot.log_parser import GameStateReconstructor, PowerLogParser

_latest = find_latest_power_log()
DEFAULT_LOG = str(_latest) if _latest else ""
# 用法：python tests/verify_replay.py [Power.log 路径]
# 没有本机日志时请显式传入路径，否则脚本会提示而不是崩溃。


def replay_prefix(log_path: str, max_games: int = 1) -> GameStateReconstructor:
    """回放前 N 场完整对局（以 CREATE_GAME 为边界）。"""
    recon = GameStateReconstructor()
    parser = PowerLogParser()
    games = 0
    with open(log_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            action = parser.feed_line(line)
            if not action:
                continue
            if action.action in ("CREATE_GAME", "RESET_GAME"):
                games += 1
                if games > max_games:
                    break
            recon.apply(action)
    return recon


def main() -> int:
    log = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG
    if not log:
        print("未找到 Power.log；请显式传入路径：python tests/verify_replay.py <Power.log>")
        return 2
    db = CardDB.load(Path(__file__).resolve().parents[1] / "data" / "cards_zhCN.json")

    recon = replay_prefix(log)
    print(f"日志：{log}")
    print(f"实体总数：{len(recon.entities)}  回合：{recon.turn}  步骤：{recon.step}")
    print()

    failures: list[str] = []

    for pid in (1, 2):
        v = recon.player_view(pid)
        print(f"=== 玩家 {pid} ===")
        print(f"  英雄        : {v.hero.display_name if v.hero else '缺失'}"
              f" ({v.hero.card_id if v.hero else '-'})")
        print(f"  攻血/护甲   : {v.hero_atk}/{v.hero_hp} + {v.armor}")
        print(f"  法力        : {v.mana}/{v.mana_max}")
        print(f"  手牌        : {len(v.hand)}")
        print(f"  场面        : {len(v.board)}")
        print(f"  牌库        : {v.deck_count}")
        print(f"  墓地        : {len(v.graveyard)}")
        print(f"  技能        : {v.hero_power.display_name if v.hero_power else '缺失'}"
              f" ({v.hero_power.cost if v.hero_power else '-'}费)")
        print()

        # ── 校验 ──
        if not v.hero:
            failures.append(f"玩家{pid}：英雄未识别")
        elif v.hero_hp <= 0:
            failures.append(f"玩家{pid}：英雄血量异常 {v.hero_hp}")
        if not v.hero_power:
            failures.append(f"玩家{pid}：英雄技能未识别")
        if len(v.hand) not in (3, 4, 5, 6, 7, 8):
            failures.append(f"玩家{pid}：起手手牌数异常 {len(v.hand)}")
        if v.deck_count <= 0:
            failures.append(f"玩家{pid}：牌库数为 {v.deck_count}（实体声明可能已丢失）")

        for e in v.hand[:4]:
            card = db.by_name(e.display_name)
            if card:
                print(f"    手牌 #{e.display_name} {card.cost}费 "
                      f"{card.attack}/{card.health} {card.text[:24]}")

    print()
    if failures:
        print("校验未通过：")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("✓ 校验通过：双方英雄 / 技能 / 手牌 / 牌库 均正确还原")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())