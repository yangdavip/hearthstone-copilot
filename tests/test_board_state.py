"""局面读取的回归测试：防止「场面随从数虚高 / 敌我颠倒」这类问题复发。

这些用例都来自实测踩过的坑：
1. 场上并存多种非随从实体（英雄/技能/武器/附魔/占位实体），不能都算成随从
2. 英雄技能必须取 PLAY 区的实体，不能取到牌库区的同名实体
3. 玩家身份绑定不能被历史对局的玩家名污染
4. 实体 id 跨对局复用，新对局必须换代
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hs_copilot.log_parser import (  # noqa: E402
    T,
    GameStateReconstructor,
)

#: 实测存在的日志（用于回填测试）。仓库 clone 下来没有本机日志时自动跳过相关用例。
def _find_real_log() -> str | None:
    from hs_copilot.config import find_latest_power_log

    p = find_latest_power_log()
    if p is not None:
        return str(p)
    legacy = Path(
        "/Applications/Hearthstone/Logs/Hearthstone_2026_10_09_19_43_04/Power.log"
    )
    return str(legacy) if legacy.exists() else None


DEFAULT_LOG = _find_real_log() or ""

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        FAILURES.append(msg)


def build_roster() -> GameStateReconstructor:
    """构造一个「什么实体都混在场上」的局面。"""
    r = GameStateReconstructor()
    r.game_started = True
    r.turn = 5

    # 玩家实体与英雄（整份日志只声明一次）
    for pid, hero_eid, power_eid, hero_cid in ((1, 64, 65, "HERO_02a"), (2, 66, 67, "HERO_05")):
        pe = r._entity(2 if pid == 1 else 3)
        pe.declared.update({
            T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
            "HERO_ENTITY": str(hero_eid), T.CONTROLLER: str(pid),
            T.ZONE: "PLAY", "MAXHANDSIZE": "10",
            T.RESOURCES: "5", T.RESOURCES_USED: "2",
        })
        pe.tags.update({T.RESOURCES: "5", T.RESOURCES_USED: "2"})
        h = r._entity(hero_eid)
        h.card_id = hero_cid
        h.declared.update({T.CARDTYPE: "HERO", T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.HEALTH: "30", T.ATK: "0"})
        hp = r._entity(power_eid)
        hp.card_id = "CS2_049_H1" if pid == 1 else "HERO_05bp"
        hp.declared.update({T.CARDTYPE: "HERO_POWER", T.CONTROLLER: str(pid), T.ZONE: "PLAY"})
        hp.tags.update({T.ZONE: "PLAY", T.CONTROLLER: str(pid)})

    def mk(eid, cid, ctype, ctrl, zone, pos, atk=0, hp_=0):
        e = r._entity(eid)
        e.card_id = cid
        if ctype:
            e.declared[T.CARDTYPE] = ctype
        e.declared.update({T.ZONE: zone, T.ZONE_POSITION: str(pos), T.CONTROLLER: str(ctrl)})
        e.tags.update({T.ZONE: zone, T.ZONE_POSITION: str(pos), T.CONTROLLER: str(ctrl)})
        if atk or hp_:
            e.tags.update({T.ATK: str(atk), T.HEALTH: str(hp_)})
        return e

    # 玩家2 场上的「脏东西」—— 每一种都不该被算成随从
    mk(70, "", None, 2, "PLAY", 0)  # 占位实体（cardId 空）
    mk(71, "", None, 2, "PLAY", 0)  # 占位实体
    e = mk(72, "", None, 2, "PLAY", 0)
    e.name = "UNKNOWN ENTITY [cardType=INVALID]"  # 换牌界面占位
    mk(73, "", "ENCHANTMENT", 2, "PLAY", 0)  # 附魔
    mk(74, "WEAPON_01", "WEAPON", 2, "PLAY", 0, 3, 2)  # 武器

    # 玩家2 真正的随从：3 个
    mk(80, "BAR_035t", "MINION", 2, "PLAY", 1, 1, 1)
    mk(81, "CATA_185", "MINION", 2, "PLAY", 2, 3, 4)
    mk(82, "TIME_609t2", "MINION", 2, "PLAY", 3, 2, 4)
    # 牌库里的技能同名实体（不应被当成生效技能）
    mk(83, "CS2_049_H1", "HERO_POWER", 2, "DECK", 0)

    return r


def test_minion_count() -> None:
    print("\n[测试1] 场面随从数只算真随从")
    r = build_roster()
    v = r.player_view(2)
    check(len(v.board) == 3, f"玩家2 场面应为 3 个随从，实际 {len(v.board)}")
    names = {e.card_id for e in v.board}
    check(names == {"BAR_035t", "CATA_185", "TIME_609t2"}, "只计入三个 MINION 实体")
    check(v.weapon is not None, "武器被正确识别（不计入随从）")
    check(v.hero is not None, "英雄被正确识别（不计入随从）")


def test_hero_power_zone() -> None:
    print("\n[测试2] 英雄技能取 PLAY 区实体，不是牌库区的")
    r = build_roster()
    v = r.player_view(2)
    check(v.hero_power is not None, "英雄技能存在")
    if v.hero_power:
        check(
            v.hero_power.card_id == "HERO_05bp",
            f"技能应为 HERO_05bp（稳固射击），实际 {v.hero_power.card_id}",
        )
        check(v.hero_power.zone == "PLAY", f"技能所在区应为 PLAY，实际 {v.hero_power.zone}")


def test_hero_hp() -> None:
    print("\n[测试3] 英雄血量与承伤计算")
    r = build_roster()
    h = r.hero_of(1)
    check(h is not None, "hero_of 能定位英雄")
    if h:
        r.entities[h.id].tags["DAMAGE"] = "12"
        check(r.player_view(1).hero_hp == 18, f"30-12=18 血，实际 {r.player_view(1).hero_hp}")


def test_player_name_binding() -> None:
    print("\n[测试4] 玩家名按对局重置，避免历史名字挤占槽位")
    from hs_copilot.log_parser import PowerLogParser

    p = PowerLogParser()
    p.set_player_name("我")

    # 第一局：只有 甲/乙
    for name in ("甲#1", "乙#2"):
        p._resolve_id(name)
        p.player_slot_of(p._named_ids[name])
    check(
        p._pname_order == ["甲", "乙"],
        f"第一局只认前两名（实际 {p._pname_order}）",
    )

    # 新对局：CREATE_GAME 会调reset_player_order，历史名字必须清掉
    p.reset_player_order()
    for name in ("丙#3", "丁#4"):
        p._resolve_id(name)
        p.player_slot_of(p._named_ids[name])
    check(
        p._pname_order == ["丙", "丁"],
        f"第二局重置为丙/丁（实际 {p._pname_order}）",
    )
    p.reset_player_order()
    check(p._pname_order == [], "新对局已清空顺序表")
    check("丙#3" not in p._named_ids, "旧对局的虚拟 id 已作废")
    check(p.resolve_my_pid() == 0, "新对局未收集满两名时 resolve_my_pid 返回 0")


def test_backfill_current_game() -> None:
    """回归：GUI 启动时必须能读到当前对局的历史。

    实测问题：炉石一个会话写多个对局到同一 Power.log，
    LogWatcher 若从文件末尾开始跟读，CREATE_GAME 与 FULL_ENTITY 声明
    都已在历史里，实体表保持为空 → 血量/场面/手牌全显示 0，
    大模型据此回复「双方生命均为0，对局已结束」。
    """
    print("\n[测试5] LogWatcher 回填当前对局（否则全是 0）")
    import shutil
    import tempfile

    from hs_copilot.log_parser import parse_log_file
    from hs_copilot.watcher import LogWatcher

    src = Path(DEFAULT_LOG) if DEFAULT_LOG else None
    if src is None or not src.exists():
        print("     （跳过：本机无炉石日志）")
        return

    # 复制一份，避免测试期间被游戏写入
    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir) / "Power.log"
    shutil.copy(src, tmp)

    try:
        # 对照：不回填 → 实体表为空（复现 bug 现场）
        w0 = LogWatcher(tmp, start_from_end=True)
        w0.poll(timeout=0.1)
        check(
            len(w0.recon.entities) == 0,
            "对照：不回填时实体表为空（这就是 bug 现场）",
        )

        # 回填 → 与全量回放结果一致
        w = LogWatcher(tmp, start_from_end=True, player_name="玩家甲")
        w.backfill_current_game()
        ref = parse_log_file(tmp, player_name="玩家甲")

        check(len(w.recon.entities) > 0, f"回填后实体表非空（{len(w.recon.entities)} 个）")
        check(w.recon.turn == ref.turn, f"回合数一致（{w.recon.turn} vs {ref.turn}）")

        for pid in (1, 2):
            v = w.recon.player_view(pid)
            f = ref.player_view(pid)
            check(
                v.hero_hp == f.hero_hp,
                f"P{pid} 血量与全量回放一致（{v.hero_hp} vs {f.hero_hp}）",
            )
            check(
                len(v.board) == len(f.board),
                f"P{pid} 场面数一致（{len(v.board)} vs {len(f.board)}）",
            )
            check(
                len(v.hand) == len(f.hand),
                f"P{pid} 手牌数一致（{len(v.hand)} vs {len(f.hand)}）",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_hand_unknown_cards() -> None:
    """回归：卡号未揭示的手牌必须仍被计入，不能被当成「没有手牌」。

    实测问题：抽牌时手牌以「无卡号占位实体」入区（cardId 为空），
    原过滤条件要求「卡号非空」，导致我方手牌整体消失，
    模型据此回复「手牌为空，只能空过」—— 而实际手里是有牌的。
    """
    print("\n[测试6] 卡号未揭示的手牌仍计入手牌数")
    r = GameStateReconstructor()
    r.game_started = True

    def mkpid(pid, eid, hero_eid):
        pe = r._entity(eid)
        pe.declared.update({T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
                            "HERO_ENTITY": str(hero_eid), T.CONTROLLER: str(pid), T.ZONE: "PLAY"})
        h = r._entity(hero_eid)
        h.declared.update({T.CARDTYPE: "HERO", T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.HEALTH: "30"})
        h.tags.update({T.HEALTH: "30", T.ZONE: "PLAY", T.CONTROLLER: str(pid)})

    mkpid(1, 2, 64)
    mkpid(2, 3, 66)

    # 我方 4 张手牌：卡号全空（占位实体）
    for i, eid in enumerate((7, 22, 41, 106), 1):
        e = r._entity(eid)
        e.declared.update({T.ZONE: "HAND", T.ZONE_POSITION: str(i), T.CONTROLLER: "1"})
        e.tags.update({T.ZONE: "HAND", T.ZONE_POSITION: str(i), T.CONTROLLER: "1"})
        e.name = "UNKNOWN ENTITY [cardType=INVALID]"

    # 对手 2 张手牌：有卡号（客户端不可见，仅被揭示时写入）
    for i, (eid, cid) in enumerate(((52, "TLC_888"), (54, "CATA_497")), 1):
        e = r._entity(eid)
        e.card_id = cid
        e.name = ""
        e.declared.update({T.ZONE: "HAND", T.ZONE_POSITION: str(i), T.CONTROLLER: "2"})
        e.tags.update({T.ZONE: "HAND", T.ZONE_POSITION: str(i), T.CONTROLLER: "2"})

    v1 = r.player_view(1)
    v2 = r.player_view(2)
    check(len(v1.hand) == 4, f"我方手牌应为 4 张（卡号空也算），实际 {len(v1.hand)}")
    check(all(not e.card_id for e in v1.hand), "我方手牌卡号确实为空（复现 bug 前提）")
    check(len(v2.hand) == 2, f"对手手牌 2 张，实际 {len(v2.hand)}")
    check(
        all(e.card_id for e in v2.hand),
        "对手已揭示的手牌卡号正确",
    )

    # 提示词里必须显式标注「卡面未知」，否则模型会误判成空手
    from hs_copilot.advisor import SYSTEM_PROMPT, build_prompt
    from hs_copilot.cards import CardDB

    db_path = Path(__file__).resolve().parents[1] / "data" / "cards_zhCN.json"
    if db_path.exists():
        db = CardDB.load(db_path)
        _, user = build_prompt(r, db, None, 1)
        check('"手牌总数": 4' in user, "提示词里包含正确的手牌总数")
        check("卡面未知" in user, "提示词里显式标注「卡面未知」")
        check(
            "手牌为空" in SYSTEM_PROMPT and "绝对不能" in SYSTEM_PROMPT,
            "系统提示词禁止回复「手牌为空」",
        )

def test_account_id_identity() -> None:
    """回归：必须靠 account_id（战网 lo）判定本机，不能靠 PlayerID 或玩家名。

    实测教训：炉石会随先后手把本机在 PlayerID 1 与 2 之间来回切换
    （日志里本机 lo=100000001 在对局1 是 PlayerID1、对局2~4 是 PlayerID2），
    因此 `my_player_id: 1` 这种固定配置必然在某些对局里指到对手。
    """
    print("\n[测试7] 用 account_id 判定本机（PlayerID 会随先后手换）")
    r = GameStateReconstructor()

    def mkplayer(eid, pid, lo):
        e = r._entity(eid)
        e.declared.update({
            T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
            "HERO_ENTITY": str(64 if pid == 1 else 66),
            T.CONTROLLER: str(pid), T.ZONE: "PLAY",
        })
        r.pid_account[pid] = lo

    # 本机 lo=100000001，这一局在 PlayerID 2
    mkplayer(2, 1, "11111111")  # 对手
    mkplayer(3, 2, "100000001")  # 本机
    r.my_account = "100000001"
    check(r.resolve_my_pid() == 2, f"本机在 PlayerID 2 时判定正确（实际 {r.resolve_my_pid()}）")

    # 换边后本机应在 PlayerID 1
    r2 = GameStateReconstructor()
    e1 = r2._entity(2); e1.declared.update({T.CARDTYPE: "PLAYER", "PLAYER_ID": "1"})
    e2 = r2._entity(3); e2.declared.update({T.CARDTYPE: "PLAYER", "PLAYER_ID": "2"})
    r2.pid_account = {1: "100000001", 2: "11111111"}
    r2.my_account = "100000001"
    check(r2.resolve_my_pid() == 1, f"本机在 PlayerID 1 时判定正确（实际 {r2.resolve_my_pid()}）")

    # 未配置 account_id 时应返回 0（宁可不判，也不要猜错）
    r3 = GameStateReconstructor()
    r3.pid_account = {1: "11111111", 2: "22222222"}
    check(r3.resolve_my_pid() == 0, "未配置 account_id 时返回 0（不猜）")

    # 配置不存在的账号也返回 0
    r4 = GameStateReconstructor()
    r4.pid_account = {1: "11111111"}
    r4.my_account = "99999999"
    check(r4.resolve_my_pid() == 0, "账号不在本局时返回 0")


def test_first_player_name_binding() -> None:
    """回归：用 FIRST_PLAYER 权威配对玩家名与 PlayerID。

    实测 bug 链（连续三轮「敌我反了」的根因）：
      1. 玩家显示名挂在「名#号」虚拟实体上，与 PlayerID 无算术关系；
      2. 名字的**出现顺序**与 PlayerID 无关——某局里 EAGhost 先出现却对应 PlayerID 2；
      3. 于是「按出现顺序绑定」会把敌我认反，且法力会同步到错误的 Player 实体。

    FIRST_PLAYER=1 同时出现在 Player 实体(2/3) 与「名#号」实体上，标记同一个人，
    据此配对才是权威的。
    """
    print("\n[测试8] FIRST_PLAYER 权威配对名字↔PlayerID（而非出现顺序）")
    from hs_copilot.log_parser import PowerLogParser

    p = PowerLogParser()
    # 先出现的是「甲」，但它其实是 PlayerID 2（与出现顺序相反）
    p.note_player_entity(2, "1")
    p.note_player_entity(3, "2")
    p._resolve_id("甲#111")
    p._resolve_id("乙#222")

    # 甲 带 FIRST_PLAYER=1，且 Player 实体 3 也带 → 甲 = PlayerID 2
    vid_a = p._named_ids["甲#111"]
    from hs_copilot.log_parser import Action

    act = Action(action="TAG_CHANGE", entity_id=vid_a)
    act.tags.append(("FIRST_PLAYER", "1"))
    p._note_first_player_tag(act)

    act2 = Action(action="TAG_CHANGE", entity_id=3)
    act2.tags.append(("FIRST_PLAYER", "1"))
    p._note_first_player_tag(act2)

    pairs = p.resolve_first_player_pairs()
    check(pairs.get("甲") == 2, f"甲 → PlayerID 2（实际 {pairs.get('甲')}）")
    check(p.slot_of_name("甲") == 2, f"slot_of_name(甲) = 2（实际 {p.slot_of_name('甲')}）")
    check(p._name_to_pid.get("甲") == 2, "权威映射已写入，优先于出现顺序")
    # 乙 未被 FIRST_PLAYER 标记 → 应落到「另一侧」= 3 - 2 = 1
    # （注意：真实实现里未被标记的名字走出现顺序兜底，此处验证兜底可用）
    slot_b = p.player_slot_of(p._named_ids["乙#222"])
    check(
        slot_b in (0, 1),
        f"乙 走兜底路径，未被误判成PlayerID 2（实际 {slot_b}）",
    )


def test_scan_player_identity() -> None:
    """回归：跨对局身份识别——lo 横跨 PlayerID + 名字参与对局数。

    实测踩过的坑（都写进断言里防止再犯）：
      1. **不能用「名字横跨 PlayerID 1/2」判定本机** —— 每局两名玩家各占一侧，
         两个名字都会横跨，该判据无法区分敌我（我据此误判过一次）。
         正确判据是 ``lo``（战网账号 ID）横跨。
      2. **不能用名字的出现顺序绑定 PlayerID** —— 顺序与 PlayerID 无关，
         必须用 ``FIRST_PLAYER`` 权威配对。
      3. 名字↔lo 的自动配对不可靠（炉石换角色时英雄名会变），
         身份判定应以 lo 为准，名字仅作显示。
    """
    print("\n[测试9] 跨对局身份识别：lo 横跨 PlayerID + 名字对局数")
    import tempfile
    from pathlib import Path

    from hs_copilot.log_parser import scan_player_identity

    # 构造 4 局：我方（我方）贯穿全部 4 局，对手每局换人（各只 1 局）
    parts = []
    # 对局 0/1/3 用同一个对手 lo=222（他也在换边）；对局 2 换个新对手
    foe_lo = ["222", "222", "2229", "222"]
    for i, (me_pid, foe_pid) in enumerate(
        [("1", "2"), ("2", "1"), ("1", "2"), ("2", "1")]
    ):
        fp_eid = "2" if me_pid == "1" else "3"
        fp_name = "我方#7" if me_pid == "1" else ("对手常客#8" if i != 2 else "对手新#8")
        parts.append(
            "PowerTaskList.DebugPrintPower() -     CREATE_GAME\n"
            f"GameState.DebugPrintPower() -     Player EntityID=2 PlayerID={me_pid} "
            "GameAccountId=[hi=1 lo=111]\n"
            + f"GameState.DebugPrintPower() -     Player EntityID=3 PlayerID={foe_pid} "
            f"GameAccountId=[hi=1 lo={foe_lo[i]}]\n"
            + f"PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity={fp_eid} "
            "tag=FIRST_PLAYER value=1\n"
            + f"PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity={fp_name} "
            "tag=FIRST_PLAYER value=1\n"
            + "PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity=我方#7 "
            "tag=TURN value=1\n"
            + f"PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity="
            f"{'对手常客#8' if i != 2 else '对手新#8'} tag=TURN value=1\n"
        )
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as tf:
        tf.write("".join(parts))
        tmp = tf.name
    try:
        ident = scan_player_identity(tmp)
        lo_map = ident["lo_map"]
        gc = ident["game_count"]
        names = ident["names"]

        check(len(lo_map) == 3, f"扫出 3 个 lo（实际 {len(lo_map)}）")
        check(
            gc.get("我方") == 4,
            f"★ 我方参与 4 局（实际 {gc.get('我方')}）—— 这是判本机的唯一可靠依据",
        )
        check(
            gc.get("对手常客") == 3,
            f"常客对手参与 3 局（实际 {gc.get('对手常客')}）",
        )
        check(
            gc.get("对手新") == 1,
            f"新对手只参与 1 局（实际 {gc.get('对手新')}）",
        )

        # ⚠️ 回归：lo 横跨两侧无法区分敌我（本机换边时对手也换边）
        spanning_lo = [lo for lo, p in lo_map.items() if len(p) > 1]
        check(
            len(spanning_lo) > 1,
            f"⚠️ 多个 lo 都横跨 PlayerID → lo 横跨不能区分敌我"
            f"（实际横跨的有 {len(spanning_lo)} 个）",
        )
        # ⚠️ 回归：名字横跨同样无区分度
        spanning_nm = [n for n, p in names.items() if len(p) > 1]
        check(
            len(spanning_nm) >= 1,
            f"⚠️ 名字横跨也无区分度（实际横跨的有 {spanning_nm}）",
        )

        # 名字↔PlayerID 由 FIRST_PLAYER 权威配对（与出现顺序无关）
        check(
            names.get("我方") == {1, 2},
            f"★ FIRST_PLAYER 配对：我方精确落在 PlayerID 1/2"
            f"（实际 {sorted(names.get('我方', ()))}）",
        )
        # 判定本机 = 参与对局数最多且>1
        top = max(gc.values())
        winner = [n for n, v in gc.items() if v == top and top > 1]
        check(winner == ["我方"], f"★ 判定本机 = {winner}（应唯一且为我方）")
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_no_unbound_closure_vars() -> None:
    """回归：闭包内赋值外层变量必须声明 nonlocal（或改用共享容器）。

    实测 bug：``gui()`` 的 ``loop()`` 里给外层的 ``my_pid`` 赋值却没写
    ``nonlocal``，Python 把它当成局部变量，于是**读取**时抛::

        UnboundLocalError: cannot access local variable 'my_pid'
        where it is not associated with a value

    浮窗直接不可用（后台线程崩溃）。现在改为共享容器 ``me["pid"]``，
    规避整类问题。本测试用 AST 静态检查所有模块。
    """
    print("\n[测试10] 闭包变量作用域（UnboundLocalError 防护）")
    import ast

    def assigned(fn):
        out = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                            out.add(n.id)
            elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                if isinstance(sub.target, ast.Name):
                    out.add(sub.target.id)
            elif isinstance(sub, (ast.For, ast.AsyncFor)):
                for n in ast.walk(sub.target):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
        return out

    def params(fn):
        a = {x.arg for x in fn.args.args + fn.args.kwonlyargs}
        if fn.args.vararg:
            a.add(fn.args.vararg.arg)
        return a

    def nonlocal_names(fn):
        d = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Nonlocal):
                d.update(sub.names)
        return d

    import pathlib

    bad: list[str] = []
    for path in sorted(
        (pathlib.Path(__file__).resolve().parents[1] / "src" / "hs_copilot").glob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for outer in ast.walk(tree):
            if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            outer_vars = assigned(outer) - params(outer)
            if not outer_vars:
                continue
            for inner in outer.body:
                if not isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                inner_vars = assigned(inner) - params(inner) - nonlocal_names(inner)
                # for/with 的目标变量本身就是局部的（Python 语义），
                # 即使外层有同名变量也不构成 UnboundLocalError。
                loop_targets: set[str] = set()
                for sub in ast.walk(inner):
                    if isinstance(sub, (ast.For, ast.AsyncFor)):
                        for n2 in ast.walk(sub.target):
                            if isinstance(n2, ast.Name):
                                loop_targets.add(n2.id)
                    elif isinstance(sub, ast.With):
                        for item in sub.items:
                            if item.optional_vars:
                                for n2 in ast.walk(item.optional_vars):
                                    if isinstance(n2, ast.Name):
                                        loop_targets.add(n2.id)
                inner_vars -= loop_targets
                # ★ 精确判据：UnboundLocalError 只在「**先读后写**」时发生。
                # 若变量在函数体内第一次出现就是赋值（如 `x = y + y`），
                # 那是普通局部变量，即使外层有同名变量也毫无关系——
                # Python 只在被判定为局部变量后才抛出该异常。
                # 所以：找出「读操作出现在同层赋值之前」的变量才告警。
                for nm in inner_vars & outer_vars:
                    first_is_store = False
                    seen_store = False
                    for sub in ast.walk(inner):
                        if isinstance(sub, ast.Assign):
                            for t in sub.targets:
                                for n2 in ast.walk(t):
                                    if (
                                        isinstance(n2, ast.Name)
                                        and n2.id == nm
                                        and isinstance(n2.ctx, ast.Store)
                                    ):
                                        seen_store = True
                                        first_is_store = True
                                        break
                        elif isinstance(sub, ast.Name) and sub.id == nm:
                            if isinstance(sub.ctx, ast.Load) and not seen_store:
                                first_is_store = False
                                break
                            if isinstance(sub.ctx, ast.Store):
                                seen_store = True
                                break
                    # first_is_store=False 表示赋值前有读取 → 会抛异常
                    if first_is_store is False:
                        bad.append(f"{path.name}: {outer.name}()->{inner.name}() 变量 {nm}")

    check(not bad, f"无「赋值外层变量却未声明 nonlocal」的问题（发现 {bad}）")


def test_idle_and_finished_state() -> None:
    """回归：对局结束后浮窗不应继续显示上一局信息。

    实测两个 bug 叠加：
    1. ``in_progress`` 只排除了中文「对局结束」，而炉石的真实终态是
       ``state=COMPLETE`` / ``step=FINAL_GAMEOVER``（原始字符串），
       于是终局被当成「进行中」，界面照旧渲染。
    2. 套牌/选人界面**不写Power.log**。工具停在上一局的终局画面，
       让人以为「没更新」。需要靠日志静默时长识别「已离开战斗」。
    """
    print("\n[测试11] 对局终态与日志停滞判定")
    from hs_copilot.overlay import BoardSnapshot

    # ① 炉石原始终态字符串必须被识别为「已结束」
    for state, step in [
        ("COMPLETE", "FINAL_GAMEOVER"),
        ("COMPLETE", ""),
        ("", "FINAL_GAMEOVER"),
        ("WAITING", ""),
        ("INVALID", ""),
    ]:
        snap = BoardSnapshot(turn=9, step=step, state=state)
        check(
            not snap.in_progress,
            f"终态 state={state!r} step={step!r} → in_progress=False",
        )

    # ② 进行中的对局不能被误判
    snap = BoardSnapshot(turn=7, step="MAIN_ACTION", state="RUNNING")
    check(snap.in_progress, "对局中 state=RUNNING → in_progress=True")

    # ③ 日志静默 => 离开战斗（套牌/选人界面不写 Power.log）
    snap = BoardSnapshot(turn=9, step="FINAL_GAMEOVER", state="COMPLETE", idle_seconds=4400.0)
    check(
        not snap.in_progress,
        f"日志静默 {snap.idle_seconds:.0f}s → in_progress=False",
    )

    # ④ 刚写入过日志（对局中）不应受idle 影响
    snap = BoardSnapshot(turn=7, step="MAIN_ACTION", state="RUNNING", idle_seconds=1.0)
    check(snap.in_progress, "日志活跃 idle=1s → 仍判定 in_progress=True")

    # ⑤ 终态 + 停更 → 文案应说明是「上一局」
    from hs_copilot.log_parser import GameStateReconstructor

    r = GameStateReconstructor()
    r.state = "COMPLETE"
    r.step = "FINAL_GAMEOVER"
    r.turn = 9
    r.idle_seconds = 4400.0
    snap = BoardSnapshot.from_recon(r, 1)
    check(
        "上一局" in snap.state and "73" in snap.state,
        f"文案含「上一局…分钟前」（实际 {snap.state!r}）",
    )


def test_turn_advice_trigger_once() -> None:
    """回归：回合建议**只在我方回合开始时**出现一次。

    实测一个回合内的 step 序列（玩家 1 视角）：
        MAIN_READY → MAIN_START_TRIGGERS → MAIN_START → MAIN_ACTION → MAIN_END → …

    两个踩过的坑：
    1. 旧版把 ``MAIN_READY`` 也算作可行动步骤 → 每回合触发**两次**建议。
       但``MAIN_READY`` 只是「准备就绪」，玩家还不能操作。
    2. 旧版把 ``MAIN_START`` 归入换牌步骤 → 每个回合都弹「起手留牌建议」。
       ``MAIN_START`` 只是回合开始的触发器，每回合都会经过。

    另外还有：解析器重建对局时同一回合会反复进出 MAIN_ACTION，
    因此还需要冷却 + 回合号双重去重。
    """
    print("\n[测试12] 回合建议只在我方回合开始时触发一次")
    from hs_copilot.watcher import ACTIONABLE_STEPS, MULLIGAN_STEPS

    # ①MAIN_READY / MAIN_START 不能算「可行动」或「换牌」
    check("MAIN_READY" not in ACTIONABLE_STEPS,
          "MAIN_READY 不算可行动步骤（否则每回合触发两次）")
    check("MAIN_START" not in ACTIONABLE_STEPS,
          "MAIN_START 不算可行动步骤")
    check("MAIN_ACTION" in ACTIONABLE_STEPS,
          "MAIN_ACTION 是唯一的可行动步骤")
    check("MAIN_START" not in MULLIGAN_STEPS,
          "MAIN_START 不算换牌（否则每回合弹起手留牌）")

    # ② 用真实日志验证「每回合恰好触发一次」
    # 不用合成日志：真实格式里 STEP 挂在 GameEntity(1)、CURRENT_PLAYER 挂在
    # 玩家名实体上，二者不在同一动作块；合成日志把两者写在同一块会掩盖时序问题。
    import glob
    import os

    real = sorted(
        glob.glob("/Applications/Hearthstone/Logs/*/Power.log"),
        key=os.path.getmtime,
    )
    if not real:
        print("     （跳过：本机无炉石日志）")
        return

    from hs_copilot.watcher import LogWatcher

    w = LogWatcher(real[-1], start_from_end=False)
    w.set_my_pid(1)
    w.turn_cooldown = 0  # 关掉时间冷却，只验证「回合号去重」
    fires = []
    for _ in range(60000):
        for t in w.poll(timeout=0):
            if t.kind == "my_turn":
                # ★ Trigger.recon 是**同一个可变对象**，读它拿到的是终局状态而非
                # 触发瞬间的状态。必须在 poll 返回前把回合号取出来。
                fires.append(int(t.turn))

    # ★ 核心断言：每个回合号最多提示一次。
    # 只看回合号，不掺对局号——「同一对局同一回合」在数据上就等于「同一回合号」，
    # 之前把 _game_no 掺进 key 导致不同回合被误判为重复（我的统计 bug）。
    dup = {r: fires.count(r) for r in set(fires) if fires.count(r) > 1}
    check(not dup, f"每个回合最多提示一次（重复回合：{dup}）")
    check(
        len(fires) == len(set(fires)),
        f"提示次数 == 唯一回合数（{len(fires)} vs {len(set(fires))}）",
    )
    # 一份 Power.log 里有多场对局，新对局回合号会从 1 重新开始；
    # watcher 本身正是用「回合号回退」识别新对局。旧断言要求整份日志
    # 全局单调，隐含「只有一局」的错误假设，改为分段内单调。
    segments: list[list[int]] = []
    cur: list[int] = []
    for t in fires:
        if cur and t < cur[-1]:
            segments.append(cur)
            cur = []
        cur.append(t)
    if cur:
        segments.append(cur)
    check(
        all(seg == sorted(seg) for seg in segments),
        f"每个对局内回合号单调递增（分段 {[ (s[0], s[-1]) for s in segments ]}）",
    )

    # ③ 另一侧同样每回合至多一次
    w2 = LogWatcher(real[-1], start_from_end=False)
    w2.set_my_pid(2)
    w2.turn_cooldown = 0
    fires2 = [
        int(t.turn)
        for _ in range(60000)
        for t in w2.poll(timeout=0)
        if t.kind == "my_turn"
    ]
    dup2 = {r: fires2.count(r) for r in set(fires2) if fires2.count(r) > 1}
    check(not dup2, f"另一侧同样每回合至多一次（重复：{dup2}）")


def test_gui_identity_resolution() -> None:
    """回归：GUI 启动时的身份判定链路（敌我颠倒的高发点）。

    实测事故：改完身份识别代码后**没有重启旧的浮窗实例**，
    旧实例仍按老逻辑显示 → 用户看到「英雄又反了」，而数据层其实是对的。
    因此除了逻辑正确，还必须保证：
    1) GUI 启动流程会先问``resolve_my_pid()``，用它覆盖配置里的 my_player_id；
    2) 同一时刻只允许一个实例（否则旧实例会继续显示错误结果）。
    """
    print("\n[测试13] GUI 身份判定链路")
    import glob
    import os

    real = sorted(
        glob.glob("/Applications/Hearthstone/Logs/*/Power.log"),
        key=os.path.getmtime,
    )
    if not real:
        print("     （跳过：本机无炉石日志）")
        return

    from hs_copilot.config import Config
    from hs_copilot.overlay import BoardSnapshot
    from hs_copilot.watcher import LogWatcher

    cfg = Config.load()
    if not cfg.player_name or cfg.player_name == "你的炉石显示名":
        print("     （跳过：未配置本机玩家名，见 config/config.local.yaml）")
        return
    check(bool(cfg.player_name), f"配置了 player_name（{cfg.player_name!r}）")

    #模拟 GUI 启动：backfill → resolve → 覆盖 my_pid
    w = LogWatcher(
        real[-1],
        start_from_end=True,
        player_name=cfg.player_name,
        account_id=cfg.account_id,
    )
    w.backfill_current_game()
    resolved = w.recon.resolve_my_pid()
    check(resolved in (1, 2), f"能判定本机 PlayerID（{resolved}）")

    my_pid = resolved or cfg.my_player_id
    snap = BoardSnapshot.from_recon(w.recon, my_pid)
    foe_pid = 2 if my_pid == 1 else 1

    # ① 名字来自**当前日志**：配置里的 player_name 是身份判据，不是显示名。
    # 玩家改过名 / 换了角色时两者本就不同，旧断言把「配置名」当成显示名，
    # 会误报成敌我颠倒。只要配置名确实出现在本局，就必须落在我方那一侧。
    shown = snap.names.get(my_pid)
    if cfg.player_name in snap.names.values():
        check(
            shown == cfg.player_name,
            f"▼我方显示的名字 = 本局出现的配置名（显示 {shown!r}）",
        )
    else:
        check(
            bool(shown) and resolved in (1, 2),
            f"配置名未出现在本局日志，但 PlayerID 判定正确且显示名非空"
            f"（显示 {shown!r}，配置 {cfg.player_name!r}，PID {resolved}）",
        )
    # ② 双方名字不能相同（相同= 没区分开，必然有一边是错的）
    check(
        snap.names.get(my_pid) != snap.names.get(foe_pid) or not snap.names.get(foe_pid),
        f"敌我名字不同（我 {snap.names.get(my_pid)!r} / 敌 {snap.names.get(foe_pid)!r}）",
    )
    # ③ 双方英雄不能是同一张卡
    check(
        snap.my_hero != snap.foe_hero or not snap.foe_hero,
        f"敌我英雄不同（我 {snap.my_hero!r} / 敌 {snap.foe_hero!r}）",
    )


def test_card_and_prompt_payload() -> None:
    """回归：卡牌按卡号精确匹配 + 提示词含完整局面 + 区域快照不复活实体。

    实测事故：只按中文名查卡表时「梦魇之王萨维斯」命中酒馆战棋英雄卡
    （无效果文本），地标被算成 0/2 随从，模型据此给的建议完全不可用。
    """
    print("\n[测试14] 卡牌描述与提示词完整性")
    import json

    from hs_copilot.advisor import build_prompt, describe_entity
    from hs_copilot.cards import CardDB
    from hs_copilot.log_parser import Action, Entity, GameStateReconstructor

    db_path = Path(__file__).resolve().parents[1] / "data" / "cards_zhCN.json"
    if not db_path.exists():
        print("     （跳过：无卡表）")
        return
    db = CardDB.load(db_path)

    # ① 同名卡必须按卡号取到正式卡，而不是战棋英雄/旧版重印
    e = Entity(id=31, card_id="EDR_856", name="梦魇之王萨维斯")
    d = describe_entity(e, db)
    check(
        d.get("卡号") == "EDR_856" and d.get("卡表匹配") == "按日志卡号精确匹配",
        f"按日志卡号精确匹配（{d.get('卡号')} / {d.get('卡表匹配')}）",
    )
    check(
        d.get("类型") == "随从" and "战吼" in d.get("卡牌效果", ""),
        f"拿到中文卡牌效果文本（{d.get('卡牌效果', '')[:24]}）",
    )

    # ② 构造含地标 / 未知手牌的局面
    r = GameStateReconstructor()
    r.turn, r.step, r.current_player = 5, "MAIN_ACTION", 1
    for pid, pe_id in ((1, 2), (2, 3)):
        pe = r._entity(pe_id)
        pe.declared.update({
            T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
            T.CONTROLLER: str(pid), T.ZONE: "PLAY",
            T.RESOURCES: "5", T.RESOURCES_USED: "0",
        })

    def mk(eid, cid, ctype, pid, zone, pos, atk=0, hp=0, name=""):
        ent = r._entity(eid)
        card = db.by_id(cid) if cid else None
        ent.card_id = cid
        ent.name = name or (card.name if card else cid)
        ent.tags.update({
            T.CARDTYPE: ctype, T.CONTROLLER: str(pid), T.ZONE: zone,
            T.ZONE_POSITION: str(pos), T.ATK: str(atk), T.HEALTH: str(hp),
        })
        return ent

    mk(10, "EDR_102", "MINION", 1, "PLAY", 1, 5, 4)
    mk(11, "JAIL_987", "LOCATION", 1, "PLAY", 2, 0, 2)
    mk(12, "TLC_888", "MINION", 1, "HAND", 1, 3, 3)
    mk(13, "", "MINION", 1, "HAND", 2, 0, 0,
       name="UNKNOWN ENTITY [cardType=INVALID]")
    mk(20, "CORE_REV_990", "LOCATION", 2, "PLAY", 1, 0, 2)
    mk(21, "CATA_556", "MINION", 2, "PLAY", 1, 1, 2)

    v_me, v_foe = r.player_view(1), r.player_view(2)
    check(len(v_me.board) == 1 and len(v_me.locations) == 1,
          "地标不计入随从，单列 locations")
    check(len(v_foe.board) == 1 and len(v_foe.locations) == 1,
          "对手地标同样单列")
    check(not v_me.deck_count_known, "牌库无实体时标注为未知而不是 0")

    _, user = build_prompt(r, db, None, 1)
    p = json.loads(user)
    check(len(p["我方"]["场上地标"]) == 1 and len(p["对手"]["场上地标"]) == 1,
          "提示词包含双方场上地标（含耐久与冷却）")
    check(all(k in p["我方"] for k in ("英雄技能", "武器", "奥秘", "场面随从")),
          "提示词包含英雄技能 / 武器 / 奥秘 / 随从")
    check("近期战况" in p and "数据说明" in p and "对局状态" in p["局面"],
          "提示词包含近期战况 / 数据说明 / 对局状态")
    check("未知" in str(p["我方"]["牌库剩余"]),
          "牌库未知时输出「未知」而非 0")
    check(all(x.get("卡表匹配") == "按日志卡号精确匹配" and x.get("卡牌效果")
               for x in p["我方"]["场面随从"]),
          "场面随从都带精确匹配的中文效果文本")
    hand = p["我方"]["手牌"]
    check(len(hand) == 2 and any(x.get("卡面未知") for x in hand),
          "卡面未知的手牌仍保留并显式标注")
    check(all("编号" in x for x in hand), "手牌都有从 1 起的编号")
    check(r.board_state(1)["me"] != r.board_state(2)["me"],
          "board_state 按 my_pid 切换敌我")

    # ③ 旧 zone=PLAY 快照不得把已入墓地的实体复活
    r2 = GameStateReconstructor()
    snap = {"id": "99", "zone": "PLAY", "zonePos": "4",
            "player": "2", "entityName": "测试随从"}
    r2.apply(Action(action="TAG_CHANGE", entity_id=99,
                     entity_fields=dict(snap), tags=[("DAMAGE", "0")]))
    check(r2.entities[99].zone == "PLAY", "新实体用快照初始化区域")
    r2.apply(Action(action="TAG_CHANGE", entity_id=99,
                     entity_fields=dict(snap), tags=[(T.ZONE, "GRAVEYARD")]))
    check(r2.entities[99].zone == "GRAVEYARD", "权威 tag=ZONE 生效")
    r2.apply(Action(action="TAG_CHANGE", entity_id=99,
                     entity_fields=dict(snap), tags=[("DAMAGE", "0")]))
    check(r2.entities[99].zone == "GRAVEYARD",
          "旧 zone=PLAY 快照不会复活已入墓地的随从")


def test_follow_latest_log_dir() -> None:
    """回归：主界面期间启动、开局后新建日志目录时必须自动切过去。

    实测事故：炉石每次启动客户端新建 Logs/Hearthstone_<时间戳>/，
    Power.log 只在进入对局后才写。军师一直挂在旧目录上，
    用户看到「对局已开始但界面没有任何信息」。
    """
    print("\n[测试15] 自动切换到新对局日志目录")
    import os
    import tempfile
    import time as _time

    import hs_copilot.config as cfgmod
    from hs_copilot.watcher import LogWatcher

    line = "D 23:20:00.0000000 PowerTaskList.DebugPrintPower() - CREATE_GAME\n"
    with tempfile.TemporaryDirectory() as td:
        old = Path(td) / "old.log"
        new = Path(td) / "new.log"
        old.write_text(line, encoding="utf-8")
        new.write_text(line, encoding="utf-8")
        now = _time.time()
        os.utime(old, (now - 600, now - 600))
        os.utime(new, (now, now))

        orig = cfgmod.find_latest_power_log
        cfgmod.find_latest_power_log = lambda: new
        try:
            w = LogWatcher(old, start_from_end=False)
            w.follow_latest = True
            w.latest_check_interval = 0.0
            w.backfill_current_game()  # 主界面期间启动：挂的是旧日志
            check(w.path == old, "启动时挂在旧日志上")
            w.poll(timeout=0)  # 开局后新目录出现 Power.log
            check(w.path == new, f"已自动切到新日志（{w.path.name}）")
            check(
                hasattr(w, "_fh") and w._inode == new.stat().st_ino,
                "新日志句柄已打开并可增量跟读",
            )
            check(not w._advised_turns and w._prev_turn == 0,
                  "触发去重状态已重置，不会漏掉新对局的回合建议")
            w.close()
        finally:
            cfgmod.find_latest_power_log = orig


def test_first_player_other_side_and_mana() -> None:
    """回归：FIRST_PLAYER 只标记先手，后手名字也必须绑定 PlayerID。

    实测事故：后手（本机）名字的 slot 返回 0，挂在「名#号」实体上的
    RESOURCES 从未同步到其 Player 实体，浮窗显示「我方 法力 0/0」，
    模型据此认为一费都没有，建议完全不可用。
    """
    print("\n[测试16] 后手玩家名字绑定与法力同步")
    import glob
    import os

    real = sorted(
        glob.glob("/Applications/Hearthstone/Logs/*/Power.log"),
        key=os.path.getmtime,
    )
    if not real:
        print("     （跳过：本机无炉石日志）")
        return
    from hs_copilot.log_parser import parse_log_file

    r = parse_log_file(real[-1], player_name="玩家甲", account_id="100000001")
    if len(r.player_names) < 2 or r.step in ("FINAL_GAMEOVER", "GAME_OVER") \
            or r.state == "COMPLETE":
        print("     （跳过：最新日志不是进行中的双方对局）")
        return
    v1, v2 = r.player_view(1), r.player_view(2)
    check(len(r._pname_slot) >= 2,
          f"两个名字都绑定到 PlayerID（{r._pname_slot}）")
    check(v1.mana > 0 and v2.mana > 0,
          f"双方法力都能读到（{v1.mana}/{v1.mana_max} vs {v2.mana}/{v2.mana_max}）")


def test_current_player_and_turn_tags() -> None:
    """回归：CURRENT_PLAYER 是 0/1 布尔标记，TURN 有玩家级副本。

    实测事故：旧版把 CURRENT_PLAYER 的 0/1 当成玩家编号，恒为 1；
    本机是 PlayerID 2 时「轮到我方」永远不成立，整局一次建议都没给。
    玩家级 TURN（从 1 重新数）又会覆盖全局回合号，破坏回合去重。
    """
    print("\n[测试17] 行动方识别与回合触发")
    import tempfile

    from hs_copilot.log_parser import (
        Action,
        GameStateReconstructor,
        PowerLogParser,
        T,
    )
    from hs_copilot.watcher import LogWatcher

    r = GameStateReconstructor()
    p = PowerLogParser()
    r._slot_resolver = p.player_slot_of
    r._pname_resolver = p.player_name_of
    vid_me = p._resolve_id("我#1")
    vid_foe = p._resolve_id("敌#2")
    p._name_to_pid.update({"我": 2, "敌": 1})

    r.apply(Action(action="TAG_CHANGE", entity_id=1, tags=[(T.TURN, "9")]))
    check(r.turn == 9, "全局 TURN 生效")
    r.apply(Action(action="TAG_CHANGE", entity_id=vid_me, tags=[(T.TURN, "1")]))
    check(r.turn == 9, "玩家级 TURN 不会覆盖全局回合号")
    r.apply(Action(action="TAG_CHANGE", entity_id=vid_foe,
                   tags=[("CURRENT_PLAYER", "0")]))
    r.apply(Action(action="TAG_CHANGE", entity_id=vid_me,
                   tags=[("CURRENT_PLAYER", "1")]))
    check(r.current_player == 2, "布尔 CURRENT_PLAYER 正确识别我方 PlayerID 2")

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "Power.log"
        f.write_text("", encoding="utf-8")
        w = LogWatcher(f, start_from_end=False)
        w.set_my_pid(2)
        w.recon.current_player = 2
        w.recon.step = "MAIN_ACTION"
        w.recon.turn = 9
        kinds = [t.kind for t in w.poll(timeout=0)]
        check("my_turn" in kinds, f"启动已在我方回合时补触发（{kinds}）")
        w.close()


def test_summoning_sickness() -> None:
    """回归：刚召唤（EXHAUSTED=1）的随从本回合不能攻击，伤害不得计入斩杀。

    实测事故：客户端上场时写 ``tag=EXHAUSTED value=1``，但旧版只输出含混的
    「本回合已行动/无法攻击」且无字段时模型默认能打；detect_lethal 更把所有
    随从攻击力一律累加，虚报斩杀后模型按「有斩杀必须优先」给出让刚上场
    随从打脸的方案。
    """
    print("\n[测试18] 召唤失调 / 攻击资格与斩杀计算")
    import json

    from hs_copilot.advisor import (
        SYSTEM_PROMPT, Advice, LLMClient, build_prompt,
        detect_lethal, estimate_board_value,
    )
    from hs_copilot.cards import CardDB

    r = GameStateReconstructor()
    r.turn, r.step, r.current_player, r.state = 8, "MAIN_ACTION", 1, "RUNNING"
    for pid, pe_id, hero_id in ((1, 2, 64), (2, 3, 66)):
        pe = r._entity(pe_id)
        pe.declared.update({
            T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
            T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.RESOURCES: "7",
        })
        pe.tags[T.RESOURCES] = "7"
        h = r._entity(hero_id)
        h.card_id = "HERO_02a" if pid == 1 else "HERO_05"
        h.declared.update({
            T.CARDTYPE: "HERO", T.CONTROLLER: str(pid),
            T.ZONE: "PLAY", T.HEALTH: "30", T.ATK: "0",
        })
    # 对手英雄 5 血：可打脸伤害恰好 5，刚好构成一次斩杀
    r.entities[66].declared[T.HEALTH] = "5"

    def mk(eid, cid, ctrl, pos, atk, hp, **tags):
        e = r._entity(eid)
        e.card_id = cid
        e.name = cid
        e.declared.update({
            T.CARDTYPE: "MINION", T.CONTROLLER: str(ctrl),
            T.ZONE: "PLAY", T.ZONE_POSITION: str(pos),
        })
        e.tags.update({
            T.ZONE: "PLAY", T.ZONE_POSITION: str(pos),
            T.CONTROLLER: str(ctrl), T.ATK: str(atk), T.HEALTH: str(hp),
        })
        e.tags.update({k: str(v) for k, v in tags.items()})
        return e

    sick = mk(101, "TEST_SICK", 1, 1, 3, 3, EXHAUSTED=1, NUM_TURNS_IN_PLAY=1)
    ok = mk(102, "TEST_OK", 1, 2, 2, 2, EXHAUSTED=0)
    used = mk(103, "TEST_USED", 1, 3, 4, 4,
              EXHAUSTED=1, NUM_ATTACKS_THIS_TURN=1, NUM_TURNS_IN_PLAY=3)
    frozen = mk(104, "TEST_FROZEN", 1, 4, 5, 5, FROZEN=1)
    rush = mk(105, "TEST_RUSH", 1, 5, 2, 2,
              EXHAUSTED=1, RUSH=1, NUM_TURNS_IN_PLAY=1)
    charge = mk(106, "TEST_CHARGE", 1, 6, 1, 1,
                EXHAUSTED=1, CHARGE=1, NUM_TURNS_IN_PLAY=1)
    wind = mk(107, "TEST_WINDFURY", 1, 7, 2, 2,
              WINDFURY=1, NUM_ATTACKS_THIS_TURN=1)
    foe_sick = mk(201, "TEST_FOE_SICK", 2, 1, 6, 6,
                  EXHAUSTED=1, NUM_TURNS_IN_PLAY=1, TAUNT=1)

    check(not sick.attack_status()["can_attack"] and sick.attack_status()["just_summoned"],
          "刚召唤随从（EXHAUSTED=1）判定为召唤失调、不可攻击")
    check(ok.attack_status()["can_attack_hero"], "正常随从可以打脸")
    check(not used.attack_status()["can_attack"], "本回合已攻击的随从不能再打")
    check(not frozen.attack_status()["can_attack"], "被冻结的随从不能攻击")
    st_rush = rush.attack_status()
    check(st_rush["can_attack"] and not st_rush["can_attack_hero"],
          "突袭可攻击随从但不能打脸")
    check(charge.attack_status()["can_attack_hero"], "冲锋刚上场也能打脸")
    check(wind.attack_status()["can_attack"], "风怒打过一次仍可再攻击")
    check(foe_sick.attack_status()["ready_next_turn"],
          "对手刚召唤随从下个对手回合即可攻击（不能无视）")

    lethal = detect_lethal(r, 1)
    check(lethal is not None and lethal["available"] == 5,
          f"斩杀只统计可打脸伤害（实际 {lethal and lethal['available']}，应为 5）")
    check(bool(lethal) and any("TEST_SICK" in x for x in lethal["excluded_minions"]),
          "刚召唤随从列入「未计入斩杀」名单")
    check("can_attack" in sick.to_brief() and sick.to_brief()["just_summoned"],
          "board --json 的随从条目也带攻击资格")
    check(estimate_board_value(r.player_view(1))["ready_atk"] == 7,
          "场面价值区分账面攻击力(19)与本回合可用攻击力(7)")
    check(estimate_board_value(r.player_view(2), next_turn=True)["ready_atk"] == 6,
          "对手场面按「下个对手回合」评估威胁（6 攻而非 0）")

    _, user = build_prompt(r, CardDB([]), None, 1)
    p = json.loads(user)
    mine = p["我方"]["场面随从"]
    check(all("本回合能否攻击" in x and "刚召唤" in x and "能否打脸" in x for x in mine),
          "我方每个随从都显式标注攻击资格（无字段时模型会默认能打）")
    mine_by_name = {x["名称"]: x for x in mine}
    check(mine_by_name["TEST_SICK"]["本回合能否攻击"] == "否"
          and mine_by_name["TEST_SICK"]["刚召唤"] == "是",
          "提示词里刚召唤随从明确标为「否」")
    foe_entry = p["对手"]["场面随从"][0]
    check("本回合能否攻击" not in foe_entry and foe_entry["对手下回合能否攻击"] == "是",
          "对手随从按「下回合威胁」标注，不误写成不能攻击")
    ready = p["【系统预判】本回合可用攻击"]
    check({x["名称"] for x in ready["本回合不能攻击"]}
          == {"TEST_SICK", "TEST_USED", "TEST_FROZEN"},
          "可用攻击汇总只列真正不能打的随从")
    check(ready["可打脸伤害（未计对手嘲讽）"] == 5,
          "系统预判的可打脸伤害为 5，不含刚召唤/已攻击/冻结的随从")
    check(all(k in SYSTEM_PROMPT for k in ("本回合能否攻击", "刚召唤", "仅能攻击随从")),
          "系统提示词含攻击资格硬规则")

    # 兜底：模型仍然违规时，必须拦下并撤销虚报的斩杀
    r.entities[66].declared[T.HEALTH] = "30"  # 提高血量，实际已无斩杀
    bad = Advice(lethal=True, summary="可以斩杀",
                 plan=["让 TEST_SICK 攻击英雄完成斩杀"])
    LLMClient._validate_attack(bad, r, 1)
    check(not bad.lethal and any("不能" in w for w in bad.warnings),
          "模型让刚召唤随从攻击 / 虚报斩杀时被返回前校验拦截")


def test_advice_only_on_my_turn_start() -> None:
    """回归：只在我方回合**开始**给一次建议，对手回合 / 同回合重复进出都不给。

    实测事故：`gui` 的「启动即分析」无条件请求模型，浮窗在对手回合也显示
    「第 2 回合建议」，模型自己回复「非我方行动回合」。另外 ``CURRENT_PLAYER
    value=0`` 原本被直接忽略，一旦配对的 ``value=1`` 因槽位未就绪解析失败，
    current_player 会停在上一回合的旧值，把对手回合误判成我方回合。
    """
    print("\n[测试19] 建议只在自己的回合开始触发")
    import tempfile

    from hs_copilot.log_parser import Action, GameStateReconstructor
    from hs_copilot.watcher import LogWatcher

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "Power.log"
        f.write_text("", encoding="utf-8")
        w = LogWatcher(f, start_from_end=False)
        w.set_my_pid(1)
        r = w.recon
        r._slot_resolver = lambda eid: {-3: 1, -4: 2}.get(eid, 0)
        r.turn, r.state, r.current_player, r.step = 1, "RUNNING", 1, ""

        def fire(*actions):
            out = []
            for a in actions:
                out.extend(w._apply(a))
            return [t for t in out if t.kind == "my_turn"]

        def tag(name, value, eid=1):
            return Action(action="TAG_CHANGE", entity_id=eid, tags=[(name, value)])

        # ① 对手回合（turn=2，行动方 PID2）进入 MAIN_ACTION —— 不得触发
        r.step = "MAIN_END"
        got = fire(
            tag("TURN", "2"),
            tag("CURRENT_PLAYER", "1", -4),
            tag("STEP", "MAIN_END"),
            tag("STEP", "MAIN_ACTION"),
        )
        check(not got, "对手回合的 MAIN_ACTION 不生成建议")

        # ② value=0 兜底：非行动方是 PID2，则行动方必然是 PID1
        r.current_player = 2
        r.apply(tag("CURRENT_PLAYER", "0", -4))
        check(r.current_player == 1, "CURRENT_PLAYER=0 也能把行动方切回我方")

        # ③ 我方回合（turn=3）开始 —— 恰好触发一次，且快照字段正确
        r.step = "MAIN_END"
        got = fire(tag("TURN", "3"), tag("STEP", "MAIN_ACTION"))
        check(len(got) == 1, f"我方回合开始触发 1 次（实际 {len(got)}）")
        if got:
            check(got[0].turn == 3 and got[0].current_player == 1
                  and got[0].step == "MAIN_ACTION",
                  "Trigger 带触发瞬间的回合/行动方/step 快照")

        # ④ 同一回合内再次进出 MAIN_ACTION —— 不得重复
        r.step = "MAIN_END"
        got = fire(tag("STEP", "MAIN_END"), tag("STEP", "MAIN_ACTION"))
        check(not got, "同一我方回合内重复进入 MAIN_ACTION 不重复给建议")

        # ⑤ 下一个我方回合（turn=5）—— 再触发一次
        r.step = "MAIN_END"
        got = fire(tag("TURN", "5"), tag("STEP", "MAIN_ACTION"))
        check(len(got) == 1 and got[0].turn == 5, "下一个我方回合开始再触发一次")
        w.close()

    # ⑥ 统一的「我方行动阶段」判据
    r2 = GameStateReconstructor()
    r2.turn, r2.step, r2.state, r2.current_player = 5, "MAIN_ACTION", "RUNNING", 1
    check(r2.is_my_action_phase(1), "轮到我方 + MAIN_ACTION = 可给建议")
    check(not r2.is_my_action_phase(2), "行动方是对手时不可给建议")
    r2.step = "MAIN_END"
    check(not r2.is_my_action_phase(1), "MAIN_END 不可给建议")
    r2.step, r2.state = "MAIN_ACTION", "COMPLETE"
    check(not r2.is_my_action_phase(1), "对局已结束不可给建议")
    r2.state, r2.idle_seconds = "RUNNING", 120.0
    check(not r2.is_my_action_phase(1), "日志长时间停更不可给建议")

    # ⑦ 虚拟名字实体（负 id）上的布尔 1 不能被当成 PlayerID
    r3 = GameStateReconstructor()
    r3._slot_resolver = lambda eid: 0
    r3.current_player = 2
    r3.apply(Action(action="TAG_CHANGE", entity_id=-9,
                    tags=[("CURRENT_PLAYER", "1")]))
    check(r3.current_player == 2,
          "槽位解析失败时不把布尔标记 1 当成 PlayerID 1")

    # ⑧ 名字判据：日志明确写了「谁在行动」，优先于可能残留的 PlayerID 槽位
    r4 = GameStateReconstructor()
    r4.turn, r4.step, r4.state, r4.current_player = 8, "MAIN_ACTION", "RUNNING", 1
    r4.player_names = {1: "我", 2: "对手"}
    r4.my_player_name = "我"
    r4.current_player_name = "对手"
    check(not r4.is_my_action_phase(1),
          "行动方名字是对手时，即使编号残留也不给建议")
    r4.current_player_name = "我"
    check(r4.is_my_action_phase(1), "行动方名字是我方时允许给建议")

    # ⑧b 玩家级 TURN 的名字最优先（每回合必写，比 CURRENT_PLAYER 残留可靠）
    r5 = GameStateReconstructor()
    r5.turn, r5.step, r5.state, r5.current_player = 9, "MAIN_ACTION", "RUNNING", 1
    r5.player_names = {1: "我", 2: "对手"}
    r5.my_player_name = "我"
    r5.current_player_name = "我"       # 布尔标记残留成我方
    r5.turn_owner_name = "对手"         # 但玩家级 TURN 明确是对手回合
    check(not r5.is_my_action_phase(1),
          "玩家级 TURN 名字优先，对手回合即使布尔标记残留也不给建议")
    r5.turn_owner_name = "我"
    check(r5.is_my_action_phase(1), "玩家级 TURN 名字是我方时允许给建议")

    # ⑨ 硬约束：两个建议之间全局 TURN 至少差 2（不会连续回合都给）
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "Power.log"
        f.write_text("", encoding="utf-8")
        w = LogWatcher(f, start_from_end=False)
        w.set_my_pid(1)
        r = w.recon
        r._slot_resolver = lambda eid: {-3: 1, -4: 2}.get(eid, 0)
        r.turn, r.state, r.current_player, r.step = 1, "RUNNING", 1, ""

        def tag2(name, value, eid=1):
            return Action(action="TAG_CHANGE", entity_id=eid, tags=[(name, value)])

        def fire2(*actions):
            out = []
            for a in actions:
                out.extend(w._apply(a))
            return [t for t in out if t.kind == "my_turn"]

        r.step = "MAIN_END"
        first = fire2(tag2("TURN", "3"), tag2("STEP", "MAIN_ACTION"))
        r.step = "MAIN_END"
        second = fire2(tag2("TURN", "4"), tag2("STEP", "MAIN_ACTION"))
        check(len(first) == 1 and not second,
              "相邻全局回合（差 1，必含对手回合）不会连续给建议")
        w.close()


def test_health_not_ready_guard() -> None:
    """回归：开局血量尚未写入日志时，禁止模型断言「对局已结束 / 只能空过」。

    实测事故：新局开局瞬间 ``hero_hp`` 读到 0（英雄实体 / HEALTH 还没写进
    Power.log），但 ``step=MAIN_ACTION``、``state=RUNNING``，模型据此回复
    「对手生命为 0，对局已结束，无需再出牌」，而下一秒状态条显示对手 28 血。
    """
    print("\n[测试20] 血量未就绪时禁止断言对局结束")
    import json

    from hs_copilot.advisor import SYSTEM_PROMPT, Advice, LLMClient, build_prompt
    from hs_copilot.cards import CardDB

    def make(with_hero: bool) -> GameStateReconstructor:
        r = GameStateReconstructor()
        r.turn, r.step, r.state, r.current_player = 1, "MAIN_ACTION", "RUNNING", 1
        for pid, pe_id, hid in ((1, 2, 64), (2, 3, 66)):
            pe = r._entity(pe_id)
            pe.declared.update({
                T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
                T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.RESOURCES: "3",
            })
            pe.tags[T.RESOURCES] = "3"
            h = r._entity(hid)
            h.card_id = "HERO_02a" if pid == 1 else "HERO_05"
            d = {T.CARDTYPE: "HERO", T.CONTROLLER: str(pid),
                 T.ZONE: "PLAY", T.ATK: "0"}
            if with_hero:
                d[T.HEALTH] = "30"
            h.declared.update(d)
        return r

    db = CardDB([])
    r = make(False)
    _, user = build_prompt(r, db, None, 1)
    p = json.loads(user)
    check(list(p)[0] == "【数据告警】血量未就绪",
          "告警放在 payload 第一个字段（模型第一眼就能看到）")
    check("未就绪" in p["局面"]["血量数据"], "局面显式标注血量数据未就绪")
    check("未知" in str(p["我方"]["生命"]) and "未知" in str(p["对手"]["生命"]),
          "进行中读到 0 血时把「生命」遮罩成「未知」")
    check("血量数据" in p["数据说明"], "数据说明补充血量未知的含义")
    check(all(k in SYSTEM_PROMPT for k in ("血量未就绪", "禁止断言", "按未知处理")),
          "系统提示词含规则 18 禁止断言对局结束")

    bad = Advice(lethal=True,
                 summary="对手生命为0，对局已结束，无需再出牌",
                 plan=["只能空过"])
    LLMClient._validate_readiness(bad, r, 1)
    check(not bad.lethal, "兜底撤销模型虚报的斩杀标记")
    check("血量数据尚未刷新" in bad.summary, "兜底改写「对局已结束」的结论")
    check(bool(bad.plan) and all("空过" not in s for s in bad.plan),
          "兜底移除「只能空过」并补一条可执行建议")
    check(bool(bad.warnings) and "血量尚未" in bad.warnings[0],
          "兜底把血量未就绪警告置顶")

    # 血量就绪时不得误伤
    r2 = make(True)
    _, u2 = build_prompt(r2, db, None, 1)
    p2 = json.loads(u2)
    check(p2["局面"]["血量数据"] == "已就绪"
          and "【数据告警】血量未就绪" not in p2,
          "血量就绪时不加告警")
    check(p2["我方"]["生命"] == 30 and p2["对手"]["生命"] == 30,
          "血量就绪时保留真实数值")
    good = Advice(summary="可以正常出牌", plan=["打出测试牌（2费）"])
    LLMClient._validate_readiness(good, r2, 1)
    check(good.summary == "可以正常出牌"
          and good.plan == ["打出测试牌（2费）"] and not good.warnings,
          "正常局面不被兜底误改")

    # 终局快照按复盘处理，不启用「进行中」的血量告警
    r3 = make(False)
    r3.state = "COMPLETE"
    _, u3 = build_prompt(r3, db, None, 1)
    check("【数据告警】血量未就绪" not in json.loads(u3),
          "终局快照不加血量未就绪告警")


def test_game_plan() -> None:
    """[测试21] 套牌级整局规划：自动识牌、画像、跨回合复用、提示词注入。"""
    import json
    import tempfile

    from hs_copilot.advisor import (  # noqa: E402
        SYSTEM_PROMPT,
        Advice,
        LLMClient,
        build_prompt,
    )
    from hs_copilot.cards import CardDB  # noqa: E402
    from hs_copilot.config import Config  # noqa: E402
    from hs_copilot.deck import (  # noqa: E402
        DeckTracker,
        find_active_deck,
    )
    from hs_copilot.planner import (  # noqa: E402
        DeckProfile,
        GamePlan,
        PlanEngine,
        fallback_plan,
        my_turn_of,
    )

    print("\n[测试21] 套牌级整局规划")

    # ① Decks.log 只认最后一次「Finding Game With Deck」，不受卡组列表噪声干扰
    code = (
        "AAECAfrwBhSqigT9nwSooATOngae+QbEgQeilgfknQfQvwfTwAf2yQeC1Aeb1Afa3wfv4wf87"
        "gen9wfm/Qf//QekgQgFr58EzrsHj74Hw8AHycAHAAA="
    )
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "Power.log").write_text("", encoding="utf-8")
        (d / "Decks.log").write_text(
            "I 23:21:05.9317150 Deck Contents Received:\n"
            "I 23:21:05.9317150 ### 旧卡组\n"
            "I 23:21:05.9317150 # Deck ID: 111\n"
            "I 23:21:05.9317150 AAEBAQcBrYoDAAAA\n"
            "I 00:54:47.7766860 Finding Game With Deck:\n"
            "I 00:54:47.7766860 ### 超级进化萨\n"
            "I 00:54:47.7766860 # Deck ID: 9465884738\n"
            f"I 00:54:47.7766860 {code}\n",
            encoding="utf-8",
        )
        active = find_active_deck(d / "Power.log")
    check(active is not None and active.name == "超级进化萨",
          "Decks.log 自动识别当前套牌名")
    check(active is not None and active.deck_id == "9465884738"
          and active.code == code, "取最后一次 Finding 块而不是卡组列表")
    check(find_active_deck(Path("/nonexistent/Power.log")) is None,
          "Decks.log 缺失时返回 None")
    # 同一客户端连续多局：Decks.log 追加新块后必须能发现换牌
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "Power.log").write_text("", encoding="utf-8")
        dl = d / "Decks.log"
        dl.write_text(
            "I 00:10:00.0 Finding Game With Deck:\n"
            "I 00:10:00.0 ### 甲套牌\n"
            "I 00:10:00.0 # Deck ID: 1001\n"
            "I 00:10:00.0 AAEBAQcBrYoDAAAA\n", encoding="utf-8")
        first = find_active_deck(d / "Power.log")
        with dl.open("a", encoding="utf-8") as f:
            f.write(
                "I 00:30:00.0 Finding Game With Deck:\n"
                "I 00:30:00.0 ### 乙套牌\n"
                "I 00:30:00.0 # Deck ID: 1002\n"
                "I 00:30:00.0 AAEBAQcBrYoDAAAA\n")
        second = find_active_deck(d / "Power.log")
    check(first is not None and second is not None
          and second.name == "乙套牌" and second.key != first.key,
          "同一日志目录内换卡组能识别出新的 Deck ID")

    cfg = Config.load()
    db = CardDB.load(cfg.cards_path)
    deck = DeckTracker.from_deckstring(code, db, name="超级进化萨")
    deck.deck_id = "9465884738"
    check(deck.size >= 25 and len(deck.total) >= 20, "套牌码解出完整卡表")

    # ② 套牌画像：完整中文卡表 + 战术角色
    profile = DeckProfile.build(deck, db)
    check(len(profile.cards) == len(deck.total), "画像覆盖套牌全部卡种")
    check(all("效果" in c and "费用" in c for c in profile.cards),
          "每张卡都带中文效果文本与费用")
    check("任务与任务线" in profile.roles, "识别任务体系")
    check("解场与去除" in profile.roles, "识别解场牌")
    check("完整卡表" in profile.to_prompt_dict(), "慢层能拿到完整卡表")

    # ③ 兜底计划与提示词注入
    fp = fallback_plan(profile, 1)
    check(len(fp.stages) == 3 and bool(fp.win_condition), "兜底计划有三阶段与致胜条件")
    check(bool(fp.keep_cards), "兜底计划标记了必须保留的核心卡")
    pd = fp.to_prompt_dict(2)
    check(bool(pd["当前阶段"]["目标"]) and bool(pd["使用规则"]),
          "计划摘要含当前阶段目标与使用规则")
    check(my_turn_of(14) == 7, "全局回合换算我方回合")

    r = GameStateReconstructor()
    r.turn, r.step, r.state = 3, "MAIN_ACTION", "RUNNING"
    _, user = build_prompt(
        r, db, deck, 1, plan=fp, profile=profile,
        plan_progress={"我方回合": 2, "当前阶段目标": "推进任务"},
    )
    pay = json.loads(user)
    keys = list(pay.keys())
    check("【整局计划】" in pay and "【计划进度】" in pay,
          "回合提示词注入整局计划与计划进度")
    check(keys.index("【整局计划】") < keys.index("局面"),
          "整局计划排在局面之前，模型先看战略")
    check(pay["我的套牌"]["说明"].startswith("完整卡表已在整局计划阶段分析"),
          "回合层只带精简套牌，不重复塞 30 张卡表")
    check(all(k in SYSTEM_PROMPT for k in ("整局计划优先", "资源纪律", "plan_step")),
          "系统提示词新增规则 19/20/21")

    # ④ 低频生成 + 跨回合稳定（用假客户端，不联网）
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat_json(self, system: str, user: str, max_tokens=None) -> str:
            self.calls += 1
            return json.dumps({
                "archetype": "中速兆示任务萨",
                "win_condition": "完成任务后铺场进化，减费死亡之翼终结",
                "stages": [
                    {"my_turn_from": 1, "my_turn_to": 3, "goal": "累计使用 2 个不同类型随从",
                     "actions": ["优先低费随从"]},
                    {"my_turn_from": 4, "my_turn_to": 7, "goal": "完成任务并启动升腾",
                     "actions": ["保留妖术"]},
                    {"my_turn_from": 8, "my_turn_to": 99, "goal": "死亡之翼或奥拉基尔终结",
                     "actions": []},
                ],
                "core_cards": [
                    {"name": "妖术", "role": "稀缺单体解", "keep": True},
                    {"name": "升腾", "role": "场面爆发", "keep": True},
                ],
                "resource_rules": ["妖术非关键威胁不使用"],
                "lethal_setup": {"components": ["灭世者死亡之翼"]},
                "next_milestone": "推进任务计数",
            }, ensure_ascii=False)

    fake = FakeClient()
    eng = PlanEngine(fake, db, enabled=True)
    r.turn = 3
    plan_a, _ = eng.update(r, deck, 1)
    check(fake.calls == 1 and plan_a is not None and plan_a.source == "llm",
          "每局只生成一次整局计划")
    r.turn = 5
    plan_b, _ = eng.update(r, deck, 1)
    check(plan_b is plan_a and fake.calls == 1,
          "同局跨回合复用同一计划，回合建议不额外请求")
    r.turn = 1  # 回合号回退 = 新对局
    plan_c, _ = eng.update(r, deck, 1)
    check(plan_c is not plan_a, "新对局回合号回退时重新规划")
    check(eng.update(r, DeckTracker(card_db=db), 1)[0] is None,
          "未识别到套牌时优雅降级，不崩")

    # ⑤ 资源纪律提醒：延后使用不能误报，直接打出要提醒
    gp = GamePlan(
        archetype="测试萨", win_condition="测试", source="llm",
        stages=[{"my_turn_from": 1, "my_turn_to": 99, "goal": "推进任务", "actions": []}],
        core_cards=[{"name": "升腾", "role": "核心", "keep": True}],
    )
    adv_keep = Advice(summary="保留", plan=["保留法力，等下回合配合升腾使用"])
    LLMClient._apply_plan(adv_keep, gp, r)
    check(not any("需保留" in w for w in adv_keep.warnings),
          "「留到下回合用」不算动用核心卡，不误报")
    check("【整局】" in adv_keep.to_text(), "建议文本带整局目标")
    adv_use = Advice(summary="打出", plan=["打出升腾（4费）"])
    LLMClient._apply_plan(adv_use, gp, r)
    check(any("需保留" in w for w in adv_use.warnings),
          "无理由打出核心卡时给出资源纪律提醒")


def test_unknown_hand_guard() -> None:
    """回归：日志尚未揭示手牌卡号时，不得把「实体N」丢给模型瞎猜。

    实测事故：起手 / 新抽的手牌先是 ``UNKNOWN ENTITY ... CardID=``，真实卡号由
    随后几百毫秒内的 ``SHOW_ENTITY`` 补齐。旧版 payload 写「实体14 / 卡面未知」，
    规则 6 还要求模型「按有 N 张未知手牌给建议」，于是产出
    「#1 实体14：若为低费随从/解牌可本回合打出」这种毫无信息量的建议。
    现在必须是：可读占位 + 套牌候选池 + 禁止猜测的提示词 + 返回前硬校验。
    """
    print("\n[测试22] 未揭示手牌不再产生猜测式建议")
    import json
    from hs_copilot.advisor import (
        SYSTEM_PROMPT,
        Advice,
        LLMClient,
        build_prompt,
        describe_entity,
    )
    from hs_copilot.cards import CardDB
    from hs_copilot.config import Config
    from hs_copilot.deck import DeckTracker

    code = (
        "AAECAfrwBhSqigT9nwSooATOngae+QbEgQeilgfknQfQvwfTwAf2yQeC1Aeb1Afa3wfv4wf87"
        "gen9wfm/Qf//QekgQgFr58EzrsHj74Hw8AHycAHAAA="
    )
    cfg = Config.load()
    db = CardDB.load(cfg.cards_path)
    deck = DeckTracker.from_deckstring(code, db, name="测试套牌")

    def make_recon(reveal_all: bool = False) -> GameStateReconstructor:
        r = GameStateReconstructor()
        r.turn, r.step, r.state, r.current_player = 4, "MAIN_ACTION", "RUNNING", 1
        for pid, pe_id, hid in ((1, 2, 64), (2, 3, 66)):
            pe = r._entity(pe_id)
            pe.declared.update({
                T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
                T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.RESOURCES: "4",
            })
            pe.tags[T.RESOURCES] = "4"
            h = r._entity(hid)
            h.card_id = "HERO_02a" if pid == 1 else "HERO_05"
            h.declared.update({
                T.CARDTYPE: "HERO", T.CONTROLLER: str(pid), T.ZONE: "PLAY",
                T.ATK: "0", T.HEALTH: "30",
            })
        hands = (
            (10, "CORE_LOOT_373", "治疗之雨"), (88, "TLC_888", "云端翔龙"),
            (30, "CORE_EX1_259", "闪电风暴"), (14, "CORE_CS2_189", "妖术"),
        ) if reveal_all else (
            (10, "", ""), (88, "", ""),
            (30, "CORE_EX1_259", "闪电风暴"), (14, "CORE_CS2_189", "妖术"),
        )
        for i, (eid, cid, nm) in enumerate(hands, 1):
            e = r._entity(eid)
            e.card_id, e.name = cid, nm
            e.declared.update({
                T.CARDTYPE: "SPELL" if cid else "INVALID",
                T.CONTROLLER: "1", T.ZONE: "HAND", T.ZONE_POSITION: str(i),
            })
            e.tags.update({T.ZONE: "HAND", T.ZONE_POSITION: str(i), T.CONTROLLER: "1"})
        return r

    r = make_recon()
    hand = r.player_view(1).hand
    check(len(hand) == 4 and sum(1 for e in hand if not e.card_id) == 2,
          "构造出 2 张未揭示 + 2 张已揭示手牌")

    d0 = describe_entity(hand[0], db, index=1)
    check(d0["名称"] == "（卡面未揭示）" and "实体" not in d0["名称"],
          "内部回退名「实体N」不再写进 payload")
    check(d0.get("卡面未知") is True and d0.get("不可用于出牌步骤") is True,
          "未知牌显式标记禁止用于出牌步骤")

    _, user = build_prompt(r, db, deck, 1)
    p = json.loads(user)
    check(list(p.keys())[0] == "【未揭示手牌推演】",
          "未揭示手牌推演排在 payload 最前面")
    pool = p["【未揭示手牌推演】"]
    check(pool["未揭示张数"] == 2 and pool["候选池卡种"] > 0,
          "候选池给出未揭示张数与剩余卡种")
    check(all("法力消耗" in x and "效果" in x for x in pool["候选池"]),
          "候选池每条都带费用与中文效果文本")
    check("严禁把某个候选指派" in pool["使用规则"], "候选池明确不可指派给编号")
    check(p["我方"]["手牌总数"] == 4 and p["我方"]["已揭示手牌数"] == 2
          and p["我方"]["未揭示手牌数"] == 2, "手牌总数仍按真实数量统计")
    check("卡面未知（最高优先级之一" in SYSTEM_PROMPT
          and "严禁把这些编号写进" in SYSTEM_PROMPT,
          "系统提示词规则 6 禁止对未知牌瞎猜")
    check("按「有N张手牌但内容未知」来给建议" not in SYSTEM_PROMPT,
          "旧版鼓励猜测的规则 6 已移除")
    check("只允许填入带「卡号」的已揭示编号" in SYSTEM_PROMPT,
          "输出格式限定 hand_hints 只准引用已揭示编号")

    bad = Advice(
        summary="按计划铺场",
        plan=["#1 实体14 若为低费随从可本回合打出，需按实际费用判断",
              "用已知随从交换（2费）"],
        warnings=["#3 实体30 如果是过牌可以配合现有随从", "注意对手下回合威胁"],
        hand_hints=[
            {"index": 1, "name": "实体14", "reason": "卡面未知，若为低费随从/解牌可本回合打出"},
            {"index": 2, "name": "实体88", "reason": "卡面未知，优先考虑是否能在4费内打出"},
            {"index": 3, "name": "闪电风暴", "reason": "唯一 AOE，保留"},
        ],
    )
    LLMClient._validate_unknown_hand(bad, r, 1)
    check([h.get("index") for h in bad.hand_hints] == [3],
          "硬校验剔除未知编号的 hand_hints，只留已揭示的 #3")
    check(all("若为" not in s and "实体" not in s for s in bad.plan),
          "硬校验移除猜测式出牌步骤")
    check(any("日志尚未揭示" in w for w in bad.warnings),
          "硬校验补上「日志尚未揭示手牌」告警")
    check("【手牌编号】" not in Advice(summary="s", plan=[], warnings=[]).to_text(),
          "无手牌提示时不渲染空白区块")

    # 不误伤：卡号全部就绪时不应出现候选池，兜底也不改动建议
    r2 = make_recon(reveal_all=True)
    _, user2 = build_prompt(r2, db, deck, 1)
    p2 = json.loads(user2)
    check("【未揭示手牌推演】" not in p2 and p2["我方"]["未揭示手牌数"] == 0,
          "手牌全部揭示时不注入候选池")
    ok = Advice(summary="正常出牌", plan=["打出妖术（3费）"],
                warnings=["注意过载"],
                hand_hints=[{"index": 1, "name": "治疗之雨", "reason": "回复"}])
    LLMClient._validate_unknown_hand(ok, r2, 1)
    check(ok.plan == ["打出妖术（3费）"] and len(ok.hand_hints) == 1
          and ok.warnings == ["注意过载"], "手牌就绪时兜底完全不改动建议")


def test_privacy_and_auto_correct() -> None:
    """回归：发往模型前脱敏 + 校验器驳回后自动改正一次。

    对应 2026-11-15 从 GitHub 同类项目借鉴的两项实践：
    ``CuZaoo`` 的脱敏层、``lscs-public`` 的「校验驳回 → 原因回传 → 重写一次」。
    """
    print("\n[测试23] 脱敏层与校验器自动改正")
    import json
    from hs_copilot.advisor import LLMClient
    from hs_copilot.cards import CardDB
    from hs_copilot.config import Config, _merge_dicts
    from hs_copilot.privacy import collect_secrets, scrub, scrub_messages

    cfg = Config.load()
    db = CardDB.load(cfg.cards_path)

    check(
        _merge_dicts(
            {"llm": {"api_key": "", "model": "x"}},
            {"llm": {"api_key": "secret"}},
        ) == {"llm": {"api_key": "secret", "model": "x"}},
        "config.local.yaml 深合并覆盖，不丢同段其它字段",
    )
    check(cfg.privacy.redact is True and cfg.llm.auto_correct is True,
          "脱敏与自动改正默认开启")

    class FakeCfg:
        player_name = "玩家甲"
        account_id = "100000001"

    r = GameStateReconstructor()
    r.turn, r.step, r.state, r.current_player = 4, "MAIN_ACTION", "RUNNING", 1
    r.player_names = {1: "玩家甲", 2: "机智的水獭"}
    secrets = collect_secrets(r, FakeCfg())
    check(secrets.get("玩家甲") == "[本机玩家]"
          and secrets.get("机智的水獭") == "[对手玩家]",
          "收集本机/对手显示名并映射为可区分的中性占位")
    check(secrets.get("100000001") == "[账号]", "战网账号 lo 也进入脱敏表")

    fake_key = "sk-" + "X" * 24
    dirty = (
        "01:27:06.8446260 Entity=玩家甲#12345 tag=RESOURCES value=6 "
        "GameAccountId=[hi=144115200000000001 lo=100000001] "
        + fake_key + " /Users/someone/Desktop/x "
        "/Applications/Hearthstone/Logs/Hearthstone_x/Power.log"
    )
    clean = scrub(dirty, secrets)
    for needle in ("玩家甲", "#12345", "100000001", "sk-", "/Users/",
                   "01:27:06", "tag=RESOURCES", "Power.log"):
        check(needle not in clean, f"脱敏移除 {needle}")

    payload = json.dumps(
        {"我方": {"生命": 28, "说明": "玩家甲 01:27:06.8446260 tag=ZONE value=HAND"},
         "对手": {"生命": 25}},
        ensure_ascii=False,
    )
    scrubbed = scrub(payload, secrets)
    parsed = json.loads(scrubbed)
    check(parsed["我方"]["生命"] == 28 and parsed["对手"]["生命"] == 25,
          "脱敏后 JSON 仍可解析，数值字段与结构未受损")
    check("玩家甲" not in scrubbed, "JSON 字符串值里的玩家名被替换")
    original_msgs = [{"role": "user", "content": "玩家甲"}]
    out_msgs = scrub_messages(original_msgs, secrets)
    check(out_msgs[0]["content"] == "[本机玩家]"
          and original_msgs[0]["content"] == "玩家甲",
          "scrub_messages 返回新列表，不修改调用方数据")

    # 自动改正用的最小局面：3 费、双方英雄 28 血、无手牌未揭示问题
    for pid, pe_id, hid in ((1, 2, 64), (2, 3, 66)):
        pe = r._entity(pe_id)
        pe.declared.update({
            T.CARDTYPE: "PLAYER", "PLAYER_ID": str(pid),
            T.CONTROLLER: str(pid), T.ZONE: "PLAY", T.RESOURCES: "3",
        })
        pe.tags[T.RESOURCES] = "3"
        hh = r._entity(hid)
        hh.card_id = "HERO_02a" if pid == 1 else "HERO_05"
        hh.declared.update({
            T.CARDTYPE: "HERO", T.CONTROLLER: str(pid), T.ZONE: "PLAY",
            T.ATK: "0", T.HEALTH: "28",
        })

    class Fake(LLMClient):
        def __init__(self, config, responses):
            self.cfg = config
            self.responses = list(responses)
            self.sent = []

        def available(self):
            return True

        def chat_messages(self, messages, max_tokens=None, secrets=None):
            self.sent.append([m["content"] for m in messages])
            return self.responses.pop(0) if self.responses else ""

    bad = json.dumps({
        "summary": "强攻", "lethal": False,
        "plan": ["打出妖术（5费）", "打出闪电风暴（5费）"],
        "warnings": [], "hand_hints": [], "plan_step": "",
    }, ensure_ascii=False)
    good = json.dumps({
        "summary": "修正后", "lethal": False,
        "plan": ["打出妖术（2费）"],
        "warnings": [], "hand_hints": [], "plan_step": "",
    }, ensure_ascii=False)
    still = json.dumps({
        "summary": "还是超费", "lethal": False,
        "plan": ["打出妖术（9费）"],
        "warnings": [], "hand_hints": [], "plan_step": "",
    }, ensure_ascii=False)

    f = Fake(cfg, [bad, good])
    adv = f.advise(r, db, None, 1)
    check(len(f.sent) == 2 and [len(x) for x in f.sent] == [2, 4],
          "驳回后只追加一次改正请求（2 条 → 4 条消息）")
    check(f.sent[1][0] == f.sent[0][0] and f.sent[1][1] == f.sent[0][1]
          and f.sent[1][2] == bad,
          "前三条消息完全一致，可命中服务商前缀缓存")
    check("超过本回合可用 3 费" in f.sent[1][3],
          "校验器把具体驳回原因回传给模型")
    check(adv.summary == "修正后" and adv.plan == ["打出妖术（2费）"],
          "采用改正后的干净方案，而不是带警告的旧方案")
    check(not any("⚠️" in w for w in adv.warnings) and adv.rejections == [],
          "改正版不再带兜底警告，内部驳回字段已清空")

    cfg.llm.auto_correct = False
    f2 = Fake(cfg, [bad])
    adv2 = f2.advise(r, db, None, 1)
    check(len(f2.sent) == 1 and any("超费" in w for w in adv2.warnings),
          "auto_correct=false 时只请求一次并保留本地警告")
    cfg.llm.auto_correct = True

    f3 = Fake(cfg, [bad, still])
    adv3 = f3.advise(r, db, None, 1)
    check(len(f3.sent) == 2 and "自动改正未通过" in adv3.warnings[0],
          "改正后仍驳回时不再重试，保留本地修正版")

    f4 = Fake(cfg, ["这不是 JSON", good])
    adv4 = f4.advise(r, db, None, 1)
    check(len(f4.sent) == 2 and adv4.summary == "修正后",
          "输出非法 JSON 也能通过自动改正救回")


if __name__ == "__main__":
    print("=" * 56)
    print("局面读取回归测试")
    print("=" * 56)
    test_minion_count()
    test_hero_power_zone()
    test_hero_hp()
    test_player_name_binding()
    test_backfill_current_game()
    test_hand_unknown_cards()
    test_first_player_name_binding()
    test_scan_player_identity()
    test_no_unbound_closure_vars()
    test_idle_and_finished_state()
    test_turn_advice_trigger_once()
    test_gui_identity_resolution()
    test_account_id_identity()
    test_card_and_prompt_payload()
    test_follow_latest_log_dir()
    test_first_player_other_side_and_mana()
    test_current_player_and_turn_tags()
    test_summoning_sickness()
    test_advice_only_on_my_turn_start()
    test_health_not_ready_guard()
    test_game_plan()
    test_unknown_hand_guard()
    test_privacy_and_auto_correct()
    print("\n" + "=" * 56)
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项失败：")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("✓ 全部通过")
    raise SystemExit(0)